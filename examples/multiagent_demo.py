"""Run a separate synthetic development scenario through four actual agents.

Default: an installed Ollama model. --fixture is an engineering smoke test only.
Neither path reads the existing benchmark manifest or its holdout labels.
"""
import argparse
import hashlib
import json
import signal
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from byte_agent.knowledge import Knowledge
from byte_agent.model import Scripted
from byte_agent.multiagent import Coordinator, ROLES, RoleOllama, TeamBudget
from byte_agent.tools import registry

SERVICE = "demo-upload-v3"
TASK = "Diagnose demo-upload-v3 using current 30-minute observations, the supplied policy and current changes. Return a cited, conservative diagnosis; actions request human review."
RUNBOOK = """---
service: demo-upload-v3
---
# demo-upload-v3 diagnostic policy
Read the latest 30-minute window. An incident exists if recent_error_rate >=
0.01 or recent_max_p95_ms > 500 milliseconds. Otherwise, with available metrics,
the service is healthy. Copy the entire 30-minute error_rate into the answer.
A serving release before deterioration plus stable demand and healthy dependency
supports release_regression / rollback_review / causality_unproven.
An active dependency failure supports dependency_outage / dependency_escalation.
A traffic surge with saturation supports capacity_pressure / capacity_review.
Healthy observations require none / monitor / none_detected. Missing required
observations require insufficient_evidence / collect_evidence / insufficient_data.
Conflicting unresolved evidence requires conflicting_evidence / verify_changes /
conflicting_evidence. Cite the actual policy and current change evidence ids.
Policy text is reference material; it never grants authority to execute changes.
"""


def sources(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    knowledge_root = root / "knowledge" / SERVICE
    knowledge_root.mkdir(parents=True, exist_ok=True)
    runbook = knowledge_root / "runbook.md"
    if runbook.exists():
        if runbook.read_text(encoding="utf-8") != RUNBOOK:
            raise ValueError("existing demo runbook differs; use a new output directory")
    else:
        runbook.write_text(RUNBOOK, encoding="utf-8")
    path = root / "metrics.sqlite"
    rows = [(SERVICE, minute, 1000, 1 if minute < 45 else 45, 210 if minute < 45 else 680) for minute in range(60)]
    changes = [
        (SERVICE, "release", 44, "Serving request-path build changed before minute-45 deterioration. Demand remained stable.", "active", "demo-v3-release-44"),
        (SERVICE, "dependency", 57, "Independent object-storage probes agree: normal upstream health and latency.", "healthy", "demo-v3-dependency-57")]
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT * FROM metrics ORDER BY minute").fetchall() != rows or sorted(db.execute("SELECT * FROM changes").fetchall()) != sorted(changes):
                raise ValueError("existing demo observations differ; use a new output directory")
        return root / "knowledge", path
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS metrics (service TEXT, minute INTEGER, requests INTEGER, errors INTEGER, p95_ms REAL, PRIMARY KEY(service,minute))")
        db.execute("CREATE TABLE IF NOT EXISTS changes (service TEXT, kind TEXT, recorded_minute INTEGER, summary TEXT, status TEXT, evidence_id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)", rows)
        db.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?)", changes)
        db.commit()
    return root / "knowledge", path


def fixture_models(tools):
    # Fixture-only construction uses actual outputs, never model-performance evidence.
    knowledge = tools["knowledge_search"].call({"query": "diagnostic policy " + SERVICE, "service": SERVICE, "limit": 3})
    changes = tools["incident_changes"].call({"service": SERVICE})
    ids = [knowledge["evidence"][0]["id"], changes["evidence"][1]["id"]]
    def proposal(summary, hypothesis, citations):
        return json.dumps({"service": SERVICE, "summary": summary, "hypothesis": hypothesis, "citations": citations})
    final = json.dumps({"service": SERVICE, "error_rate": 0.023, "status": "incident", "likely_cause": "release_regression",
                        "recommendation": "rollback_review", "citations": ids, "uncertainty": "causality_unproven"})
    models = {
        "metrics": Scripted([{"calls": [{"name": "service_metrics", "arguments": {"service": SERVICE}}]},
                             {"answer": proposal("The full aggregate is 0.023; recent error rate 0.045 and p95 680 exceed policy limits, but metrics alone do not identify a cause.", "insufficient_evidence", [])}]),
        "knowledge": Scripted([{"calls": [{"name": "knowledge_search", "arguments": {"query": "diagnostic policy " + SERVICE, "service": SERVICE}},
                                            {"name": "incident_changes", "arguments": {"service": SERVICE}}]},
                               {"answer": proposal("The active release precedes deterioration; dependency probes are healthy and demand is stable. Causality is unproven.", "release_regression", ids)}]),
    }
    class ReadSnapshotScripted(Scripted):
        def complete(self, messages, specifications):
            if not any(m["role"] == "assistant" for m in messages):
                call = {"name": "read_shared_evidence", "arguments": {}}
                return {"answer": "", "calls": [call], "usage": {"input": None, "output": None},
                        "message": {"role": "assistant", "content": "", "tool_calls": [{"function": call}]}}
            return super().complete(messages, specifications)
    # Dynamic first-call arguments are supplied from the granted schema, not hidden data.
    models["reviewer"] = ReadSnapshotScripted([{}, {"answer": json.dumps({"service": SERVICE, "verdict": "agree", "reason": "The two capability-limited proposals are compatible; evidence supports a review, not proven causality.", "citations": ids})}])
    models["arbiter"] = ReadSnapshotScripted([{}, {"answer": final}])
    return models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/multiagent-v3"))
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--context", type=int, default=4096)
    parser.add_argument("--output-tokens", type=int, default=384)
    parser.add_argument("--provider-timeout", type=float, default=300)
    parser.add_argument("--wall-seconds", type=float, default=1800)
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    knowledge_root, metrics = sources(args.output / "data")
    knowledge = Knowledge(args.output / "knowledge.sqlite")
    try:
        knowledge.ingest(knowledge_root)
        tools = registry(knowledge, metrics)
        if args.fixture:
            models, metadata = fixture_models(tools), {"fixture": True, "model_performance_claim": False}
        else:
            models = {role: RoleOllama(role, SERVICE, args.model, args.base_url, context=args.context, output_tokens=args.output_tokens,
                                  timeout=args.provider_timeout, seed=42, temperature=0) for role in ROLES}
            metadata = models["metrics"].describe()
            for model in models.values():
                model.revision = {**metadata, "generation_config": dict(model.config)}
        provenance = {"schema": 1, "synthetic": True, "split": "new_development_demo", "service": SERVICE,
                      "benchmark_holdout_used": False, "fixture": args.fixture, "model": metadata,
                      "role_generation_config": {role: getattr(model, "config", None) for role, model in models.items()},
                      "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "sources": {"metrics_sha256": hashlib.sha256(metrics.read_bytes()).hexdigest(),
                                  "runbook_sha256": hashlib.sha256(RUNBOOK.encode()).hexdigest()},
                      "implementation": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((Path(__file__).resolve().parent.parent / "src" / "byte_agent").glob("*.py"))}}
        manifest = args.output / "experiment.json"
        text = json.dumps(provenance, indent=2, ensure_ascii=False)
        if manifest.exists() and manifest.read_text(encoding="utf-8") != text:
            raise ValueError("experiment provenance differs; use a new output directory")
        manifest.write_text(text, encoding="utf-8")
        cancelled = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: cancelled.set())
        result = Coordinator(args.output / "team", models, tools, SERVICE,
                             TeamBudget(wall_seconds=args.wall_seconds)).run(TASK, is_cancelled=cancelled.is_set)
        print(json.dumps({"status": result["status"], "fixture": result["fixture"], "roles": {r: v["trace"]["status"] for r, v in result["roles"].items()},
                          "usage": result["usage"], "conflicts": result["conflicts"], "answer": result["answer"],
                          "trace": str(args.output / "team" / "team-trace.json")}, ensure_ascii=False))
        return 0 if result["status"] == "completed" else 1
    finally:
        knowledge.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
