import importlib.util
import io
import json
import tempfile
import time
import unittest
from unittest import mock
from contextlib import closing
from pathlib import Path

from byte_agent.knowledge import Knowledge
from byte_agent.model import Scripted
from byte_agent.multiagent import Coordinator, ROLES, RoleOllama, TeamBudget, _DeadlineModel, _operator_tool, _sha, _snapshot_tool, role_answer_schema
from byte_agent.runtime import Runtime
from byte_agent.tools import registry

spec = importlib.util.spec_from_file_location("multiagent_example", Path(__file__).resolve().parent.parent / "examples" / "multiagent_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class MultiAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        knowledge_root, metrics = demo.sources(self.root / "data")
        self.knowledge = Knowledge(self.root / "knowledge.sqlite")
        self.knowledge.ingest(knowledge_root)
        self.tools = registry(self.knowledge, metrics)
        self.models = demo.fixture_models(self.tools)

    def tearDown(self):
        self.knowledge.db.close()
        self.temporary.cleanup()

    def team(self, **kwargs):
        return Coordinator(self.root / "team", self.models, self.tools, demo.SERVICE, **kwargs)

    def test_four_isolated_model_runs_and_real_tool_permissions(self):
        result = self.team().run(demo.TASK)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["fixture"])
        self.assertEqual(set(result["roles"]), set(ROLES))
        self.assertEqual(result["usage"], {"steps": 8, "calls": 5, "tokens": 0, "unknown_usage": 8})
        expected = {"metrics": {"service_metrics"}, "knowledge": {"knowledge_search", "incident_changes"},
                    "reviewer": {"read_shared_evidence"}, "arbiter": {"read_shared_evidence"}}
        for role, entry in result["roles"].items():
            self.assertTrue((self.root / "team" / "agents" / role / "journal.sqlite").is_file())
            self.assertEqual({event["name"] for event in entry["trace"]["events"] if event["kind"] == "tool"}, expected[role])
            self.assertEqual(sum(event["kind"] == "model" for event in entry["trace"]["events"]), 2)
        metric = result["roles"]["metrics"]["trace"]
        self.assertEqual(metric["events"][1]["arguments"]["window_minutes"], 30)
        self.assertNotIn("window_minutes", metric["messages"][2]["tool_calls"][0]["function"]["arguments"])
        knowledge = result["roles"]["knowledge"]["trace"]
        tool_events = [event for event in knowledge["events"] if event["kind"] == "tool"]
        self.assertEqual([event["arguments"]["limit"] for event in tool_events], [3, 20])
        for call in knowledge["messages"][2]["tool_calls"]:
            self.assertNotIn("limit", call["function"]["arguments"])
        for role in ("reviewer", "arbiter"):
            trace = result["roles"][role]["trace"]
            self.assertEqual(trace["messages"][2]["tool_calls"][0]["function"]["arguments"], {})
            self.assertEqual(trace["events"][1]["arguments"]["snapshot_sha256"], result["roles"][role]["snapshot_sha256"])
        self.assertEqual(json.loads(result["answer"])["error_rate"], 0.023)
        self.assertEqual(len(result["snapshots"]), 2)

    def test_resume_does_not_reissue_provider_calls(self):
        team = self.team()
        first = team.run(demo.TASK)
        for model in self.models.values():
            model.complete = lambda *_: (_ for _ in ()).throw(AssertionError("completed runs cannot replay provider calls"))
        second = team.run(demo.TASK)
        self.assertEqual(first["roles"], second["roles"])
        self.assertEqual(first["answer"], second["answer"])

    def test_resume_after_role_commit_before_coordinator_commit(self):
        # Simulate the narrow crash boundary using a completed role's durable journal.
        first = self.team().run(demo.TASK)
        import sqlite3
        database = self.root / "team" / "coordinator.sqlite"
        with closing(sqlite3.connect(database)) as db:
            state = json.loads(db.execute("SELECT body FROM state").fetchone()[0])
            state.update(status="running", roles={}, snapshots=[], conflicts=[], answer="")
            db.execute("UPDATE state SET body=?", (json.dumps(state),))
            db.commit()
        for model in self.models.values():
            model.complete = lambda *_: (_ for _ in ()).throw(AssertionError("durable role cannot replay provider calls"))
        second = self.team().run(demo.TASK)
        self.assertEqual(second["status"], "completed")
        self.assertEqual(first["roles"], second["roles"])

    def test_service_scope_stops_before_cross_service_source_access(self):
        self.models["metrics"] = Scripted([
            {"calls": [{"name": "service_metrics", "arguments": {"service": "other-service"}}]},
            {"answer": json.dumps({"service": demo.SERVICE, "summary": "No observation.", "hypothesis": "insufficient_evidence", "citations": []})}])
        result = self.team().run(demo.TASK)
        self.assertEqual(result["status"], "role_failed")
        self.assertEqual(set(result["roles"]), {"metrics"})
        trace = result["roles"]["metrics"]["trace"]
        self.assertEqual(trace["status"], "validation_failed")
        self.assertEqual(trace["events"][1]["error"], "ValueError")
        self.assertEqual(result["answer"], "")

    def test_global_call_budget_stops_before_reviewer(self):
        result = self.team(budget=TeamBudget(calls=3)).run(demo.TASK)
        self.assertEqual(result["status"], "budget_exceeded")
        self.assertEqual(set(result["roles"]), {"metrics", "knowledge"})
        self.assertEqual(result["usage"]["calls"], 3)

    def test_per_role_steps_bound_stops_without_degraded_success(self):
        result = self.team(budget=TeamBudget(role_steps=1)).run(demo.TASK)
        self.assertEqual(result["status"], "role_failed")
        self.assertEqual(result["roles"]["metrics"]["trace"]["status"], "budget_exceeded")
        self.assertEqual(result["answer"], "")

    def test_provider_failure_is_uncertain_without_retry(self):
        def failing(*_):
            raise TimeoutError("lost model response")
        self.models["metrics"].complete = failing
        result = self.team().run(demo.TASK)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(result["usage"]["unknown_usage"], 1)
        self.assertEqual(set(result["roles"]), {"metrics"})
        again = self.team().run(demo.TASK)
        self.assertEqual(result["roles"], again["roles"])

    def test_cancellation_between_roles(self):
        result = self.team().run(demo.TASK, is_cancelled=lambda: (self.root / "team" / "agents" / "metrics" / "trace.json").exists())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(set(result["roles"]), {"metrics"})
        self.assertEqual(result["answer"], "")

    def test_cooperative_deadline_stops_subsequent_roles(self):
        previous = self.models["metrics"].complete
        clock = [0.0]
        def slow(*args):
            decision = previous(*args)
            clock[0] = 2.0  # A returned provider response crosses the safe deadline edge.
            return decision
        self.models["metrics"].complete = slow
        with mock.patch("byte_agent.multiagent.time.monotonic", side_effect=lambda: clock[0]):
            result = self.team(budget=TeamBudget(wall_seconds=1.0)).run(demo.TASK)
        self.assertEqual(result["status"], "timed_out")
        self.assertNotIn("knowledge", result["roles"])

    def test_review_disagreement_requires_conservative_final_and_repair(self):
        review = json.loads(self.models["reviewer"].decisions[1]["answer"])
        review.update(verdict="disagree", reason="Independent review questions the proposed causal interpretation; verify observations before intervention.")
        self.models["reviewer"].decisions[1]["answer"] = json.dumps(review)
        original = json.loads(self.models["arbiter"].decisions[1]["answer"])
        original.update(likely_cause="conflicting_evidence", recommendation="verify_changes", uncertainty="conflicting_evidence")
        self.models["arbiter"].decisions.append({"answer": json.dumps(original)})
        result = self.team().run(demo.TASK)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(json.loads(result["answer"])["recommendation"], "verify_changes")
        validations = [event["valid"] for event in result["roles"]["arbiter"]["trace"]["events"] if event["kind"] == "validation"]
        self.assertEqual(validations, [False, True])

    def test_shared_snapshot_returns_copies_not_mutable_shared_memory(self):
        snapshot = {"nested": {"value": 1}}
        tools = _snapshot_tool(snapshot, "a" * 64)
        first = tools["read_shared_evidence"].call({})
        first["snapshot"]["nested"]["value"] = 9
        snapshot["nested"]["value"] = 7
        second = tools["read_shared_evidence"].call({})
        self.assertEqual(second["snapshot"]["nested"]["value"], 1)

    def test_tampered_snapshot_invalidates_completed_run(self):
        result = self.team().run(demo.TASK)
        path = self.root / "team" / "shared" / (result["snapshots"][0] + ".json")
        path.write_text("{}", encoding="utf-8")
        result = self.team().run(demo.TASK)
        self.assertEqual(result["status"], "evidence_error")
        self.assertEqual(result["answer"], "")

    def test_identity_and_distinct_adapter_constraints(self):
        self.team().run(demo.TASK)
        with self.assertRaises(ValueError):
            self.team().run("A different task")
        with self.assertRaises(ValueError):
            Coordinator(self.root / "other", {role: self.models["metrics"] for role in ROLES}, self.tools, demo.SERVICE)
        with self.assertRaises(ValueError):
            TeamBudget(tokens=True)
        with self.assertRaises(ValueError):
            TeamBudget(wall_seconds=float("nan"))

    def test_evidence_bound_rejects_oversized_snapshot(self):
        with self.assertRaises(ValueError):
            _snapshot_tool({"text": "x" * 24000}, "a" * 64)

    def test_removed_parameters_are_rejected_before_operator_binding(self):
        metric = _operator_tool(self.tools["service_metrics"], {"window_minutes": 30})
        search = _operator_tool(self.tools["knowledge_search"], {"limit": 3}, ("source",))
        changes = _operator_tool(self.tools["incident_changes"], {"limit": 20})
        shared = _snapshot_tool({"service": demo.SERVICE}, "a" * 64)["read_shared_evidence"]
        for tool, args in [(metric, {"service": demo.SERVICE, "window_minutes": 30}),
                           (search, {"query": "policy", "service": demo.SERVICE, "limit": -1}),
                           (search, {"query": "policy", "service": demo.SERVICE, "source": "foreign/runbook.md"}),
                           (changes, {"service": demo.SERVICE, "limit": 20}),
                           (shared, {"snapshot_sha256": "a" * 64})]:
            with self.subTest(tool=tool.name, args=args), self.assertRaises(ValueError):
                tool.call(args)
        self.assertNotIn("window_minutes", metric.spec()["inputSchema"]["properties"])
        self.assertNotIn("limit", search.spec()["inputSchema"]["properties"])
        self.assertNotIn("source", search.spec()["inputSchema"]["properties"])
        self.assertEqual(shared.spec()["inputSchema"]["properties"], {})

    def test_operator_configuration_is_bound_to_tool_and_team_identity(self):
        first = _operator_tool(self.tools["knowledge_search"], {"limit": 3}, ("source",))
        second = _operator_tool(self.tools["knowledge_search"], {"limit": 4}, ("source",))
        self.assertEqual(first.spec()["inputSchema"], second.spec()["inputSchema"])
        self.assertNotEqual(first.revision, second.revision)
        team = self.team()
        team.run(demo.TASK)
        team.role_policy["knowledge"]["knowledge_search"]["limit"] = 4
        with self.assertRaises(ValueError):
            team.run(demo.TASK)

    def test_pending_unbound_request_replays_to_durable_effective_arguments(self):
        import sqlite3
        model = Scripted([{"calls": [{"name": "service_metrics", "arguments": {"service": demo.SERVICE}}]}, {"answer": "done"}])
        tools = {"service_metrics": _operator_tool(self.tools["service_metrics"], {"window_minutes": 30})}
        directory = self.root / "pending-role"
        runtime = Runtime(directory, _DeadlineModel(model, time.monotonic() + 30), tools)
        with self.assertRaises(RuntimeError):
            runtime.run("task", crash_after_model=True)
        with closing(sqlite3.connect(directory / "journal.sqlite")) as db:
            pending = json.loads(db.execute("SELECT body FROM state").fetchone()[0])["pending"]
        self.assertEqual(pending[0]["arguments"], {"service": demo.SERVICE})
        trace = runtime.run("task")
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(trace["events"][1]["arguments"], {"service": demo.SERVICE, "window_minutes": 30})
        self.assertEqual(trace["messages"][2]["tool_calls"][0]["function"]["arguments"], {"service": demo.SERVICE})
        with closing(sqlite3.connect(directory / "journal.sqlite")) as db:
            stored = json.loads(db.execute("SELECT body FROM state").fetchone()[0])
        self.assertEqual(stored["events"][1]["arguments"]["window_minutes"], 30)
        self.assertEqual(stored["pending"], [])

    def test_metrics_capability_does_not_establish_cause(self):
        proposal = json.loads(self.models["metrics"].decisions[1]["answer"])
        proposal["hypothesis"] = "capacity_pressure"
        self.models["metrics"].decisions[1]["answer"] = json.dumps(proposal)
        result = self.team(budget=TeamBudget(role_steps=2)).run(demo.TASK)
        self.assertEqual(result["status"], "role_failed")
        validations = [event for event in result["roles"]["metrics"]["trace"]["events"] if event["kind"] == "validation"]
        self.assertIn("metrics_has_no_causal_evidence_requires_insufficient_evidence", {e["code"] for e in validations[0]["errors"]})

    def test_structured_output_does_not_make_fabricated_citations_valid(self):
        proposal = json.loads(self.models["knowledge"].decisions[1]["answer"])
        proposal["citations"] = ["invented-but-schema-compatible"]
        self.models["knowledge"].decisions[1]["answer"] = json.dumps(proposal)
        result = self.team(budget=TeamBudget(role_steps=2)).run(demo.TASK)
        self.assertEqual(result["status"], "role_failed")
        validations = [event for event in result["roles"]["knowledge"]["trace"]["events"] if event["kind"] == "validation"]
        self.assertIn("specialist_citations_not_observed", {e["code"] for e in validations[0]["errors"]})


class StructuredRoleTests(unittest.TestCase):
    def model(self, role):
        model = RoleOllama(role, demo.SERVICE, "qwen3:4b-instruct")
        class Transport:
            def __init__(self):
                self.bodies = []
            def open(self, request, timeout):
                self.bodies.append(json.loads(request.data))
                response = {"message": {"role": "assistant", "content": "{}"}, "prompt_eval_count": 11,
                            "eval_count": 7, "done_reason": "stop", "total_duration": 19}
                return io.BytesIO(json.dumps(response).encode())
        model.transport = Transport()
        return model

    @staticmethod
    def observation(name, value):
        return {"role": "tool", "tool_name": name, "content": json.dumps(value)}

    def test_native_tools_continue_until_all_required_successful_observations(self):
        model = self.model("knowledge")
        specs = [{"name": "incident_changes", "description": "changes", "inputSchema": {"type": "object"}}]
        search = self.observation("knowledge_search", {"service": demo.SERVICE, "evidence": []})
        failed = self.observation("incident_changes", {"error": "invalid args"})
        wrong = self.observation("incident_changes", {"service": "different", "changes": []})
        for messages in ([], [search], [search, failed], [search, wrong]):
            result = model.complete(messages, specs)
            body = model.transport.bodies[-1]
            self.assertNotIn("format", body)
            self.assertTrue(body["tools"])
            self.assertEqual(result["timing"]["response_mode"], "native_tools")
        changes = self.observation("incident_changes", {"service": demo.SERVICE, "changes": []})
        result = model.complete([search, changes], specs)
        body = model.transport.bodies[-1]
        self.assertEqual(body["tools"], [])
        self.assertEqual(body["format"], role_answer_schema("knowledge", demo.SERVICE))
        self.assertEqual(result["timing"]["response_mode"], "json_schema")
        self.assertEqual(result["usage"], {"input": 11, "output": 7})
        self.assertEqual(result["timing"]["total_duration"], 19)
        self.assertEqual(result["timing"]["structured_schema_sha256"], _sha(body["format"]))

    def test_metrics_and_snapshot_phase_require_actual_operator_observations(self):
        metric = self.model("metrics")
        self.assertFalse(metric._ready([self.observation("service_metrics", {"service": demo.SERVICE, "window_minutes": 15})]))
        self.assertTrue(metric._ready([self.observation("service_metrics", {"service": demo.SERVICE, "window_minutes": 30})]))
        reviewer = self.model("reviewer")
        snapshot = {"service": demo.SERVICE, "observations": []}
        self.assertFalse(reviewer._ready([self.observation("read_shared_evidence", {"snapshot": snapshot, "snapshot_sha256": "a" * 64})]))
        self.assertTrue(reviewer._ready([self.observation("read_shared_evidence", {"snapshot": snapshot, "snapshot_sha256": _sha(snapshot)})]))
        self.assertFalse(reviewer._ready([{"role": "assistant", "tool_name": "read_shared_evidence", "content": json.dumps({"snapshot": snapshot, "snapshot_sha256": _sha(snapshot)})}]))

    def test_schema_has_no_observed_values_or_citation_enumeration(self):
        for role in ROLES:
            schema = role_answer_schema(role, demo.SERVICE)
            props = schema["properties"]
            self.assertEqual(props["service"]["enum"], [demo.SERVICE])
            self.assertNotIn("enum", props["citations"]["items"])
            self.assertNotIn("demo-v3-release-44", json.dumps(schema))
            self.assertNotIn("0.023", json.dumps(schema))
        self.assertEqual(role_answer_schema("metrics", demo.SERVICE)["properties"]["hypothesis"]["enum"], ["insufficient_evidence"])
        first = self.model("metrics")
        other = RoleOllama("metrics", "another-service", "qwen3:4b-instruct")
        self.assertEqual(first.config["structured_final_schema_sha256"], _sha(first.final_schema))
        self.assertNotEqual(first.config["structured_final_schema_sha256"], other.config["structured_final_schema_sha256"])
        self.assertNotEqual(first.config["structured_final_schema_sha256"], self.model("knowledge").config["structured_final_schema_sha256"])


if __name__ == "__main__":
    unittest.main()
