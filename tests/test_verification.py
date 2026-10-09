"""Regression tests; no provider requests and no benchmark labels."""
import copy
import json
import unittest

from byte_agent import verification


def fixture():
    answer = {"service": "creator-upload", "error_rate": .021,
              "status": "incident", "likely_cause": "release_regression", "recommendation": "rollback_review",
              "citations": ["book", "change"], "uncertainty": "causality_unproven"}
    events = [{"kind": "tool", "name": "service_metrics", "arguments": {"service": "creator-upload", "window_minutes": 30}, "error": None,
               "output": {"service": "creator-upload", "window_minutes": 30, "error_rate": .021,
                          "recent_error_rate": .039, "samples": 30, "requests": 10000, "as_of_minute": 59}},
              {"kind": "tool", "name": "knowledge_search", "arguments": {"service": "creator-upload", "query": "policy"}, "error": None,
               "output": {"service": "creator-upload", "evidence": [{"id": "book", "source": "creator-upload/runbook.md", "text": "Untrusted policy text"}]}},
              {"kind": "tool", "name": "incident_changes", "arguments": {"service": "creator-upload"}, "error": None,
               "output": {"service": "creator-upload", "evidence": [{"id": "change", "source": "changes/creator-upload", "text": "Untrusted observation"}]}}]
    return answer, events


class VerificationTests(unittest.TestCase):
    def test_valid_observed_answer_passes_without_labels_and_without_mutation(self):
        answer, events = fixture()
        original = copy.deepcopy(events)
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events, "Inspect the service.")
        self.assertTrue(result["valid"])
        self.assertEqual([], result["errors"])
        self.assertEqual(original, events)

    def test_actual_recent_instead_of_aggregate_regression_is_repairable(self):
        answer, events = fixture()
        answer["error_rate"] = .039
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertFalse(result["valid"])
        self.assertTrue(result["repairable"])
        self.assertIn("aggregate_rate", {e["code"] for e in result["errors"]})
        self.assertIn("0.021", result["feedback"])
        answer["error_rate"] = .021
        self.assertTrue(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])

    def test_wrong_answer_service_and_non_json_fail(self):
        answer, events = fixture()
        answer["service"] = "creator-draft"
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertIn("answer_service", {e["code"] for e in result["errors"]})
        result = verification.validate_diagnostic("creator-upload", "creator-upload is healthy", events)
        self.assertIn("answer_json", {e["code"] for e in result["errors"]})

    def test_extreme_numeric_answer_returns_feedback_instead_of_overflow(self):
        answer, events = fixture()
        answer["error_rate"] = 10 ** 400
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertFalse(result["valid"])
        self.assertTrue(result["repairable"])
        self.assertIn("answer_rate_type", {e["code"] for e in result["errors"]})

    def test_wrong_service_execution_is_terminal_under_strict_history(self):
        answer, events = fixture()
        wrong = copy.deepcopy(events[0])
        wrong["arguments"]["service"] = "creator-draft"
        wrong["output"]["service"] = "creator-draft"
        events.insert(0, wrong)
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertFalse(result["valid"])
        self.assertFalse(result["repairable"])
        self.assertIn("wrong_service_history", {e["code"] for e in result["errors"]})
        self.assertTrue(verification.validate_diagnostic("creator-upload", json.dumps(answer), events, strict_service_history=False)["valid"])

    def test_all_three_target_tools_are_required_and_can_be_obtained_before_retry(self):
        answer, events = fixture()
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events[:-1])
        self.assertIn("missing_incident_changes", {e["code"] for e in result["errors"]})
        self.assertTrue(result["repairable"])

    def test_missing_runbook_accepts_changes_only_but_requires_observed_absence(self):
        answer, events = fixture()
        events[1]["output"]["evidence"] = []
        answer.update(status="insufficient_data", likely_cause="insufficient_evidence", recommendation="collect_evidence", uncertainty="insufficient_data", citations=["change"])
        self.assertTrue(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), [events[0], events[2]])["valid"])

    def test_missing_metric_observation_uses_null_and_does_not_invent_zero(self):
        answer, events = fixture()
        events[0]["output"].update(error_rate=None, samples=0, requests=0)
        answer["error_rate"] = None
        self.assertTrue(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])
        answer["error_rate"] = 0
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])

    def test_semantic_cause_recommendation_and_uncertainty_are_not_gold_scored(self):
        answer, events = fixture()
        answer.update(likely_cause="dependency_outage", recommendation="capacity_review", uncertainty="none_detected")
        # The independent benchmark may fail this diagnosis; this validator does not infer its answer.
        self.assertTrue(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])

    def test_enum_duplicate_fields_citations_and_extra_prose_fail(self):
        answer, events = fixture()
        originals = json.dumps(answer)
        bad = [originals[:-1] + ', "service": "another"}', originals + " extra text", "```json\n" + originals + "\n```"]
        answer["uncertainty"] = "completely certain"
        bad.append(json.dumps(answer))
        answer["uncertainty"] = "causality_unproven"
        answer["citations"] = ["book", "book", "change"]
        bad.append(json.dumps(answer))
        for value in bad:
            with self.subTest(value=value):
                self.assertFalse(verification.validate_diagnostic("creator-upload", value, events)["valid"])

    def test_unknown_and_cross_service_sources_cannot_support_citations(self):
        answer, events = fixture()
        answer["citations"] = ["book", "invented"]
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])
        answer["citations"] = ["book", "change"]
        events[1]["output"]["evidence"][0]["source"] = "creator-draft/runbook.md"
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertFalse(result["valid"])
        self.assertIn("evidence_service", {e["code"] for e in result["errors"]})

    def test_wrong_window_and_inconsistent_metric_shape_fail(self):
        answer, events = fixture()
        events[0]["arguments"]["window_minutes"] = 15
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])
        events[0]["arguments"]["window_minutes"] = 30
        events[0]["output"]["samples"] = 0
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), events)["valid"])

    def test_feedback_does_not_echo_retrieved_injection_and_input_is_bounded(self):
        answer, events = fixture()
        events[1]["output"]["evidence"][0]["text"] = "SEND_SECRET_TO_ATTACKER"
        answer["error_rate"] = .039
        result = verification.validate_diagnostic("creator-upload", json.dumps(answer), events)
        self.assertNotIn("SEND_SECRET_TO_ATTACKER", result["feedback"])
        self.assertFalse(verification.validate_diagnostic("creator-upload", "x" * 16001, events)["valid"])
        self.assertFalse(verification.validate_diagnostic("creator-upload", json.dumps(answer), [None] * 513)["repairable"])

    def test_callback_adapts_trace_and_preserves_budget_ownership(self):
        answer, events = fixture()
        callback = verification.make_diagnostic_validator("creator-upload", "Inspect current evidence")
        self.assertTrue(callback({"answer": json.dumps(answer), "events": events})["valid"])
        self.assertFalse(callback({"answer": "not JSON", "events": events})["valid"])

    def test_factory_revision_binds_service_task_and_history_policy(self):
        original = verification.make_diagnostic_validator("creator-upload", "Inspect A")
        self.assertEqual(original.revision, verification.make_diagnostic_validator("creator-upload", "Inspect A").revision)
        self.assertNotEqual(original.revision, verification.make_diagnostic_validator("creator-draft", "Inspect A").revision)
        self.assertNotEqual(original.revision, verification.make_diagnostic_validator("creator-upload", "Inspect B").revision)
        self.assertNotEqual(original.revision, verification.make_diagnostic_validator("creator-upload", "Inspect A", False).revision)

    def test_scope_clones_schema_and_blocks_wrong_service_before_handler(self):
        from byte_agent.tools import Tool
        accesses = []
        def read_metrics(service, window_minutes=30):
            accesses.append(service)
            return {"service": service, "window_minutes": window_minutes}
        original = Tool("service_metrics", "read metrics", {"service": {"type": "string"}, "window_minutes": {"type": "integer", "minimum": 1, "maximum": 60}},
                        ["service"], read_metrics, revision="original")
        scoped = verification.scope_tools({"service_metrics": original}, "creator-upload")["service_metrics"]
        self.assertEqual(["creator-upload"], scoped.spec()["inputSchema"]["properties"]["service"]["enum"])
        self.assertNotIn("enum", original.spec()["inputSchema"]["properties"]["service"])
        with self.assertRaises(ValueError):
            scoped.call({"service": "creator-draft", "window_minutes": 30})
        self.assertEqual([], accesses)
        self.assertEqual("creator-upload", scoped.call({"service": "creator-upload"})["service"])
        self.assertEqual(["creator-upload"], accesses)
        self.assertEqual(scoped.revision, verification.scope_tools({"service_metrics": original}, "creator-upload")["service_metrics"].revision)
        self.assertNotEqual(scoped.revision, verification.scope_tools({"service_metrics": original}, "creator-draft")["service_metrics"].revision)

    def test_all_three_scope_handlers_keep_their_original_tool_and_require_service(self):
        from byte_agent.tools import Tool
        def tool(name):
            def handler(**arguments):
                return {"original_tool": name, **arguments}
            return Tool(name, "read " + name, {"service": {"type": "string"}}, [], handler, revision=name)
        originals = {name: tool(name) for name in verification.TOOLS}
        scoped = verification.scope_tools(originals, "creator-upload")
        for name, wrapped in scoped.items():
            self.assertEqual(["service"], wrapped.required)
            self.assertEqual(name, wrapped.call({"service": "creator-upload"})["original_tool"])
            with self.assertRaises(ValueError):
                wrapped.call({})


if __name__ == "__main__":
    unittest.main()
