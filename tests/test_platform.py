import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from byte_agent.cli import seed_metrics
from byte_agent.knowledge import Knowledge
from byte_agent.model import Scripted, Ollama
from byte_agent.runtime import Runtime
from byte_agent.tools import registry
from byte_agent.mcp import handle


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "a.md").write_text("search error rollback requires review", encoding="utf-8")
        self.kb = Knowledge(self.root / "kb.sqlite")
        self.kb.ingest(self.docs)
        self.metrics = self.root / "metrics.sqlite"
        seed_metrics(self.metrics)
        self.tools = registry(self.kb, self.metrics)

    def tearDown(self):
        self.kb.db.close()
        self.tmp.cleanup()

    def test_retrieval_version_and_deletion(self):
        first = self.kb.search("rollback")[0]["id"]
        (self.docs / "a.md").write_text("search rollback v2", encoding="utf-8")
        self.kb.ingest(self.docs)
        self.assertNotEqual(first, self.kb.search("rollback")[0]["id"])
        (self.docs / "a.md").unlink()
        self.kb.ingest(self.docs)
        self.assertEqual([], self.kb.search("rollback"))

    def test_dense_fusion_dimension(self):
        self.kb.ingest(self.docs, lambda texts: [[1., 0.] for t in texts])
        self.assertTrue(self.kb.search("unmatched", vector=[1., 0.]))
        with self.assertRaises(ValueError):
            self.kb.search("query", vector=[1.])

    def test_repeated_paragraphs_and_empty_dense_index(self):
        (self.docs / "a.md").write_text("same paragraph\n\nsame paragraph", encoding="utf-8")
        self.assertEqual(2, self.kb.ingest(self.docs))
        (self.docs / "a.md").unlink()
        self.assertEqual(0, self.kb.ingest(self.docs, lambda texts: []))

    def test_input_fingerprint_tracks_content_not_sqlite_bytes(self):
        revision = self.tools["service_metrics"].revision
        seed_metrics(self.metrics)
        self.assertEqual(revision, registry(self.kb, self.metrics)["service_metrics"].revision)
        connection = sqlite3.connect(self.metrics)
        with connection:
            connection.execute("UPDATE metrics SET errors=60 WHERE service='search'")
        connection.close()
        self.assertNotEqual(revision, registry(self.kb, self.metrics)["service_metrics"].revision)

    def test_read_only_and_schema(self):
        self.assertEqual(.05, self.tools["service_metrics"].call({"service": "search"})["error_rate"])
        self.assertEqual(0, self.tools["service_metrics"].call({"service": "' OR 1=1 --"})["samples"])
        for args in ({"service": "search", "sql": "DROP TABLE metrics"}, {"service": "search", "window_minutes": True}, {"service": "search", "window_minutes": 99}):
            with self.assertRaises(ValueError):
                self.tools["service_metrics"].call(args)

    def test_resume_durable_pending(self):
        model = Scripted([{"calls": [{"name": "service_metrics", "arguments": {"service": "search"}}]}, {"answer": "ok"}])
        runtime = Runtime(self.root / "run", model, self.tools)
        with self.assertRaises(RuntimeError):
            runtime.run("task", crash_after_model=True)
        result = runtime.run("task")
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["calls"])
        self.assertEqual(2, result["unknown_usage"])
        self.assertEqual(result, runtime.run("task"))
        with self.assertRaises(ValueError):
            runtime.run("other task")

    def test_budget_blocks_tools(self):
        model = Scripted([{"calls": [{"name": "service_metrics", "arguments": {"service": "search"}}] * 2}])
        result = Runtime(self.root / "run", model, self.tools, max_calls=1).run("task")
        self.assertEqual("budget_exceeded", result["status"])
        self.assertEqual(1, result["calls"])

    def test_unknown_tool_is_observation(self):
        model = Scripted([{"calls": [{"name": "deploy", "arguments": {}}]}, {"answer": "blocked"}])
        result = Runtime(self.root / "run", model, self.tools).run("task")
        event = next(e for e in result["events"] if e["kind"] == "tool")
        self.assertEqual("ValueError", event["error"])

    def test_provider_loss_is_not_retried(self):
        class Broken:
            fixture = False
            count = 0
            def complete(self, messages, tools):
                self.count += 1
                raise TimeoutError()
        model = Broken()
        runtime = Runtime(self.root / "run", model, self.tools)
        self.assertEqual("uncertain", runtime.run("task")["status"])
        resumed = runtime.run("task")
        self.assertEqual("uncertain", resumed["status"])
        self.assertEqual(1, resumed["unknown_usage"])
        self.assertEqual(1, model.count)

    def test_mcp_subprocess(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "service_metrics", "arguments": {"service": "search"}}}]
        # CLI expects knowledge.sqlite; copy the current logical database.
        target = sqlite3.connect(self.root / "knowledge.sqlite")
        self.kb.db.backup(target)
        target.close()
        process = subprocess.run([sys.executable, "-m", "byte_agent", "mcp", "--data", str(self.root)], input="\n".join(json.dumps(r) for r in requests) + "\n", text=True, capture_output=True, timeout=15)
        self.assertEqual(0, process.returncode, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(2, len(replies))
        self.assertEqual(.05, replies[1]["result"]["structuredContent"]["error_rate"])

    def test_ollama_wire_conversion(self):
        model = Ollama("test")
        model.request = lambda path, body: {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "service_metrics", "arguments": {"service": "search"}}}]}, "prompt_eval_count": 10, "eval_count": 5}
        result = model.complete([], [t.spec() for t in self.tools.values()])
        self.assertEqual("service_metrics", result["calls"][0]["name"])
        self.assertEqual({"input": 10, "output": 5}, result["usage"])


if __name__ == "__main__":
    unittest.main()
