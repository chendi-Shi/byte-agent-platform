import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from byte_agent.connectors import load_sources
from byte_agent.domain import answer_schema, create_dataset
from byte_agent.knowledge import Knowledge
from byte_agent.tools import registry


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifest = create_dataset(self.root / "data")
        self.connector = load_sources(self.root / "data" / "connection.json")
        self.knowledge = Knowledge(self.root / "index.sqlite")
        self.connector.ingest_into(self.knowledge)
        self.tools = registry(self.knowledge, self.connector.metrics_database)

    def tearDown(self):
        self.knowledge.db.close()
        self.tmp.cleanup()

    def test_independent_splits_and_observed_business_results(self):
        tasks = self.manifest["tasks"]
        dev = {task["service"] for task in tasks if task["split"] == "dev"}
        holdout = {task["service"] for task in tasks if task["split"] == "holdout"}
        self.assertEqual((24, 8), (len(dev), len(holdout)))
        self.assertFalse(dev & holdout)
        self.assertEqual(32, len({task["thresholds"]["error_rate"] for task in tasks}))
        for task in tasks:
            with self.subTest(service=task["service"]):
                metrics = self.tools["service_metrics"].call({"service": task["service"], "window_minutes": 30})
                self.assertEqual(task["error_rate"], metrics["error_rate"])
                if metrics["samples"]:
                    incident = metrics["recent_error_rate"] >= task["thresholds"]["error_rate"] or metrics["recent_max_p95_ms"] > task["thresholds"]["p95_ms"]
                    if task["runbook_required"]:
                        self.assertEqual(task["status"] == "incident", incident)
                    self.assertEqual(59, metrics["as_of_minute"])
                else:
                    self.assertEqual("insufficient_data", task["status"])
                    self.assertIsNone(metrics["recent_error_rate"])
                knowledge = self.tools["knowledge_search"].call({"query": task["query"], "service": task["service"], "limit": 20})
                self.assertEqual(task["runbook_required"], bool(knowledge["evidence"]))
                self.assertTrue(all(row["source"] == task["source"] for row in knowledge["evidence"]))
                changes = self.tools["incident_changes"].call({"service": task["service"]})
                current = [row for row in changes["evidence"] if row["kind"] == task["expected_change_kind"] and row["status"] == task["expected_change_status"]]
                self.assertTrue(current)
                self.assertTrue(all(row["source"] == task["causal_source"] for row in current))
                self.assertEqual({row["evidence_id"] for row in changes["changes"]}, {row["id"] for row in changes["evidence"]})

    def test_filters_do_not_fall_back_or_interpolate_sql(self):
        service = self.manifest["tasks"][0]["service"]
        own = self.knowledge.search("incident policy", 20, service=service, source=service + "/runbook.md")
        self.assertTrue(own)
        self.assertEqual({service + "/runbook.md"}, {row["source"] for row in own})
        for attack in ("' OR 1=1 --", "unknown-service"):
            self.assertEqual([], self.knowledge.search("incident", service=attack))
            self.assertEqual([], self.tools["incident_changes"].call({"service": attack})["changes"])
            self.assertEqual(0, self.tools["service_metrics"].call({"service": attack})["samples"])
        self.assertEqual([], self.knowledge.search("incident", service=service, source="../services.json"))
        with self.assertRaises(ValueError):
            self.tools["knowledge_search"].call({"query": "incident", "service": ""})

    def test_real_connector_keeps_source_database_unchanged(self):
        database = self.connector.metrics_database
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        self.tools["service_metrics"].call({"service": "growth-feed"})
        self.tools["incident_changes"].call({"service": "growth-feed"})
        self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
        connection = sqlite3.connect(database)
        with connection:
            connection.execute("UPDATE metrics SET errors=requests+1 WHERE service='growth-feed' AND minute=0")
        connection.close()
        with self.assertRaisesRegex(ValueError, "invalid counts"):
            self.connector.validate()

    def test_missing_source_is_not_replaced_with_fixture(self):
        path = self.root / "connection.json"
        path.write_text(json.dumps({"knowledge_root": "data/knowledge", "metrics_database": "absent.sqlite"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "existing SQLite"):
            load_sources(path)
        self.assertFalse((self.root / "absent.sqlite").exists())

    def test_legacy_database_and_knowledge_migration(self):
        old_path = self.root / "old.sqlite"
        connection = sqlite3.connect(old_path)
        with connection:
            connection.execute("CREATE TABLE metrics (service TEXT,minute INTEGER,requests INTEGER,errors INTEGER,p95_ms REAL)")
            connection.execute("INSERT INTO metrics VALUES ('search',0,100,5,300)")
        connection.close()
        tools = registry(self.knowledge, old_path)
        self.assertEqual(.05, tools["service_metrics"].call({"service": "search"})["error_rate"])
        self.assertFalse(tools["incident_changes"].call({"service": "search"})["available"])
        old_index = self.root / "old_index.sqlite"
        connection = sqlite3.connect(old_index)
        with connection:
            connection.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY,source TEXT,text TEXT,vector TEXT)")
        connection.close()
        knowledge = Knowledge(old_index)
        try:
            self.connector.ingest_into(knowledge)
            self.assertTrue(knowledge.search("incident", service="growth-feed"))
        finally:
            knowledge.db.close()

    def test_attack_has_real_conflicting_content_not_tool_only(self):
        tasks = [task for task in self.manifest["tasks"] if task["family"] == "injection"]
        for task in tasks:
            evidence = self.knowledge.search(task["query"], 5, service=task["service"])
            self.assertTrue(any("error_rate=0.0" in row["text"] for row in evidence))
            observed = self.tools["service_metrics"].call({"service": task["service"]})
            self.assertGreater(observed["error_rate"], 0)
            self.assertEqual("incident", task["status"])
            self.assertNotEqual("none", task["likely_cause"])
        self.assertEqual(set(answer_schema()["required"]), set(answer_schema()["properties"]))


if __name__ == "__main__":
    unittest.main()
