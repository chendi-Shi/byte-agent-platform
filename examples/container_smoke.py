"""Build/run the Compose demo profile with two containers; explicit fixtures.

This integration check executes Docker only when invoked. It uses a fresh
project, ephemeral bearer environment, actual Waitress/TCP queue and synthetic
numeric callbacks. It does not invoke an Agent, model or multiple physical hosts.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def compose_rows(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def fixture_result(job, value):
    """Check the handler receipt after completion releases queue ownership."""
    expected = value * 2
    worker = job.result.get("worker") if isinstance(job.result, dict) else None
    if (job.status != "completed" or job.attempts != 1 or not isinstance(job.result, dict) or
            type(job.result.get("value")) is not int or job.result.get("value") != expected or not isinstance(worker, str) or
            re.fullmatch(r"[0-9a-f]{32}", worker) is None or job.worker is not None or
            job.lease_token is not None or job.lease_until is not None):
        raise RuntimeError("numeric fixture callback result or completed lease state invalid")
    # The handler records the worker that performed the callback. Terminal
    # queue rows clear worker/lease ownership, so those fields cannot identify
    # the historical executor or be compared to this receipt.
    return {"id": job.id, "status": job.status, "attempts": job.attempts,
            "input": value, "expected": expected, "result": job.result}


def command(arguments, environment, secret, *, timeout=600, emit=False):
    completed = subprocess.run(arguments, env=environment, capture_output=True, text=True, timeout=timeout)
    if emit:
        # Compose build/up output contains no runtime env dump. Still scrub the
        # generated credential before emitting any diagnostic text to CI logs.
        safe = (completed.stdout + completed.stderr).replace(secret, "<redacted>")
        print(safe[-6000:], flush=True)
    if completed.returncode:
        raise RuntimeError("container command failed; see sanitized CI stage output")
    return completed.stdout


def run(args):
    from byte_agent.distributed import HTTPJobQueue, QueueTransportError
    output = args.output.resolve()
    if output.exists():
        raise ValueError("select a new output directory")
    output.mkdir(parents=True)
    secret = secrets.token_hex(32)
    project = "byte-agent-ci-" + secrets.token_hex(6)
    if not re.fullmatch(r"byte-agent-ci-[0-9a-f]{12}", project):
        raise ValueError("invalid task-owned project identity")
    environment = dict(os.environ, QUEUE_TOKEN=secret)
    compose = ["docker", "compose", "--project-name", project, "--file", str(ROOT / "compose.yml"), "--profile", "demo"]
    started = time.monotonic()
    report = {"schema_version": 1, "complete": False, "fixture": True, "real_llm": False,
        "agent_runtime_executed": False, "physical_hosts": 1,
        "scope": "One Docker host, Waitress queue plus two Compose numeric fixture callback containers",
        "runner_platform": platform.system(),
        "project": project, "planned_jobs": args.jobs,
        "image_built": False, "containers_executed": False, "jobs": [],
        "configuration_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                 for name in ("compose.yml", "Dockerfile", ".dockerignore")},
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Numeric fixture callbacks only; no Agent/LLM quality or training evaluation.",
                        "Containers share one physical CI host; no physical multi-host, HA or scale result."]}
    print(json.dumps({"event": "container-smoke-start", "fixture": True, "real_llm": False,
                      "physical_hosts": 1, "project": project}), flush=True)
    passed, error = False, None
    try:
        report["docker_cli_version"] = command(["docker", "--version"], environment, secret, timeout=30).strip()
        report["compose_version"] = command(["docker", "compose", "version", "--short"], environment, secret, timeout=30).strip()
        command([*compose, "build", "queue", "worker"], environment, secret, emit=True)
        report["image_built"] = True
        report["image_id"] = command(["docker", "image", "inspect", "byte-agent-platform:distributed-v3", "--format", "{{.Id}}"],
                                     environment, secret, timeout=30).strip()
        report["container_start_attempted"] = True
        report["containers_executed"] = None  # Up may partially start before an error.
        command([*compose, "up", "--detach", "--no-build", "--scale", "worker=2", "queue", "worker"], environment, secret, emit=True, timeout=120)
        report["containers_executed"] = True
        queue = HTTPJobQueue("http://127.0.0.1:8765", secret, timeout=5)
        deadline = time.monotonic() + 90
        while True:
            try:
                # Authenticated read, rather than accepting any unauth health.
                if queue.get("0" * 32) is None:
                    break
            except (QueueTransportError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("authenticated container queue readiness timeout")
            time.sleep(.25)
        containers = compose_rows(command([*compose, "ps", "--format", "json"], environment, secret, timeout=30))
        services = Counter(row["Service"] for row in containers if row.get("State") == "running")
        if services != Counter({"queue": 1, "worker": 2}):
            raise RuntimeError("expected one running queue and two running fixture workers")
        report["running_container_counts"] = dict(services)
        report["container_ids"] = [row["ID"] for row in containers]
        identities, values = [], {}
        for index in range(args.jobs):
            value = index - args.jobs // 2
            task, payload, key = "Explicit synthetic numeric transport fixture " + str(index), {"value": value}, "container-fixture-" + str(index)
            job = queue.enqueue(task, payload, idempotency_key=key, max_attempts=1)
            repeated = queue.enqueue(task, payload, idempotency_key=key, max_attempts=1)
            if repeated.id != job.id:
                raise RuntimeError("idempotent submission created a duplicate job")
            identities.append(job.id)
            values[job.id] = value
        if len(set(identities)) != args.jobs:
            raise RuntimeError("distinct fixture submissions did not produce distinct identities")
        deadline = time.monotonic() + 90
        remaining = set(identities)
        results = {}
        while remaining:
            for identity in list(remaining):
                job = queue.get(identity)
                if job is None:
                    raise RuntimeError("submitted fixture disappeared")
                if job.status in ("failed", "cancelled"):
                    raise RuntimeError("fixture job terminated without completion")
                if job.status == "completed":
                    # Never serialize the Job lease token or its raw dataclass.
                    results[identity] = fixture_result(job, values[identity])
                    remaining.remove(identity)
            if remaining and time.monotonic() >= deadline:
                raise TimeoutError("container fixture tasks did not complete within budget")
            if remaining:
                time.sleep(.1)
        final_containers = compose_rows(command([*compose, "ps", "--format", "json"], environment, secret, timeout=30))
        final_counts = Counter(row["Service"] for row in final_containers if row.get("State") == "running")
        if final_counts != services:
            raise RuntimeError("container stopped while processing fixtures")
        report["jobs"] = [results[identity] for identity in identities]
        report["completed_jobs"] = len(results)
        report["idempotent_submission"] = True
        report["numeric_results_correct"] = True
        report["counts_by_worker_id"] = dict(Counter(row["result"]["worker"] for row in report["jobs"]))
        report["distribution_note"] = "Two worker containers ran; scheduling/fairness is not assumed. Above counts record the workers that actually completed fixtures."
        passed = True
    except BaseException as exc:
        error = exc
        report["error_type"] = type(exc).__name__
    finally:
        try:
            # Only this random task-owned Compose project is removed.
            command([*compose, "down", "--volumes", "--remove-orphans"], environment, secret, timeout=90, emit=True)
            report["cleanup_project_only"] = {"attempted": True, "succeeded": True}
        except BaseException as exc:
            report["cleanup_project_only"] = {"attempted": True, "succeeded": False, "error_type": type(exc).__name__}
            if error is None:
                error = exc
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["complete"] = passed and report["cleanup_project_only"]["succeeded"]
        encoded = json.dumps(report, ensure_ascii=False)
        if secret in encoded:
            raise RuntimeError("credential detected in report; discarded")
        write_json(output / "report.json", report)
    print(json.dumps({"event": "container-smoke-finish", "complete": report["complete"], "fixture": True,
                      "real_llm": False, "completed_jobs": report.get("completed_jobs", 0), "physical_hosts": 1}), flush=True)
    if error is not None:
        raise RuntimeError("container integration did not complete; report records the failed stage") from None
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 100:
        parser.error("--jobs must be between 1 and 100")
    run(args)


if __name__ == "__main__":
    main()
