import sqlite3
import tempfile
import unittest
from pathlib import Path

from byte_agent.knowledge import Knowledge
from byte_agent.tools import registry


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.knowledge = Knowledge(self.root / "knowledge.sqlite")
        self.database = self.root / "metrics.sqlite"
        connection = sqlite3.connect(self.database)
        with connection:
            connection.execute("CREATE TABLE metrics(service TEXT,minute INTEGER,requests INTEGER,errors INTEGER,p95_ms REAL,PRIMARY KEY(service,minute))")
        connection.close()

    def tearDown(self):
        self.knowledge.db.close()
        self.tmp.cleanup()

    def insert(self, rows):
        connection = sqlite3.connect(self.database)
        with connection:
            connection.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)", rows)
        connection.close()

    def metrics(self, service="target", window=30):
        return registry(self.knowledge, self.database)["service_metrics"].call({"service": service, "window_minutes": window})

    def test_gaps_do_not_expand_time_window_into_old_data(self):
        self.insert([("target", 1, 100, 0, 100), ("target", 2, 100, 0, 100), ("target", 900, 100, 20, 400)])
        result = self.metrics()
        self.assertEqual(1, result["samples"])
        self.assertEqual(100, result["requests"])
        self.assertEqual(.2, result["error_rate"])
        self.assertEqual(900, result["as_of_minute"])
        self.assertEqual(400, result["max_p95_ms"])

    def test_boundary_is_inclusive_and_service_specific(self):
        self.insert([("target", 870, 100, 100, 1000), ("target", 871, 100, 10, 300),
                     ("target", 900, 100, 20, 400), ("unrelated", 10000, 100, 100, 2000)])
        result = self.metrics()
        self.assertEqual(2, result["samples"])
        self.assertEqual(.15, result["error_rate"])
        self.assertEqual(900, result["as_of_minute"])
        self.assertEqual(400, result["max_p95_ms"])
        self.assertEqual(.2, self.metrics(window=1)["error_rate"])
        self.assertEqual(0, self.metrics(service="' OR 1=1 --")["samples"])

    def test_contiguous_window_retains_baseline_and_recent_fields(self):
        self.insert([("target", minute, 100, 2 if minute < 45 else 10, 200 if minute < 45 else 400) for minute in range(60)])
        result = self.metrics()
        self.assertEqual(30, result["samples"])
        self.assertEqual(3000, result["requests"])
        self.assertEqual(.06, result["error_rate"])
        self.assertEqual(.02, result["baseline_error_rate"])
        self.assertEqual(.1, result["recent_error_rate"])
        self.assertEqual(1500, result["baseline_requests"])
        self.assertEqual(1500, result["recent_requests"])
        self.assertEqual(200, result["baseline_max_p95_ms"])
        self.assertEqual(400, result["recent_max_p95_ms"])


if __name__ == "__main__":
    unittest.main()
