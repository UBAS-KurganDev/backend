"""Exercise the standalone client against the HTTP query lifecycle."""

import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "query_route.py"
QUERY_ID = "11111111-1111-4111-8111-111111111111"
RESULT = {
    "query_id": QUERY_ID,
    "result": {"routes": [], "complete": False, "warnings": ["Partial search"]},
}


class QueryClientTests(unittest.TestCase):
    def run_client(self, *, statuses, create_status=202, arguments=()):
        requests = []
        remaining_statuses = list(statuses)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def respond(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, body, dict(self.headers)))
                if create_status != 202:
                    self.respond(create_status, {"detail": "Invalid route request"})
                    return
                self.respond(
                    202, {"query_id": QUERY_ID, "status": "queued", "reused": False}
                )

            def do_GET(self):
                requests.append(("GET", self.path, None, {}))
                if self.path.endswith("/result"):
                    if remaining_statuses[0] == "failed":
                        self.respond(
                            409,
                            {"detail": {"code": "planner_error", "message": "Failed"}},
                        )
                    else:
                        self.respond(200, RESULT)
                    return
                current = remaining_statuses[0]
                if len(remaining_statuses) > 1:
                    remaining_statuses.pop(0)
                self.respond(200, {"query_id": QUERY_ID, "status": current})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}",
                    "--poll-interval",
                    "0.01",
                    "--timeout",
                    "3",
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        return completed, requests

    def test_waits_for_completion_and_preserves_partial_result(self):
        completed, requests = self.run_client(
            statuses=["queued", "running", "completed"]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), RESULT)
        self.assertIn(QUERY_ID, completed.stderr)
        self.assertIn("Статус: running", completed.stderr)
        post = requests[0]
        self.assertEqual(post[:2], ("POST", "/api/v1/queries"))
        self.assertEqual(post[2]["origin"], "MOW")
        self.assertEqual(post[2]["destination"], "TYO")
        self.assertEqual(
            str(UUID(post[3]["Idempotency-Key"])), post[3]["Idempotency-Key"]
        )
        self.assertEqual(sum(request[0] == "POST" for request in requests), 1)

    def test_failed_query_prints_backend_error_and_exits(self):
        completed, _ = self.run_client(statuses=["failed"])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            json.loads(completed.stdout)["detail"]["code"], "planner_error"
        )
        self.assertIn("HTTP 409", completed.stderr)

    def test_timeout_reports_query_id_and_worker_hint(self):
        completed, requests = self.run_client(
            statuses=["queued"], arguments=("--timeout", "0.1")
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertIn("worker", completed.stderr)
        self.assertIn(f"--query-id {QUERY_ID}", completed.stderr)
        self.assertFalse(any(request[1].endswith("/result") for request in requests))

    def test_resume_does_not_submit_another_query(self):
        completed, requests = self.run_client(
            statuses=["completed"], arguments=("--query-id", QUERY_ID)
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), RESULT)
        self.assertTrue(all(request[0] == "GET" for request in requests))

    def test_invalid_request_prints_backend_response_without_polling(self):
        completed, requests = self.run_client(statuses=["queued"], create_status=422)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            json.loads(completed.stdout), {"detail": "Invalid route request"}
        )
        self.assertIn("HTTP 422", completed.stderr)
        self.assertEqual(len(requests), 1)
