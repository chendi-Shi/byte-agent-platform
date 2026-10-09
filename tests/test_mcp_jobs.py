import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from byte_agent.jobs import JobQueue, LostLease, Worker
from byte_agent.knowledge import Knowledge
from byte_agent.mcp_client import MCPClient, MCPToolError, SyncMCPClient


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.queue = JobQueue(self.root / "jobs.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotency_and_policy_collision(self):
        job = self.queue.enqueue("diagnose search", {"service": "search"}, idempotency_key="request-1")
        repeated = JobQueue(self.queue.path).enqueue("diagnose search", {"service": "search"}, idempotency_key="request-1")
        self.assertEqual(job.id, repeated.id)
        with self.assertRaises(ValueError):
            self.queue.enqueue("different task", {"service": "search"}, idempotency_key="request-1")
        with self.assertRaises(ValueError):
            self.queue.enqueue("diagnose search", {"service": "search"}, idempotency_key="request-1", max_attempts=4)

    def test_cross_process_claim_is_unique(self):
        job = self.queue.enqueue("one read-only task")
        code = """
import json,sys,time
from pathlib import Path
from byte_agent.jobs import JobQueue
queue=JobQueue(sys.argv[1])
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists(): time.sleep(.01)
job=queue.claim(sys.argv[2],lease_seconds=15)
print(json.dumps(job.id if job else None),flush=True)
"""
        go = self.root / "go"
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.queue.path),
                     str(self.root / f"ready-{index}"), str(go)], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True) for index in range(6)]
        try:
            deadline = time.monotonic() + 20
            while len(list(self.root.glob("ready-*"))) != len(processes):
                if time.monotonic() > deadline:
                    self.fail("workers failed to initialize")
                time.sleep(.02)
            go.write_text("start")
            outputs = [process.communicate(timeout=20) for process in processes]
            for process, (_, errors) in zip(processes, outputs):
                self.assertEqual(0, process.returncode, errors)
            claimed = [json.loads(output) for output, _ in outputs]
            self.assertEqual([job.id], [identity for identity in claimed if identity])
            self.assertEqual(1, self.queue.get(job.id).attempts)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_process_crash_is_recovered_after_lease(self):
        original = self.queue.enqueue("recover after abrupt worker death")
        code = """
import json,os,sys
from byte_agent.jobs import JobQueue
job=JobQueue(sys.argv[1]).claim('crashed-worker',lease_seconds=.15)
print(json.dumps({'id':job.id,'token':job.lease_token}),flush=True)
os._exit(23)
"""
        process = subprocess.run([sys.executable, "-c", code, str(self.queue.path)],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(23, process.returncode, process.stderr)
        crashed = json.loads(process.stdout)
        time.sleep(.18)
        result = Worker(self.queue, lambda job, cancelled: {"recovered": True}, lease_seconds=1).run_once()
        self.assertEqual(original.id, result.id)
        self.assertEqual(2, result.attempts)
        self.assertEqual("completed", result.status)
        self.assertEqual({"recovered": True}, result.result)
        self.assertNotEqual(crashed["token"], result.lease_token)

    def test_concurrent_process_workers_complete_each_task_once(self):
        jobs = [self.queue.enqueue("read snapshot", {"ordinal": index}) for index in range(18)]
        code = """
import json,sys,time
from pathlib import Path
from byte_agent.jobs import JobQueue,Worker
queue=JobQueue(sys.argv[1])
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists(): time.sleep(.01)
def read(job,cancelled):
    time.sleep(.03)
    return {'ordinal':job.payload['ordinal']}
worker=Worker(queue,read,lease_seconds=10)
completed=[]
while True:
    job=worker.run_once()
    if job is None: break
    completed.append(job.id)
print(json.dumps(completed),flush=True)
"""
        go = self.root / "go-workers"
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.queue.path),
                     str(self.root / f"worker-ready-{index}"), str(go)], stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, text=True) for index in range(3)]
        try:
            deadline = time.monotonic() + 20
            while len(list(self.root.glob("worker-ready-*"))) != len(processes):
                if time.monotonic() > deadline:
                    self.fail("workers failed to initialize")
                time.sleep(.02)
            go.write_text("start")
            completed = []
            for process in processes:
                output, errors = process.communicate(timeout=20)
                self.assertEqual(0, process.returncode, errors)
                completed.extend(json.loads(output))
            self.assertEqual(sorted(job.id for job in jobs), sorted(completed))
            for job in jobs:
                result = self.queue.get(job.id)
                self.assertEqual("completed", result.status)
                self.assertEqual(1, result.attempts)
                self.assertEqual({"ordinal": job.payload["ordinal"]}, result.result)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_stale_worker_cannot_commit_or_renew(self):
        self.queue.enqueue("fencing")
        old = self.queue.claim("old", lease_seconds=.03)
        time.sleep(.05)
        replacement = self.queue.claim("replacement", lease_seconds=1)
        self.assertNotEqual(old.lease_token, replacement.lease_token)
        for action in (lambda: self.queue.complete(old, {"wrong": True}),
                       lambda: self.queue.heartbeat(old), lambda: self.queue.fail(old, "old")):
            with self.assertRaises(LostLease):
                action()
        self.queue.complete(replacement, {"correct": True})
        self.assertEqual({"correct": True}, self.queue.get(old.id).result)

    def test_queued_and_running_cancellation(self):
        queued = self.queue.enqueue("cancel queued")
        self.assertEqual("cancelled", self.queue.cancel(queued.id).status)
        self.assertIsNone(self.queue.claim("worker"))
        running = self.queue.enqueue("cancel running")
        started = threading.Event()

        def handler(job, cancelled):
            started.set()
            while not cancelled():
                time.sleep(.005)
            return {"must_be_discarded": True}

        thread = threading.Thread(target=lambda: Worker(self.queue, handler, lease_seconds=1).run_once())
        thread.start()
        self.assertTrue(started.wait(3))
        self.queue.cancel(running.id)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        result = self.queue.get(running.id)
        self.assertEqual("cancelled", result.status)
        self.assertIsNone(result.result)

    def test_heartbeat_preserves_lease_during_blocking_callback(self):
        job = self.queue.enqueue("slow read")
        started = threading.Event()

        def handler(job, cancelled):
            started.set()
            time.sleep(4.5)
            return {"ok": True}

        thread = threading.Thread(target=lambda: Worker(self.queue, handler, lease_seconds=2).run_once())
        thread.start()
        self.assertTrue(started.wait(3))
        time.sleep(2.7)
        self.assertIsNone(self.queue.claim("other", lease_seconds=1))
        thread.join(8)
        self.assertEqual("completed", self.queue.get(job.id).status)
        self.assertEqual(1, self.queue.get(job.id).attempts)

    def test_retry_budget_and_cancelled_crash_recovery(self):
        job = self.queue.enqueue("retry read", max_attempts=2)

        def broken(job, cancelled):
            raise ValueError("fixture unavailable")

        worker = Worker(self.queue, broken)
        self.assertEqual("queued", worker.run_once().status)
        result = worker.run_once()
        self.assertEqual("failed", result.status)
        self.assertEqual(2, result.attempts)
        self.assertIsNone(worker.run_once())
        cancelled = self.queue.enqueue("crashed cancelled")
        self.queue.claim("crash", lease_seconds=.03)
        self.queue.cancel(cancelled.id)
        time.sleep(.05)
        self.assertIsNone(self.queue.claim("replacement"))
        self.assertEqual("cancelled", self.queue.get(cancelled.id).status)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "install the mcp optional extra for SDK interoperability")
class MCPInteropTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "search.md").write_text("search rollback requires human review", encoding="utf-8")
        knowledge = Knowledge(self.root / "knowledge.sqlite")
        knowledge.ingest(docs)
        knowledge.db.close()
        with sqlite3.connect(self.root / "metrics.sqlite") as db:
            db.execute("CREATE TABLE metrics(service TEXT,minute INTEGER,requests INTEGER,errors INTEGER,p95_ms REAL)")
            db.execute("INSERT INTO metrics VALUES ('search',1,1000,50,480)")
        db.close()
        self.args = ["-m", "byte_agent.mcp", "--data", str(self.root), "--sdk"]
        self.env = {"PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONUTF8": "1"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_official_sdk_initialize_discover_call_and_error(self):
        async def check():
            # This uses the official client directly, independently of our adapter.
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            async with stdio_client(StdioServerParameters(command=sys.executable, args=self.args, env=self.env)) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual("byte-agent-tools", initialized.serverInfo.name)
                    self.assertIsNotNone(initialized.capabilities.tools)
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    self.assertTrue({"knowledge_search", "service_metrics", "incident_changes"} <= names)
                    self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools.tools))
                    metrics = await session.call_tool("service_metrics", {"service": "search"})
                    self.assertFalse(metrics.isError)
                    self.assertEqual(.05, metrics.structuredContent["error_rate"])
                    evidence = await session.call_tool("knowledge_search", {"query": "rollback"})
                    self.assertTrue(evidence.structuredContent["evidence"])
                    changes = await session.call_tool("incident_changes", {"service": "search"})
                    self.assertFalse(changes.isError)
                    invalid = await session.call_tool("service_metrics", {"service": "search", "window_minutes": 999})
                    self.assertTrue(invalid.isError)
                    missing = await session.call_tool("deploy", {})
                    self.assertTrue(missing.isError)
        asyncio.run(check())

    def test_async_adapter_and_runtime_sync_tool_bridge(self):
        async def check():
            async with MCPClient(sys.executable, self.args, self.env) as client:
                self.assertEqual(3, len(await client.list_tools()))
                self.assertEqual(.05, (await client.call_tool("service_metrics", {"service": "search"}))["error_rate"])
                with self.assertRaises(MCPToolError):
                    await client.call_tool("deploy", {})
        asyncio.run(check())
        with SyncMCPClient(sys.executable, self.args, self.env) as client:
            tools = client.tools(["service_metrics", "knowledge_search"], revision="test-data-v1")
            self.assertEqual("service_metrics", tools["service_metrics"].spec()["name"])
            self.assertEqual(.05, tools["service_metrics"].call({"service": "search"})["error_rate"])
            with self.assertRaises(ValueError):
                client.tools(["deploy"], revision="test-data-v1")


if __name__ == "__main__":
    unittest.main()
