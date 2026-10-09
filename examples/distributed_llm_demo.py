"""One real Ollama Agent job executed by an independent TCP worker.

The parent reads development benchmark labels only for a post-run independent
oracle. The child Agent receives the public prompt, exact service, read-only
knowledge/metrics snapshot and trusted Skill; services.json is never copied to
the worker. An independent Waitress process owns the queue database.

Requires requirements-distributed.txt and the adjacent byte-agent-eval checkout.
Default model: installed qwen3:4b-instruct; this script never downloads models.
"""
import argparse
from dataclasses import asdict
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT / "src"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hashes(root):
    return {path.relative_to(root).as_posix(): digest(path)
            for path in sorted((root / "src").rglob("*.py"))}


def serve_child(database, ready_file):
    # Only this branch runs in the queue server process. It never reads case
    # labels, task manifests, model output or any worker-local journal.
    from importlib.metadata import version
    from waitress import create_server
    from byte_agent.distributed import QueueApplication, MAX_REQUEST_BYTES
    secret, nonce = os.environ["QUEUE_TOKEN"], os.environ["BYTE_AGENT_EXPERIMENT_NONCE"]
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        raise ValueError("invalid experiment ownership identity")
    application = QueueApplication(database, secret)
    identity = {"pid": os.getpid(), "service": "byte-agent-distributed-llm-demo",
                "run_nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest()}

    def owned_application(environ, start_response):
        path = environ.get("PATH_INFO")
        if path not in ("/experiment-health", "/experiment-stop"):
            return application(environ, start_response)
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        provided_nonce = environ.get("HTTP_X_EXPERIMENT_NONCE", "")
        authorized = (isinstance(authorization, str) and authorization.isascii() and
                      isinstance(provided_nonce, str) and provided_nonce.isascii() and
                      hmac.compare_digest(authorization, "Bearer " + secret) and
                      hmac.compare_digest(provided_nonce, nonce))
        expected_method = "GET" if path == "/experiment-health" else "POST"
        valid_method = environ.get("REQUEST_METHOD") == expected_method
        status = "200 OK" if authorized and valid_method else "401 Unauthorized"
        body = json.dumps({"ok": True, **identity} if authorized and valid_method else
                          {"error": "authenticated experiment ownership required"}).encode("utf-8")
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store")])
        if authorized and valid_method and path == "/experiment-stop":
            # Only this example's authenticated owner can stop its own ephemeral
            # process. Self-exit avoids Windows launcher/child PID confusion.
            shutdown = threading.Timer(.2, lambda: os._exit(0))
            shutdown.daemon = True
            shutdown.start()
        return [body]

    server = create_server(owned_application,
        host="127.0.0.1", port=0, threads=8, max_request_body_size=MAX_REQUEST_BYTES,
        channel_timeout=15, clear_untrusted_proxy_headers=True)
    write_json(ready_file, {**identity, "host": "127.0.0.1",
                          "port": int(server.effective_port), "waitress": version("waitress")})
    server.run()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def validate_server_identity(ready, nonce):
    expected = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    if (ready.get("host") != "127.0.0.1" or type(ready.get("port")) is not int or
            not 1 <= ready["port"] <= 65535 or type(ready.get("pid")) is not int or ready["pid"] <= 0 or
            ready.get("service") != "byte-agent-distributed-llm-demo" or ready.get("run_nonce_sha256") != expected):
        raise RuntimeError("queue readiness identity does not match this experiment")


def owned_server_request(ready, secret, nonce, *, stop=False):
    validate_server_identity(ready, nonce)
    suffix = "/experiment-stop" if stop else "/experiment-health"
    request = urllib.request.Request(f"http://127.0.0.1:{ready['port']}" + suffix,
        data=b"" if stop else None, method="POST" if stop else "GET",
        headers={"Authorization": "Bearer " + secret, "X-Experiment-Nonce": nonce})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=3) as response:
        body = response.read(4097)
        if response.status != 200 or len(body) > 4096:
            raise RuntimeError("invalid authenticated ownership response")
        verified = json.loads(body)
    if (verified.get("ok") is not True or any(verified.get(key) != ready[key] for key in
            ("pid", "service", "run_nonce_sha256"))):
        raise RuntimeError("authenticated queue identity differs from readiness file")
    return verified


def wait_owned_server(process, ready_file, secret, nonce, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.is_file():
            ready = json.loads(ready_file.read_text(encoding="utf-8"))
            validate_server_identity(ready, nonce)
            try:
                owned_server_request(ready, secret, nonce)
                return ready
            except (OSError, TimeoutError):
                # Socket binding/readiness publication can precede the loop.
                pass
        elif process.poll() is not None:
            raise RuntimeError("independent queue server exited before readiness")
        time.sleep(.1)
    raise TimeoutError("independent authenticated queue readiness timeout")


def terminate_example_server(process, ready_file, secret, nonce):
    if process is None:
        return
    if ready_file is not None and ready_file.is_file():
        try:
            ready = json.loads(ready_file.read_text(encoding="utf-8"))
            owned_server_request(ready, secret, nonce, stop=True)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        except (OSError, ValueError, RuntimeError, TimeoutError):
            # Never kill an arbitrary PID read from a file. The Popen handle is
            # our own launcher; a verified live child normally self-exits above.
            pass
    terminate_owned(process)


def terminate_owned(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def sqlite_snapshot(source, target):
    """Use SQLite backup so a source WAL is included in the consistent copy."""
    source = Path(source).resolve()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    snapshot = sqlite3.connect(target)
    try:
        original.backup(snapshot)
    finally:
        snapshot.close()
        original.close()


def knowledge_snapshot(source, target):
    """Copy only contained Markdown evidence, never arbitrary sibling files."""
    source, target = Path(source).resolve(), Path(target)
    target.mkdir(parents=True)
    for path in sorted(source.rglob("*.md")):
        if not path.resolve().is_relative_to(source):
            raise ValueError("knowledge symlink escapes its authorized root")
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("model base URL must be a plain HTTP(S) origin without credentials")
    return value.rstrip("/")


def portable_job(job, output):
    result = asdict(job)
    # An active attempt's token is a capability, never a publication artifact.
    result.pop("lease_token", None)
    if isinstance(result.get("result"), dict) and result["result"].get("trace"):
        trace = Path(result["result"]["trace"]).resolve()
        if not trace.is_relative_to(output):
            raise ValueError("worker returned an artifact outside this experiment")
        result["result"]["trace"] = trace.relative_to(output).as_posix()
    return result


def experiment(args):
    from byte_agent.distributed import HTTPJobQueue
    from byte_agent.knowledge import Knowledge
    from byte_agent.model import Ollama

    data, output = args.data.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError("select a new output directory; existing experiment output is never overwritten")
    eval_root = args.eval_root.resolve()
    if not (eval_root / "src" / "byte_eval" / "scoring.py").is_file():
        raise ValueError("adjacent byte-agent-eval checkout is required for independent scoring")
    if not args.python.is_file():
        raise ValueError("--python must point to an existing Python interpreter")
    manifest_file = data / "services.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True or not isinstance(manifest.get("tasks"), list):
        raise ValueError("this benchmark smoke requires an explicitly synthetic incident dataset")
    selected = [task for task in manifest["tasks"] if task.get("id") == "creator-upload" and task.get("split") == "dev"]
    if len(selected) != 1:
        raise ValueError("dataset must contain exactly one creator-upload development case")
    case = selected[0]
    if case.get("service") != "creator-upload" or not isinstance(case.get("prompt"), str):
        raise ValueError("invalid creator-upload case")
    if not (data / "knowledge").is_dir() or not (data / "metrics.sqlite").is_file():
        raise ValueError("existing knowledge directory and metrics.sqlite are required")
    if not args.skill.is_file():
        raise ValueError("an explicit existing trusted Skill is required")
    model_url = origin(args.base_url)
    model = Ollama(args.model, model_url, 3072, 384, 900, 42)
    model_revision = model.describe()
    output.mkdir(parents=True)
    server, worker, queue, ready_file, ready = None, None, None, None, None
    secret = secrets.token_hex(32)
    run_nonce = secrets.token_hex(32)
    environment = dict(os.environ)
    environment["QUEUE_TOKEN"] = secret
    environment["BYTE_AGENT_EXPERIMENT_NONCE"] = run_nonce
    environment["PYTHONPATH"] = str(PLATFORM_ROOT / "src")
    environment["PYTHONUNBUFFERED"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    metadata = {"schema_version": 1, "experiment": "one independent Waitress server and real TCP Ollama Agent worker",
        "synthetic_incident_data": True, "fixture": False, "split": "dev",
        "task": {"id": case["id"], "service": case["service"], "prompt": case["prompt"]},
        "model": model_revision, "generation_config": model.config,
        "runtime_budgets": {"steps": 8, "calls": 16, "tokens": 48000, "context_chars": 60000},
        "lease_seconds": 30, "queue_transport": "authenticated HTTP over loopback TCP",
        "hosts": 1, "independent_worker_processes": 1, "worker_containers": 0,
        "skill": {"path": "operator/SKILL.md", "sha256": digest(args.skill)},
        "worker_received_case_fields": ["prompt", "service"], "worker_data_contains_services_json": False,
        "ground_truth_use": "parent-side byte_eval.scoring.score only after worker execution",
        "source": {"platform": code_hashes(PLATFORM_ROOT), "eval": code_hashes(eval_root),
                   "example_sha256": digest(__file__), "dataset_manifest_sha256": digest(manifest_file)},
        "python": platform.python_version(), "platform": platform.platform(),
        "limitations": ["One selected development case, not an unbiased accuracy estimate.",
                        "One physical host and independent TCP processes, not multiple physical machines or a production load test.",
                        "One coordinator with local SQLite; no replicated failover.",
                        "Read-only execution is at-least-once; queue result fencing does not make arbitrary side effects exactly-once."]}
    try:
        # Only actual observations and trusted operator instructions reach the
        # worker. Benchmark labels/services.json stay in the parent data path.
        local_data = output / "node" / "data"
        knowledge_snapshot(data / "knowledge", local_data / "knowledge")
        sqlite_snapshot(data / "metrics.sqlite", local_data / "metrics.sqlite")
        knowledge = Knowledge(local_data / "knowledge.sqlite")
        try:
            knowledge.ingest(local_data / "knowledge")
        finally:
            knowledge.db.close()
        skill = output / "operator" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        shutil.copyfile(args.skill, skill)
        metadata["worker_input_hashes"] = {path.relative_to(output).as_posix(): digest(path)
            for path in sorted(local_data.rglob("*")) if path.is_file()}
        write_json(output / "experiment.json", metadata)
        ready_file = output / "coordinator" / "server.json"
        server_command = [str(args.python), str(Path(__file__).resolve()), "--_serve",
                          "--database", str(output / "coordinator" / "jobs.db"),
                          "--ready-file", str(ready_file)]
        server = subprocess.Popen(server_command, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        ready = wait_owned_server(server, ready_file, secret, run_nonce)
        queue = HTTPJobQueue(f"http://127.0.0.1:{ready['port']}", secret, timeout=30)
        job = queue.enqueue(case["prompt"], {"service": case["service"]},
                            idempotency_key="creator-upload-real-tcp-smoke", max_attempts=1)
        metadata["job_id"], metadata["server"] = job.id, ready
        write_json(output / "experiment.json", metadata)
        command = [str(args.python), "-m", "byte_agent.distributed", "agent-worker",
            "--url", queue.base_url, "--data", str(local_data),
            "--output", str(output / "node" / "runs"), "--model", args.model,
            "--base-url", model_url, "--model-context", "3072", "--model-output", "384",
            "--timeout", "900", "--seed", "42", "--lease-seconds", "30",
            "--max-steps", "8", "--max-calls", "16", "--max-tokens", "48000",
            "--skill", str(skill), "--verify", "--once"]
        worker = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        print(json.dumps({"event": "started", "task_id": case["id"], "server_pid": ready["pid"],
                          "server_launcher_pid": server.pid,
                          "worker_pid": worker.pid}), flush=True)
        stdout, stderr = worker.communicate(timeout=args.wall_timeout)
        if secret.encode() in stdout or secret.encode() in stderr:
            raise RuntimeError("worker emitted a runtime credential; raw output discarded")
        # Preserve diagnostics after removing machine-specific path prefixes.
        diagnostic = stderr.decode("utf-8", errors="replace")[:8000]
        for path, placeholder in ((output, "<experiment>"), (PLATFORM_ROOT, "<platform>"),
                                  (eval_root, "<eval>"), (data, "<source-data>"), (args.python.parent, "<python>")):
            diagnostic = diagnostic.replace(str(path), placeholder)
        final_job = queue.get(job.id)
        if final_job is None:
            raise RuntimeError("submitted queue job is missing")
        job_record = portable_job(final_job, output)
        write_json(output / "job.json", job_record)
        write_json(output / "worker-log.json", {"exit_code": worker.returncode, "stderr": diagnostic,
                                                 "stdout_recorded_as": "job.json"})
        trace_path = output / "node" / "runs" / job.id / "trace.json"
        if not trace_path.is_file():
            raise RuntimeError("worker did not export an Agent trace")
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        if trace.get("fixture") is not False:
            raise RuntimeError("real-model smoke unexpectedly returned a fixture trace")
        # Ground truth first enters executable scoring here, after the worker
        # has terminated. It is never passed to AgentJobHandler or the queue.
        sys.path.insert(0, str(eval_root / "src"))
        from byte_eval.scoring import score
        oracle = score(case, trace)
        oracle_record = {"task_id": case["id"], "oracle": "byte_eval.scoring.score",
                         "scorer_sha256": digest(eval_root / "src" / "byte_eval" / "scoring.py"),
                         "evaluated_after_worker_exit": True, **oracle}
        write_json(output / "oracle.json", oracle_record)
        record_complete = worker.returncode == 0 and final_job.status == "completed" and trace.get("status") != "uncertain"
        unchanged_sources = metadata["source"]["platform"] == code_hashes(PLATFORM_ROOT) and metadata["source"]["eval"] == code_hashes(eval_root)
        record_complete = record_complete and unchanged_sources
        report = {"schema_version": 1, "completed": record_complete, "task_id": case["id"],
            "fixture": False, "real_llm": True, "synthetic_incident_data": True,
            "hosts": 1, "worker_processes": 1, "worker_containers": 0,
            "server_pid": ready["pid"], "server_launcher_pid": server.pid, "worker_pid": worker.pid,
            "server_readiness_authenticated": True, "waitress": ready["waitress"],
            "queue_status": final_job.status, "agent_status": trace.get("status"),
            "independent_oracle_success": oracle["success"], "oracle_failure": oracle["failure"],
            "known_tokens": trace.get("tokens"), "unknown_usage": trace.get("unknown_usage"),
            "model_steps": trace.get("steps"), "tool_calls": trace.get("calls"),
            "validation_attempts": sum(e.get("kind") == "validation" for e in trace.get("events", [])),
            "sources_unchanged_during_execution": unchanged_sources,
            "wall_seconds": round(time.monotonic() - started, 3),
            "trace_path": trace_path.relative_to(output).as_posix(), "trace_sha256": digest(trace_path),
            "experiment_path": "experiment.json", "experiment_sha256": digest(output / "experiment.json"),
            "oracle_path": "oracle.json", "oracle_sha256": digest(output / "oracle.json"),
            "job_path": "job.json", "job_sha256": digest(output / "job.json"),
            "limitations": metadata["limitations"]}
        write_json(output / "report.json", report)
        return report
    except Exception as exc:
        # Raw exceptions may contain local paths; use only their type in the
        # portable report. Preserve any Runtime journal/trace already exported.
        failure = {"schema_version": 1, "completed": False, "fixture": False,
            "task_id": "creator-upload", "error_type": type(exc).__name__,
            "server_pid": server.pid if server else None, "worker_pid": worker.pid if worker else None,
            "wall_seconds": round(time.monotonic() - started, 3),
            "note": "Infrastructure or execution did not produce the full recorded protocol. No success score inferred."}
        write_json(output / "report.json", failure)
        raise
    finally:
        terminate_owned(worker)
        terminate_example_server(server, ready_file, secret, run_nonce)
        if worker is not None:
            for stream in (worker.stdout, worker.stderr):
                if stream:
                    stream.close()
        if server is not None and server.stderr:
            server.stderr.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--eval-root", type=Path, default=PLATFORM_ROOT.parent / "byte-agent-eval")
    parser.add_argument("--skill", type=Path, default=PLATFORM_ROOT / "skills" / "incident-analysis" / "SKILL.md")
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--wall-timeout", type=float, default=2400)
    parser.add_argument("--_serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._serve:
        if args.database is None or args.ready_file is None:
            parser.error("server child requires database and readiness file")
        serve_child(args.database, args.ready_file)
        return
    if args.data is None or args.output is None:
        parser.error("--data and --output are required")
    if args.wall_timeout <= 0:
        parser.error("--wall-timeout must be positive")
    result = experiment(args)
    print(json.dumps({"completed": result["completed"], "oracle_success": result["independent_oracle_success"],
                      "report": str(args.output / "report.json")}), flush=True)


if __name__ == "__main__":
    main()
