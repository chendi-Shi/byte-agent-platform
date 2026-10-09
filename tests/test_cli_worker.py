"""Exercise real queue/Runtime CLI binding without a provider request."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from byte_agent import cli
from byte_agent.jobs import JobQueue
from byte_agent.model import Scripted
from byte_agent.runtime import Runtime, RuntimeBusy
from byte_agent.tools import Tool


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue = JobQueue(self.root / "jobs.sqlite")
        self.accesses = []

    def tearDown(self):
        self.temp.cleanup()

    def tools(self):
        def metrics(service):
            self.accesses.append(("service_metrics", service))
            return {"service": service, "window_minutes": 30, "error_rate": .05,
                    "samples": 30, "requests": 30000}
        def knowledge(service, query):
            self.accesses.append(("knowledge_search", service))
            return {"service": service, "evidence": [{"id": "book", "source": service + "/runbook.md"}]}
        def changes(service):
            self.accesses.append(("incident_changes", service))
            return {"service": service, "evidence": [{"id": "change", "source": "changes/" + service}]}
        return {
            "service_metrics": Tool("service_metrics", "metrics", {"service": {"type": "string"}}, ["service"], metrics, "fixture"),
            "knowledge_search": Tool("knowledge_search", "knowledge", {"service": {"type": "string"}, "query": {"type": "string"}}, ["query"], knowledge, "fixture"),
            "incident_changes": Tool("incident_changes", "changes", {"service": {"type": "string"}}, ["service"], changes, "fixture"),
        }

    def execute(self, payload, call_service="search", worker_service=None, verify=False):
        job = self.queue.enqueue("Diagnose target observations", payload, max_attempts=1)
        self.current_job = job
        calls = [{"name": "service_metrics", "arguments": {"service": call_service}}]
        answer = "Engineering fixture answer"
        if verify:
            calls += [{"name": "knowledge_search", "arguments": {"service": call_service, "query": "policy"}},
                      {"name": "incident_changes", "arguments": {"service": call_service}}]
            answer = json.dumps({"service": call_service, "error_rate": .05, "status": "incident",
                                 "likely_cause": "release_regression", "recommendation": "rollback_review",
                                 "citations": ["book", "change"], "uncertainty": "causality_unproven"})
        model = Scripted([{"calls": calls}, {"answer": answer}])
        argv = ["byte_agent", "worker", "--once", "--model", "fixture",
                "--queue", str(self.queue.path), "--data", str(self.root / "data"),
                "--output", str(self.root / "results")]
        if worker_service is not None:
            argv += ["--service", worker_service]
        if verify:
            argv += ["--verify"]
        with patch.object(sys, "argv", argv), patch.object(cli, "Ollama", return_value=model), \
                patch.object(cli, "registry", return_value=self.tools()), \
                patch.object(model, "complete", wraps=model.complete) as complete, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        current = self.queue.get(job.id)
        trace_path = self.root / "results" / job.id / "trace.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else None
        return current, trace, complete.call_count

    def test_payload_scopes_worker_even_without_verify(self):
        job, trace, calls = self.execute({"service": "search"})
        self.assertEqual("completed", job.status)
        self.assertEqual([("service_metrics", "search")], self.accesses)
        self.assertEqual("completed", trace["status"])
        self.assertEqual(2, calls)

    def test_payload_blocks_another_service_before_access_without_verify(self):
        job, trace, calls = self.execute({"service": "search"}, call_service="live")
        self.assertEqual("completed", job.status)
        self.assertEqual([], self.accesses)
        event = next(e for e in trace["events"] if e["kind"] == "tool")
        self.assertEqual("ValueError", event["error"])
        self.assertIn("invalid choice", event["output"]["error"])

    def test_conflicting_worker_scope_rejects_before_model(self):
        for verify in (False, True):
            with self.subTest(verify=verify):
                job, trace, calls = self.execute({"service": "search"}, worker_service="live", verify=verify)
                self.assertEqual("failed", job.status)
                self.assertIn("job service conflicts with worker service", job.error)
                self.assertEqual(0, calls)
                self.assertIsNone(trace)
                self.assertEqual([], self.accesses)

    def test_worker_scope_is_fallback_for_absent_payload(self):
        job, trace, calls = self.execute({}, worker_service="search")
        self.assertEqual("completed", job.status)
        self.assertEqual([("service_metrics", "search")], self.accesses)

    def test_equivalent_worker_configuration_keeps_resumable_identity(self):
        first, first_trace, _ = self.execute({"service": "search"})
        second, second_trace, _ = self.execute({"service": "search"}, worker_service="search")
        third, third_trace, _ = self.execute({}, worker_service="search")
        self.assertEqual("completed", second.status)
        self.assertEqual(first_trace["identity"], second_trace["identity"])
        self.assertEqual(first_trace["identity"], third_trace["identity"])

    def test_no_service_preserves_unscoped_legacy_worker(self):
        job, trace, calls = self.execute({}, call_service="live")
        self.assertEqual("completed", job.status)
        self.assertEqual([("service_metrics", "live")], self.accesses)

    def test_malformed_payload_service_does_not_silently_use_fallback(self):
        for service in (None, "", 0, [], "bad\nservice"):
            with self.subTest(service=service):
                job, trace, calls = self.execute({"service": service}, worker_service="search")
                self.assertEqual("failed", job.status)
                self.assertEqual(0, calls)
                self.assertIsNone(trace)
                self.assertEqual([], self.accesses)

    def test_verify_uses_same_payload_scope_and_observed_validator(self):
        job, trace, calls = self.execute({"service": "search"}, verify=True)
        self.assertEqual("completed", job.status)
        self.assertFalse(job.result["requires_review"])
        self.assertEqual("completed", trace["status"])
        self.assertEqual(3, len(self.accesses))
        self.assertTrue(next(e for e in trace["events"] if e["kind"] == "validation")["valid"])

    def test_verify_requires_scope_before_model(self):
        job, trace, calls = self.execute({}, verify=True)
        self.assertEqual("failed", job.status)
        self.assertIn("requires an authorized service", job.error)
        self.assertEqual(0, calls)
        self.assertIsNone(trace)

    def test_busy_runtime_waits_in_same_attempt_and_keeps_lease_renewable(self):
        attempts, renewals = [], []
        class ContendedRuntime:
            def __init__(inner, *args, **kwargs):
                inner.runtime = Runtime(*args, **kwargs)
            def run(inner, *args, **kwargs):
                attempts.append(True)
                if len(attempts) <= 2:
                    raise RuntimeBusy("other writer still owns run")
                return inner.runtime.run(*args, **kwargs)
        def waiting(seconds):
            self.assertEqual(.1, seconds)
            current = self.queue.get(self.current_job.id)
            self.assertEqual("running", current.status)
            self.assertTrue(self.queue.heartbeat(current, lease_seconds=30))
            renewals.append(current.lease_token)
        # Deterministic renewal assertions avoid depending on host scheduling;
        # the separate Worker blocking test exercises its heartbeat thread.
        with patch.object(cli, "Runtime", ContendedRuntime), patch.object(cli.time, "sleep", waiting):
            job, trace, calls = self.execute({"service": "search"})
        self.assertEqual("completed", job.status)
        self.assertEqual(1, job.attempts)
        self.assertEqual(3, len(attempts))
        self.assertEqual(2, len(renewals))
        self.assertEqual(renewals[0], renewals[1])
        self.assertEqual(2, calls)


if __name__ == "__main__":
    unittest.main()
