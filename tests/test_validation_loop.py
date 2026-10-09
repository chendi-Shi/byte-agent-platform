import tempfile
import unittest
from byte_agent.model import Scripted
from byte_agent.runtime import Runtime


def validator(trace):
    return {"valid": trace["answer"] == "valid", "errors": [], "feedback": "Required contract: valid"}
validator.revision = "bounded-contract-v1"


class ValidationLoopTests(unittest.TestCase):
    def test_repairs_output_and_records_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Scripted([{"answer": "invalid"}, {"answer": "valid"}])
            runtime = Runtime(folder, model, {}, answer_validator=validator)
            result = runtime.run("task")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"], 2)
            self.assertEqual([v["valid"] for v in result["events"] if v["kind"] == "validation"], [False, True])
            self.assertEqual(result, runtime.run("task"))

    def test_repairs_consume_normal_step_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Runtime(folder, Scripted([{"answer": "invalid"}]), {}, max_steps=1, answer_validator=validator).run("task")
            self.assertEqual(result["status"], "budget_exceeded")
            self.assertEqual(result["steps"], 1)

    def test_validator_failure_is_exported(self):
        def broken(trace):
            raise ValueError("broken validator")
        broken.revision = "broken-v1"
        with tempfile.TemporaryDirectory() as folder:
            result = Runtime(folder, Scripted([{"answer": "answer"}]), {}, answer_validator=broken).run("task")
            self.assertEqual(result["status"], "validation_error")
            self.assertEqual(result["unknown_usage"], 1)  # Fixture usage is explicitly unknown.
            self.assertEqual(result["events"][-1]["kind"], "validation_error")

    def test_validator_identity_is_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Scripted([{"answer": "valid"}])
            Runtime(folder, model, {}, answer_validator=validator).run("task")
            def changed(trace):
                return {"valid": True, "feedback": ""}
            changed.revision = "bounded-contract-v2"
            with self.assertRaises(ValueError):
                Runtime(folder, model, {}, answer_validator=changed).run("task")

    def test_irreversible_violation_stops_without_another_provider_request(self):
        def terminal(trace):
            return {"valid": False, "repairable": False, "errors": [{"code": "wrong_service_history"}], "feedback": "Access cannot be undone."}
        terminal.revision = "terminal-v1"
        with tempfile.TemporaryDirectory() as folder:
            result = Runtime(folder, Scripted([{"answer": "invalid"}]), {}, answer_validator=terminal).run("task")
            self.assertEqual(result["status"], "validation_failed")
            self.assertEqual(result["steps"], 1)


if __name__ == "__main__":
    unittest.main()
