import argparse
import json
import sqlite3
from pathlib import Path
from .knowledge import Knowledge
from .model import Ollama, Scripted
from .runtime import Runtime
from .tools import registry


def seed_metrics(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS metrics (service TEXT, minute INTEGER, requests INTEGER, errors INTEGER, p95_ms REAL, PRIMARY KEY(service,minute))")
        db.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?)", [("search", i, 1000, 50, 480) for i in range(30)] + [("live", i, 2000, 2, 120) for i in range(30)])
    db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ingest", "demo", "run", "mcp"])
    parser.add_argument("--knowledge", type=Path, default=Path("examples/knowledge"))
    parser.add_argument("--data", type=Path, default=Path("runs/data"))
    parser.add_argument("--output", type=Path, default=Path("runs/demo"))
    parser.add_argument("--task", default="Diagnose search service errors using metrics and cite the runbook.")
    parser.add_argument("--model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    knowledge = Knowledge(args.data / "knowledge.sqlite")
    embedding = Ollama(args.embedding_model, args.base_url).embed if args.embedding_model else None
    if args.command == "ingest":
        print(json.dumps({"chunks": knowledge.ingest(args.knowledge, embedding), "retrieval": "hybrid" if embedding else "lexical"}))
        return
    metrics = args.data / "metrics.sqlite"
    if args.command == "demo":
        knowledge.ingest(args.knowledge)
        seed_metrics(metrics)
        evidence = knowledge.search("search error rollback", 1)
        citation = evidence[0]["id"] if evidence else "missing"
        model = Scripted([{"calls": [{"name": "service_metrics", "arguments": {"service": "search"}},
                                     {"name": "knowledge_search", "arguments": {"query": "search error rollback"}}]},
                          {"answer": f"Synthetic search error rate is 5%. Investigate the release and use rollback review per [{citation}]. This is a scripted fixture."}])
    else:
        model = Ollama(args.model, args.base_url) if args.model else None
    tools = registry(knowledge, metrics, embedding)
    if args.command == "mcp":
        from .mcp import serve
        serve(tools)
        return
    if model is None:
        parser.error("run requires --model; ingest knowledge and provide metrics.sqlite first")
    if not model.fixture:
        model.revision = model.describe()
    result = Runtime(args.output, model, tools, max_steps=args.max_steps).run(args.task)
    print(json.dumps({"status": result["status"], "fixture": result["fixture"], "answer": result["answer"], "trace": str(args.output / "trace.json")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
