"""Real loopback HTTP regression tests; no Ollama/model weights are involved."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import unittest
import urllib.error

from byte_agent.model import Ollama


MODEL = "test-instruct:latest"
DIGEST = "a" * 64
DEFAULTS = {
    "/api/tags": {"models": [{"name": MODEL, "digest": DIGEST, "details": {"family": "test"}}]},
    "/api/show": {"template": "{{ .Prompt }}", "capabilities": ["completion", "tools"]},
    "/api/version": {"version": "test-http-only"},
}


@contextmanager
def metadata_server(overrides=None, *, trickle=None):
    """Specs are (status, JSON-or-bytes, delay); status 0 disconnects."""
    plans = overrides or {}
    counts = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            if self.command == "POST":
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                index = counts.get(self.path, 0)
                counts[self.path] = index + 1
            specs = plans.get(self.path, [(200, DEFAULTS.get(self.path, {}), 0)])
            status, payload, delay = specs[min(index, len(specs) - 1)]
            if delay:
                time.sleep(delay)
            if not status:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                if trickle and self.path == "/api/tags":
                    for value in encoded:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(trickle)
                else:
                    self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionError):
                pass  # A timeout test deliberately closes its own connection.

        do_GET = respond
        do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", counts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class MetadataTests(unittest.TestCase):
    def test_metadata_can_arrive_after_old_ten_second_limit(self):
        with metadata_server({"/api/tags": [(200, DEFAULTS["/api/tags"], 10.25)]}) as (url, counts):
            started = time.monotonic()
            revision = Ollama("test-instruct", url, timeout=20).describe()
            self.assertGreaterEqual(time.monotonic() - started, 10)
            self.assertEqual(revision["name"], MODEL)
            self.assertEqual(revision["digest"], DIGEST)
            self.assertEqual(revision["generation_config"]["timeout"], 20)
            self.assertEqual(counts, {"/api/tags": 1, "/api/show": 1, "/api/version": 1})

    def test_transient_metadata_reads_retry_and_preserve_identity(self):
        plans = {
            "/api/tags": [(503, {}, 0), (0, {}, 0), (200, DEFAULTS["/api/tags"], 0)],
            "/api/show": [(503, {}, 0), (200, DEFAULTS["/api/show"], 0)],
            "/api/version": [(503, {}, 0), (200, DEFAULTS["/api/version"], 0)],
        }
        with metadata_server(plans) as (url, counts):
            self.assertEqual(Ollama(MODEL, url, timeout=5).describe()["digest"], DIGEST)
            self.assertEqual(counts, {"/api/tags": 3, "/api/show": 2, "/api/version": 2})

    def test_transient_failure_is_bounded_without_fallback(self):
        with metadata_server({"/api/tags": [(503, {}, 0)]}) as (url, counts):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                Ollama(MODEL, url, timeout=5).describe()
            self.assertEqual(raised.exception.code, 503)
            self.assertEqual(counts, {"/api/tags": 3})

    def test_authentication_failure_is_not_retried(self):
        with metadata_server({"/api/tags": [(401, {}, 0)]}) as (url, counts):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                Ollama(MODEL, url).describe()
            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(counts, {"/api/tags": 1})

    def test_invalid_json_is_not_retried(self):
        with metadata_server({"/api/tags": [(200, b"{broken", 0)]}) as (url, counts):
            with self.assertRaises(json.JSONDecodeError):
                Ollama(MODEL, url).describe()
            self.assertEqual(counts, {"/api/tags": 1})

    def test_oversized_metadata_is_rejected_without_retry(self):
        with metadata_server({"/api/tags": [(200, b"x" * (4 * 1024 * 1024 + 1), 0)]}) as (url, counts):
            with self.assertRaisesRegex(ValueError, "exceeds 4 MiB"):
                Ollama(MODEL, url, timeout=20).describe()
            self.assertEqual(counts, {"/api/tags": 1})

    def test_missing_ambiguous_or_invalid_identity_fails_before_show(self):
        entries = DEFAULTS["/api/tags"]["models"]
        for models in ([], entries * 2, [{"name": MODEL}],
                       [{"name": MODEL, "digest": "x" * 64}], [{}], "wrong"):
            with self.subTest(models=models), metadata_server({"/api/tags": [(200, {"models": models}, 0)]}) as (url, counts):
                with self.assertRaises(ValueError):
                    Ollama(MODEL, url).describe()
                self.assertEqual(counts, {"/api/tags": 1})

    def test_invalid_template_and_thinking_identity_remain_errors(self):
        for details in ({"template": "{{ .Function }}"},
                        {"model_info": {"general.thinking_values": [True]}}):
            with self.subTest(details=details), metadata_server({"/api/show": [(200, details, 0)]}) as (url, counts):
                with self.assertRaises(ValueError):
                    Ollama(MODEL, url).describe()
                self.assertEqual(counts, {"/api/tags": 1, "/api/show": 1})

    def test_selected_short_timeout_bounds_metadata_wait(self):
        with metadata_server({"/api/tags": [(200, DEFAULTS["/api/tags"], 0.3)]}) as (url, counts):
            with self.assertRaises(TimeoutError):
                Ollama(MODEL, url, timeout=0.05).describe()
            # On a busy host the budget can expire before the server accepts.
            self.assertLessEqual(counts.get("/api/tags", 0), 1)
            self.assertNotIn("/api/show", counts)
            self.assertNotIn("/api/version", counts)

    def test_trickled_body_cannot_return_after_metadata_budget(self):
        # Individual 10ms waits fit the socket timeout, but the full JSON does
        # not fit the budget. The client must not silently accept its identity.
        with metadata_server(trickle=0.01) as (url, counts):
            with self.assertRaises(TimeoutError):
                Ollama(MODEL, url, timeout=0.2).describe()
            self.assertLessEqual(counts.get("/api/tags", 0), 1)
            self.assertNotIn("/api/show", counts)
            self.assertNotIn("/api/version", counts)

    def test_inference_and_write_requests_are_not_retried(self):
        with metadata_server({"/api/chat": [(503, {}, 0)],
                              "/api/create": [(503, {}, 0)]}) as (url, counts):
            provider = Ollama(MODEL, url)
            with self.assertRaises(urllib.error.HTTPError) as chat_error:
                provider.complete([], [])
            chat_error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as create_error:
                provider.request("/api/create", {"model": "another"})
            create_error.exception.close()
            self.assertEqual(counts, {"/api/chat": 1, "/api/create": 1})


if __name__ == "__main__":
    unittest.main()
