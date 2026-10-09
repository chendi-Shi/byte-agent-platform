# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml requirements-distributed.txt README.md LICENSE ./
COPY src ./src
COPY skills ./skills
RUN python -m pip install --no-cache-dir . -r requirements-distributed.txt \
    && useradd --uid 10001 --create-home byteagent \
    && mkdir -p /worker/data /worker/jobs /queue \
    && chown -R byteagent:byteagent /worker /queue

# Deployment bootstrap, separate from the frozen training/runtime source.
# /input must contain consistent, closed SQLite backup files without a WAL.
# Copy into each worker's private writable volume because Knowledge can migrate.
COPY <<'PY' /app/container_worker.py
import os
from pathlib import Path
import sqlite3
import sys

inputs = Path(os.environ.get("AGENT_INPUT_DIR", "/input"))
state = Path(os.environ.get("AGENT_STATE_DIR", "/worker"))
data, jobs = state / "data", state / "jobs"
data.mkdir(parents=True, exist_ok=True)
jobs.mkdir(parents=True, exist_ok=True)
for name in ("knowledge.sqlite", "metrics.sqlite"):
    source = inputs / name
    if not source.is_file():
        raise SystemExit("Prepare both consistent SQLite input snapshots before starting Agent workers.")
    if any((inputs / (name + suffix)).exists() for suffix in ("-wal", "-journal")):
        raise SystemExit("Input must be a closed SQLite backup snapshot, not a live WAL/journal database.")
    temporary = data / (name + ".next")
    temporary.unlink(missing_ok=True)
    original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    snapshot = sqlite3.connect(temporary)
    try:
        original.backup(snapshot)
    finally:
        snapshot.close()
        original.close()
    temporary.replace(data / name)

command = [sys.executable, "-m", "byte_agent.distributed", "agent-worker",
           "--url", os.environ.get("QUEUE_URL", "http://queue:8765"),
           "--data", str(data), "--output", str(jobs),
           "--model", os.environ.get("AGENT_MODEL", "qwen3:4b-instruct"),
           "--base-url", os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
           "--model-context", "3072", "--model-output", "384", "--timeout", "900",
           "--verify", "--skill", "/app/skills/incident-analysis/SKILL.md"]
if os.environ.get("AGENT_WORKER_ID"):
    command.extend(["--worker-id", os.environ["AGENT_WORKER_ID"]])
os.execv(sys.executable, command)
PY

USER byteagent
CMD ["python", "-m", "byte_agent.distributed", "serve", "--database", "/queue/jobs.db", "--host", "0.0.0.0", "--port", "8765"]
