"""Regression tests for lease and transport recovery (no model provider)."""
import asyncio
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from byte_agent.jobs import JobQueue, LostLease, Worker
from byte_agent.mcp_client import MCPClient, MCPToolError, SyncMCPClient
from byte_agent.model import Scripted
from byte_agent.runtime import Runtime
from byte_agent.tools import Tool


class LeaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_lease_loss_preserves_journal_and_reclaims(self):
        for mode in ("expired", "different_token", "different_owner"):
            with self.subTest(mode=mode):
                directory = self.root / mode
                queue = JobQueue(directory / "queue.sqlite")
                enqueued = queue.enqueue("read the snapshot", max_attempts=5)
                reads = []
                tools = {"snapshot": Tool("snapshot", "Read fixture data", {}, [], lambda: reads.append("read") or {"value": 1})}

                def expire():
                    db = sqlite3.connect(queue.path)
                    try:
                        with db:
                            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, enqueued.id))
                    finally:
                        db.close()

                class Model(Scripted):
                    def __init__(self):
                        super().__init__([{"calls": [{"name": "snapshot", "arguments": {}}]}, {"answer": "fixture done"}])
                        self.displaced = False

                    def complete(self, messages, specifications):
                        decision = super().complete(messages, specifications)
                        if not self.displaced:
                            self.displaced = True
                            if mode == "different_owner":
                                db = sqlite3.connect(queue.path)
                                try:
                                    with db:
                                        db.execute("UPDATE jobs SET worker='other-owner' WHERE id=?", (enqueued.id,))
                                finally:
                                    db.close()
                            else:
                                expire()
                                if mode == "different_token":
                                    replacement = queue.claim("replacement", lease_seconds=30)
                                    self.assert_claimed = replacement.id
                        return decision

                model = Model()

                def handler(job, cancelled):
                    return Runtime(directory / "run", model, tools).run(job.task, is_cancelled=cancelled)

                Worker(queue, handler, worker_id="first", lease_seconds=30).run_once()
                db = sqlite3.connect(directory / "run" / "journal.sqlite")
                try:
                    state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
                finally:
                    db.close()
                self.assertEqual("running", state["status"], "lost ownership must not permanently cancel the Agent journal")
                self.assertEqual(1, len(state["pending"]))
                self.assertEqual([], reads)
                expire()
                recovered = Worker(queue, handler, worker_id="recovered", lease_seconds=30).run_once()
                self.assertEqual("completed", recovered.status)
                self.assertEqual("completed", recovered.result["status"])
                self.assertEqual("fixture done", recovered.result["answer"])
                self.assertEqual(["read"], reads)

    def test_user_cancellation_still_cancels_runtime(self):
        queue = JobQueue(self.root / "queue.sqlite")
        enqueued = queue.enqueue("cancel read")
        reads = []
        tools = {"snapshot": Tool("snapshot", "Read fixture data", {}, [], lambda: reads.append("read") or {})}

        class Model(Scripted):
            def __init__(self):
                super().__init__([{"calls": [{"name": "snapshot", "arguments": {}}]}])

            def complete(self, messages, specifications):
                decision = super().complete(messages, specifications)
                queue.cancel(enqueued.id)
                return decision

        model = Model()
        result = Worker(queue, lambda job, cancelled: Runtime(self.root / "run", model, tools).run(job.task, is_cancelled=cancelled)).run_once()
        self.assertEqual("cancelled", result.status)
        self.assertEqual([], reads)
        trace = json.loads((self.root / "run" / "trace.json").read_text(encoding="utf-8"))
        self.assertEqual("cancelled", trace["status"])


class TransportFailureTests(unittest.TestCase):
    def test_transport_loss_is_observation_without_queue_retry(self):
        class BrokenSession:
            async def call_tool(self, *args, **kwargs):
                raise OSError("remote disconnected")

        client = MCPClient("unused")
        client.session = BrokenSession()
        with tempfile.TemporaryDirectory() as dirname:
            directory = Path(dirname)
            queue = JobQueue(directory / "queue.sqlite")
            enqueued = queue.enqueue("inspect snapshot")
            tools = {"snapshot": Tool("snapshot", "Read fixture", {}, [],
                lambda: asyncio.run(client.call_tool("snapshot", {})))}
            model = Scripted([{"calls": [{"name": "snapshot", "arguments": {}}]}, {"answer": "Tool unavailable; collect evidence."}])
            result = Worker(queue, lambda job, cancelled: Runtime(directory / "run", model, tools).run(job.task, is_cancelled=cancelled)).run_once()
            self.assertEqual(enqueued.id, result.id)
            self.assertEqual("completed", result.status)
            self.assertEqual(1, result.attempts)
            self.assertEqual("completed", result.result["status"])
            tool_event = next(event for event in result.result["events"] if event["kind"] == "tool")
            self.assertEqual("MCPToolError", tool_event["error"])
            self.assertIn("OSError", tool_event["output"]["error"])

    def test_sdk_call_timeout_is_a_tool_observation_error(self):
        class BrokenSession:
            async def call_tool(self, *args, **kwargs):
                raise TimeoutError("read timed out")

        async def check():
            client = MCPClient("unused")
            client.session = BrokenSession()
            with self.assertRaises(MCPToolError) as error:
                await client.call_tool("snapshot", {})
            self.assertIsInstance(error.exception, ValueError)
            self.assertIsInstance(error.exception.__cause__, TimeoutError)
        asyncio.run(check())

    def test_sync_bridge_timeout_is_a_tool_observation_error(self):
        class Thread:
            def is_alive(self):
                return True

        class Loop:
            def call_soon_threadsafe(self, *args):
                pass

        class Requests:
            def put_nowait(self, value):
                pass

        class Pending:
            def result(self, timeout):
                raise TimeoutError("bridge deadline elapsed")

        client = SyncMCPClient("unused")
        client._thread, client._loop, client._requests = Thread(), Loop(), Requests()
        with patch("concurrent.futures.Future", Pending), self.assertRaises(MCPToolError) as error:
            client.call_tool("snapshot", {})
        self.assertIsInstance(error.exception.__cause__, TimeoutError)

    def test_async_shutdown_does_not_replace_primary_error(self):
        class BrokenStack:
            async def __aexit__(self, *args):
                raise RuntimeError("shutdown failed")

        class Client(MCPClient):
            async def __aenter__(self):
                self._stack = BrokenStack()
                self.session = object()
                return self

        primary = ValueError("primary failure")
        async def check():
            with self.assertRaises(ValueError) as caught:
                async with Client("unused"):
                    raise primary
            self.assertIs(primary, caught.exception)
            with self.assertRaises(MCPToolError):
                async with Client("unused"):
                    pass
        asyncio.run(check())

    def test_sync_shutdown_does_not_replace_primary_error(self):
        class Thread:
            def join(self, timeout):
                pass
            def is_alive(self):
                return False

        class Client(SyncMCPClient):
            def __enter__(self):
                self._thread = Thread()
                return self
            def _request(self, *args):
                raise TimeoutError("shutdown failed")

        primary = ValueError("primary failure")
        with self.assertRaises(ValueError) as caught:
            with Client("unused"):
                raise primary
        self.assertIs(primary, caught.exception)
        with self.assertRaises(MCPToolError):
            with Client("unused"):
                pass


@unittest.skipUnless(importlib.util.find_spec("mcp"), "install the MCP extra")
class ActualDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = self.root / "server.py"
        self.server.write_text('''import asyncio,os
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool
server=Server("disconnect-fixture")
@server.list_tools()
async def tools():
    return [Tool(name="die",description="Abruptly exit this test process",inputSchema={"type":"object","properties":{}})]
@server.call_tool()
async def call(name,arguments):
    os._exit(23)
async def main():
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())
asyncio.run(main())
''', encoding="utf-8")
        self.env = {"PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONUTF8": "1"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_actual_async_server_disconnect_is_normalized(self):
        async def check():
            primary = []
            with self.assertRaises(MCPToolError) as caught:
                async with MCPClient(sys.executable, [str(self.server)], self.env, timeout=90) as client:
                    self.assertEqual("die", (await client.list_tools())[0]["name"])
                    try:
                        await client.call_tool("die", {})
                    except MCPToolError as exc:
                        primary.append(exc)
                        raise
            self.assertEqual(1, len(primary), "the failure must come from the actual call, not initialization")
            self.assertIs(primary[0], caught.exception)
        asyncio.run(check())

    def test_actual_sync_disconnect_preserves_call_error(self):
        primary = []
        with self.assertRaises(MCPToolError) as caught:
            with SyncMCPClient(sys.executable, [str(self.server)], self.env, timeout=90) as client:
                try:
                    client.call_tool("die", {})
                except MCPToolError as exc:
                    primary.append(exc)
                    raise
        self.assertEqual(1, len(primary))
        self.assertIs(primary[0], caught.exception)


if __name__ == "__main__":
    unittest.main()
