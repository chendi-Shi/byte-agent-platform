import io
import json
import secrets
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from byte_agent.distributed import (AgentJobHandler, HTTPJobQueue, HTTPWorker, QueueApplication,
    QueueTransportError, MAX_REQUEST_BYTES, demo_server)
from byte_agent.jobs import LostLease


class DistributedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.token = secrets.token_hex(32)
        self.server = demo_server(Path(self.temporary.name) / "jobs.db", self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = HTTPJobQueue(f"http://127.0.0.1:{self.server.server_port}", self.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def test_idempotency_conflict_and_json_limits(self):
        job = self.client.enqueue("task", {"service": "demo"}, idempotency_key="one")
        self.assertEqual(job.id, self.client.enqueue("task", {"service": "demo"}, idempotency_key="one").id)
        with self.assertRaises(ValueError):
            self.client.enqueue("different", idempotency_key="one")
        with self.assertRaises(ValueError):
            self.client.enqueue("task", {"value": float("nan")})
        with self.assertRaises(ValueError):
            self.client._request("get", {"id": job.id, "unexpected": True})
        with self.assertRaises(ValueError):
            self.client.claim("worker", lease_seconds=3601)

    def test_stale_fencing_after_expired_claim(self):
        original = self.client.enqueue("task")
        first = self.client.claim("first", lease_seconds=.1)
        time.sleep(.15)
        second = self.client.claim("second", lease_seconds=30)
        self.assertEqual(original.id, second.id)
        self.assertEqual(second.attempts, 2)
        self.assertNotEqual(first.lease_token, second.lease_token)
        for operation in (lambda: self.client.check(first), lambda: self.client.complete(first, "late"),
                          lambda: self.client.heartbeat(first), lambda: self.client.fail(first, "late")):
            with self.assertRaises(LostLease):
                operation()
        self.assertEqual(self.client.complete(second, {"ok": True}).status, "completed")

    def test_heartbeat_and_cancellation(self):
        queued = self.client.enqueue("queued")
        self.assertEqual(self.client.cancel(queued.id).status, "cancelled")
        active = self.client.enqueue("active")
        # Freeze only the wall clock consulted by the coordinator. Real TCP
        # requests still execute against its DB. Advance beyond the original
        # deadline while staying inside the renewed deadline, without relying
        # on sub-second scheduling on a loaded Windows development machine.
        with patch("byte_agent.jobs.time.time", return_value=1000):
            claim = self.client.claim("worker", lease_seconds=30)
        with patch("byte_agent.jobs.time.time", return_value=1020):
            self.assertTrue(self.client.heartbeat(claim, lease_seconds=60))
        with patch("byte_agent.jobs.time.time", return_value=1040):
            self.assertFalse(self.client.check(claim))
            self.assertEqual(self.client.cancel(active.id).status, "running")
            self.assertTrue(self.client.check(claim))
            self.assertFalse(self.client.heartbeat(claim))
            self.assertEqual(self.client.complete(claim, {"discard": "yes"}).status, "cancelled")
        self.assertIsNone(self.client.get(active.id).result)

    def test_retry_limit_and_dead_worker_limit(self):
        original = self.client.enqueue("retry", max_attempts=2)
        first = self.client.claim("first")
        self.assertEqual(self.client.fail(first, "transient").status, "queued")
        second = self.client.claim("second")
        self.assertEqual(self.client.fail(second, "again").status, "failed")
        self.assertIsNone(self.client.claim("third"))
        exhausted = self.client.enqueue("dies", max_attempts=1)
        self.client.claim("dead", lease_seconds=.05)
        time.sleep(.08)
        self.assertIsNone(self.client.claim("replacement"))
        self.assertEqual(self.client.get(exhausted.id).status, "failed")
        self.assertEqual(self.client.get(original.id).attempts, 2)

    def test_remote_worker_renews_during_handler(self):
        original = self.client.enqueue("slow")
        def handle(job, cancelled):
            time.sleep(6)
            self.assertFalse(cancelled())
            return {"ok": True}
        result = HTTPWorker(self.client, handle, lease_seconds=5).run_once()
        self.assertEqual(result.id, original.id)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.attempts, 1)

    def test_worker_cancel_is_cooperative_and_discards_result(self):
        original = self.client.enqueue("cancel")
        def handle(job, cancelled):
            self.client.cancel(job.id)
            self.assertTrue(cancelled())
            return {"must_not_publish": True}
        result = HTTPWorker(self.client, handle).run_once()
        self.assertEqual(result.id, original.id)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.result)

    def test_unknown_transport_outcome_is_not_blindly_replayed(self):
        original = self.client.enqueue("ambiguity")
        real_complete = self.client.complete
        count = []
        def ambiguous_complete(job, result):
            count.append(job.id)
            real_complete(job, result)
            raise QueueTransportError("response lost after commit")
        with patch.object(self.client, "complete", ambiguous_complete):
            with self.assertRaises(QueueTransportError):
                HTTPWorker(self.client, lambda job, cancelled: {"ok": True}).run_once()
        self.assertEqual(count, [original.id])
        self.assertEqual(self.client.get(original.id).status, "completed")

    def test_authentication_origin_and_no_credential_output(self):
        wrong = HTTPJobQueue(self.client.base_url, secrets.token_hex(32))
        with self.assertRaises(QueueTransportError) as error:
            wrong.get("missing")
        self.assertNotIn(wrong.token, str(error.exception))
        for url in ("http://user:password@localhost", "file:///tmp/queue", "http://localhost?token=x", "http://localhost/v1"):
            with self.assertRaises(ValueError):
                HTTPJobQueue(url, self.token)
        with self.assertRaises(ValueError):
            HTTPJobQueue(self.client.base_url, "short")
        with self.assertRaises(ValueError):
            HTTPJobQueue(self.client.base_url, self.token, timeout=float("nan"))

    def test_redirect_does_not_forward_bearer_credential(self):
        redirected = []
        original = self.server.get_app()
        def target(environ, start_response):
            if environ.get("PATH_INFO") == "/v1/get":
                start_response("302 Found", [("Location", self.client.base_url + "/redirect-target"),
                                             ("Content-Length", "0")])
                return [b""]
            redirected.append(environ.get("HTTP_AUTHORIZATION"))
            return original(environ, start_response)
        self.server.set_app(target)
        with self.assertRaises(QueueTransportError) as error:
            self.client.get("missing")
        self.assertEqual(redirected, [])
        self.assertNotIn(self.token, str(error.exception))

    def test_server_rejects_oversized_duplicate_and_non_object_json(self):
        application = QueueApplication(Path(self.temporary.name) / "direct.db", self.token)
        def call(raw, **extra):
            status = []
            environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/v1/enqueue", "CONTENT_TYPE": "application/json",
                       "CONTENT_LENGTH": str(len(raw)), "HTTP_AUTHORIZATION": "Bearer " + self.token,
                       "wsgi.input": io.BytesIO(raw), **extra}
            result = b"".join(application(environ, lambda code, headers: status.append(code)))
            self.assertNotIn(self.token.encode(), result)
            return status[0]
        self.assertTrue(call(b'{"task":"one","task":"two"}').startswith("400"))
        self.assertTrue(call(b'[]').startswith("400"))
        self.assertTrue(call(b'{}', CONTENT_LENGTH=str(MAX_REQUEST_BYTES + 1)).startswith("413"))
        self.assertTrue(call(b'{}', HTTP_AUTHORIZATION="Bearer \u4e2d").startswith("401"))
        self.assertTrue(call(b'{}', REQUEST_METHOD="GET").startswith("405"))
        self.assertTrue(call(b'{}', CONTENT_TYPE="text/plain").startswith("415"))

    def test_wrong_worker_cannot_commit_with_stolen_record(self):
        self.client.enqueue("ownership")
        job = self.client.claim("actual")
        with self.assertRaises(LostLease):
            self.client.complete(replace(job, worker="other"), "wrong")
        self.assertEqual(self.client.complete(job, "right").result, "right")

    def test_actual_runtime_tools_scope_skill_and_verifier_over_tcp(self):
        import importlib.util
        from byte_agent.domain import create_dataset
        from byte_agent.knowledge import Knowledge
        root = Path(__file__).resolve().parents[1]
        module = importlib.util.spec_from_file_location("fixture_example", root / "examples" / "distributed_demo.py")
        example = importlib.util.module_from_spec(module)
        module.loader.exec_module(example)
        data = Path(self.temporary.name) / "node" / "data"
        create_dataset(data)
        knowledge = Knowledge(data / "knowledge.sqlite")
        try:
            knowledge.ingest(data / "knowledge")
        finally:
            knowledge.db.close()
        skill = Path(self.temporary.name) / "operator.md"
        skill.write_text("---\nname: network-test\ndescription: operator test\n---\nUse exact service filters.\n", encoding="utf-8")
        handler = AgentJobHandler(data, Path(self.temporary.name) / "node" / "runs",
                                  example.fixture_model(data), service="social-follow", verify=True, skill=skill)
        queued = self.client.enqueue("Diagnose social-follow with current 30-minute observations.", {"service": "social-follow"})
        final = HTTPWorker(self.client, handler).run_once()
        self.assertEqual(final.id, queued.id)
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.result["agent_status"], "completed")
        trace = json.loads(Path(final.result["trace"]).read_text(encoding="utf-8"))
        self.assertTrue(trace["fixture"])
        self.assertEqual(trace["calls"], 3)
        self.assertEqual([e["valid"] for e in trace["events"] if e["kind"] == "validation"], [True])
        self.assertIn("network-test", trace["messages"][0]["content"])
        self.client.enqueue("second same-service task", {"service": "social-follow"})
        repeated = HTTPWorker(self.client, handler).run_once()
        self.assertEqual(repeated.result["agent_status"], "completed")
        repeated_trace = json.loads(Path(repeated.result["trace"]).read_text(encoding="utf-8"))
        self.assertEqual(repeated_trace["calls"], 3)
        self.client.enqueue("different service", {"service": "creator-upload"}, max_attempts=1)
        conflict = HTTPWorker(self.client, handler).run_once()
        self.assertEqual(conflict.status, "failed")
        self.assertIn("conflicts", conflict.error)


if __name__ == "__main__":
    unittest.main()
