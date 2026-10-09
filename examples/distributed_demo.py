"""Reproducible same-host multi-process TCP queue/Agent failure experiment.

Creates one independent API process and three independent Agent worker processes
with separate local data/journals. Kills another claimed worker, waits for its
server lease to expire, then confirms recovery and stale-token rejection. Uses
a clearly labelled scripted model; it does not measure LLM reasoning quality.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import platform
import queue as queue_module
import secrets
import shutil
import sys
import time
import urllib.request

from byte_agent.distributed import AgentJobHandler, HTTPJobQueue, HTTPWorker, demo_server
from byte_agent.domain import create_dataset
from byte_agent.jobs import LostLease
from byte_agent.knowledge import Knowledge
from byte_agent.model import Scripted
from byte_agent.tools import registry


def fixture_model(data, service="social-follow"):
    knowledge = Knowledge(Path(data) / "knowledge.sqlite")
    try:
        tools = registry(knowledge, Path(data) / "metrics.sqlite")
        metric = tools["service_metrics"].call({"service": service, "window_minutes": 30})
        runbook = tools["knowledge_search"].call({"query": service + " incident policy", "service": service})
        changes = tools["incident_changes"].call({"service": service})
        answer = {"service": service, "error_rate": metric["error_rate"], "status": "healthy",
                  "likely_cause": "none", "recommendation": "monitor", "uncertainty": "none_detected",
                  "citations": [runbook["evidence"][0]["id"], changes["evidence"][0]["id"]]}
        return Scripted([{"calls": [
            {"name": "service_metrics", "arguments": {"service": service, "window_minutes": 30}},
            {"name": "knowledge_search", "arguments": {"query": service + " incident policy", "service": service}},
            {"name": "incident_changes", "arguments": {"service": service}}]},
            {"answer": json.dumps(answer)}])
    finally:
        knowledge.db.close()


def api_process(database, token, ready, implementation="stdlib"):
    if implementation == "waitress":
        from importlib.metadata import version
        from waitress import create_server
        from byte_agent.distributed import QueueApplication, MAX_REQUEST_BYTES
        server = create_server(QueueApplication(database, token), host="127.0.0.1", port=0,
                               threads=8, max_request_body_size=MAX_REQUEST_BYTES,
                               channel_timeout=15, clear_untrusted_proxy_headers=True)
        ready.put({"port": int(server.effective_port), "pid": os.getpid(),
                   "server_implementation": "Waitress " + version("waitress")})
        server.run()
        return
    with demo_server(database, token) as server:
        ready.put({"port": server.server_port, "pid": os.getpid(),
                   "server_implementation": "stdlib WSGI demo server"})
        server.serve_forever()


def worker_process(url, token, data, output, worker_id, results, barrier):
    client = HTTPJobQueue(url, token)
    handler = AgentJobHandler(data, output, fixture_model(data), verify=True)
    completed = []
    worker = HTTPWorker(client, handler, worker_id=worker_id, lease_seconds=15)
    barrier.wait(timeout=90)
    while True:
        value = worker.run_once()
        if value is None:
            break
        completed.append({"id": value.id, "status": value.status,
                          "attempts": value.attempts, "result": value.result})
    results.put({"pid": os.getpid(), "worker": worker_id, "jobs": completed})


def crash_process(url, token, ready):
    client = HTTPJobQueue(url, token)
    job = client.claim("crash-worker", lease_seconds=2)
    ready.put({"job": asdict(job), "pid": os.getpid()})
    while True:
        time.sleep(1)


def _wait(message_queue, seconds=60):
    try:
        return message_queue.get(timeout=seconds)
    except queue_module.Empty:
        raise RuntimeError("child process did not report progress within the bound") from None


def experiment(output, *, jobs=24, implementation="stdlib"):
    if type(jobs) is not int or not 3 <= jobs <= 1000:
        raise ValueError("jobs must be 3..1000")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "report.json").exists() or (output / "coordinator").exists():
        raise ValueError("select a fresh output directory for an independent experiment")
    started = time.monotonic()
    token = secrets.token_hex(32)  # Runtime-only: never stored in report or command line.
    context = multiprocessing.get_context("spawn")
    messages, completed = context.Queue(), context.Queue()
    barrier = context.Barrier(3)
    processes = []
    traces = []
    try:
        api = context.Process(target=api_process, args=(output / "coordinator" / "jobs.db", token, messages, implementation))
        api.start()
        processes.append(api)
        server = _wait(messages)
        url = f"http://127.0.0.1:{server['port']}"
        client = HTTPJobQueue(url, token)

        crash_job = client.enqueue("Diagnose social-follow using current 30-minute observations.",
                                   {"service": "social-follow"}, idempotency_key="crash-recovery")
        identical = client.enqueue(crash_job.task, crash_job.payload, idempotency_key="crash-recovery")
        idempotent = identical.id == crash_job.id
        crashed = context.Process(target=crash_process, args=(url, token, messages))
        crashed.start()
        processes.append(crashed)
        crash_record = _wait(messages)
        if crash_record["job"]["id"] != crash_job.id:
            raise RuntimeError("crash process claimed the wrong job")
        crashed.terminate()
        crashed.join(10)
        if crashed.is_alive():
            raise RuntimeError("crash worker could not be terminated")
        # Poll coordinator time until the recorded lease deadline passes; the
        # consumer never renews this deliberately killed process's lease.
        while time.time() <= crash_record["job"]["lease_until"] + .2:
            time.sleep(.05)

        queued = [crash_job]
        for index in range(jobs - 1):
            queued.append(client.enqueue("Diagnose social-follow using current 30-minute observations.",
                          {"service": "social-follow"}, idempotency_key=f"task-{index}"))
        dataset = output / "seed"
        create_dataset(dataset)
        knowledge = Knowledge(dataset / "knowledge.sqlite")
        try:
            knowledge.ingest(dataset / "knowledge")
        finally:
            knowledge.db.close()
        for index in range(3):
            node = output / f"worker-{index}"
            shutil.copytree(dataset, node / "data")
            process = context.Process(target=worker_process,
                args=(url, token, node / "data", node / "runs", f"worker-{index}", completed, barrier))
            process.start()
            processes.append(process)
        workers = [_wait(completed, 180) for _ in range(3)]
        for process in processes[2:]:
            process.join(20)
            if process.exitcode != 0:
                raise RuntimeError("worker process failed")
        identifiers = [item["id"] for node in workers for item in node["jobs"]]
        states = [client.get(job.id) for job in queued]
        recovery = client.get(crash_job.id)
        stale_rejected = False
        from byte_agent.jobs import Job
        try:
            client.complete(Job(**crash_record["job"]), {"late": True})
        except LostLease:
            stale_rejected = True
        for node in workers:
            for item in node["jobs"]:
                trace_file = Path(item["result"]["trace"])
                trace = json.loads(trace_file.read_text(encoding="utf-8"))
                traces.append({"job": item["id"], "path": trace_file.relative_to(output).as_posix(),
                               "sha256": hashlib.sha256(trace_file.read_bytes()).hexdigest(),
                               "fixture": trace["fixture"], "agent_status": trace["status"],
                               "tool_calls": trace["calls"],
                               "validation_attempts": sum(event.get("kind") == "validation" for event in trace["events"])})
                # Publish relative paths, never developer machine absolute paths.
                item["result"]["trace"] = trace_file.relative_to(output).as_posix()
        report = {"schema_version": 1, "completed": True,
                  "experiment": "same-host independent-process TCP queue and scripted ReAct Agent integration",
                  "real_network_transport": True, "real_llm": False,
                  "hosts": 1, "worker_processes": 3, "worker_containers": 0,
                  "server": {"pid": server["pid"], "transport": "HTTP loopback", "backend": "SQLite on coordinator local disk",
                             "server_implementation": server["server_implementation"]},
                  "platform": platform.platform(), "python": platform.python_version(),
                  "jobs": jobs, "completed_jobs": sum(job.status == "completed" for job in states),
                  "unique_claimed_results": len(set(identifiers)), "duplicate_final_results": len(identifiers) - len(set(identifiers)),
                  "all_runtime_completed": all(job.result and job.result["agent_status"] == "completed" for job in states),
                  "idempotent_submission": idempotent, "crashed_worker_pid": crash_record["pid"],
                  "recovered_job": {"id": recovery.id, "attempts": recovery.attempts, "status": recovery.status},
                  "stale_completion_rejected": stale_rejected,
                  "counts_by_worker": {node["worker"]: len(node["jobs"]) for node in workers},
                  "workers": workers, "traces": traces,
                  "wall_seconds": round(time.monotonic() - started, 3),
                  "limitations": ["One physical Windows host, not a real multi-host deployment or a throughput benchmark.",
                                  "Scripted model verifies engineering integration only, not model reasoning accuracy.",
                                  "Single coordinator and local SQLite; no replicated failover.",
                                  "Read-only job execution is at-least-once; fencing protects queue completion, not arbitrary external side effects."]}
        required = (report["completed_jobs"] == jobs and report["unique_claimed_results"] == jobs
                    and report["duplicate_final_results"] == 0 and report["all_runtime_completed"]
                    and idempotent and stale_rejected and recovery.attempts == 2
                    and all(report["counts_by_worker"].values())
                    and all(trace["tool_calls"] == 3 and trace["validation_attempts"] == 1 for trace in traces))
        report["acceptance_passed"] = required
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not required:
            raise RuntimeError("experiment acceptance failed; inspect the recorded report.json")
        return report
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
        messages.close()
        completed.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/distributed-demo"))
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--server-implementation", choices=["stdlib", "waitress"], default="stdlib")
    parser.add_argument("--inspect", action="store_true", help="inspect sanitized status/errors of a prior local experiment")
    parser.add_argument("--check-compose", action="store_true", help="validate compose syntax without starting containers or exposing a secret")
    args = parser.parse_args()
    if args.check_compose:
        import subprocess
        environment = dict(os.environ, QUEUE_TOKEN=secrets.token_hex(32))
        checked = subprocess.run(["docker", "compose", "-f", str(Path(__file__).resolve().parents[1] / "compose.yml"),
                                  "config", "--quiet"], env=environment, capture_output=True, timeout=60)
        if checked.returncode:
            raise RuntimeError("compose validation failed: " + checked.stderr.decode(errors="replace")[:1000])
        print(json.dumps({"compose_syntax_valid": True, "containers_started": False}))
        return
    if args.inspect:
        import sqlite3
        with sqlite3.connect((args.output / "coordinator" / "jobs.db").resolve().as_uri() + "?mode=ro", uri=True) as database:
            print(json.dumps(database.execute("SELECT id,status,attempts,error FROM jobs ORDER BY created_at").fetchall(), indent=2))
        return
    report = experiment(args.output, jobs=args.jobs, implementation=args.server_implementation)
    print(json.dumps({"completed": report["completed"], "jobs": report["jobs"],
                      "recovery_attempts": report["recovered_job"]["attempts"],
                      "counts_by_worker": report["counts_by_worker"],
                      "report": str(args.output / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
