"""Browser preflight and response headers for the public query API."""

import json
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

from ubas_backend.main import create_app
from ubas_backend.query_manager.ports import QueryRepository
from ubas_backend.query_manager.schemas import GetQueryResponse, SubmitQueryResponse
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.policy import RequestPolicy

QUERY_ID = UUID("11111111-1111-4111-8111-111111111111")
ORIGIN = "http://localhost:5173"
PREFLIGHT = {
    "Origin": ORIGIN,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type,idempotency-key",
}


class CorsTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, method, headers, *, body=None, path="/api/v1/queries"):
        app = create_app()
        repo = AsyncMock(spec=QueryRepository)
        repo.submit.return_value = SubmitQueryResponse(
            query_id=QUERY_ID, status="queued", reused=False
        )
        now = datetime.now(UTC)
        repo.get.return_value = GetQueryResponse(
            query_id=QUERY_ID,
            status="queued",
            created_at=now,
            updated_at=now,
            attempt=0,
            result_available=False,
        )
        app.state.query_manager = QueryManager(repo, RequestPolicy(PlannerConfig()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
            "server": ("localhost", 8000),
            "client": ("localhost", 123),
        }
        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": json.dumps(body).encode() if body is not None else b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        response_headers = {
            key.decode(): value.decode() for key, value in start["headers"]
        }
        return start["status"], response_headers

    async def test_local_frontends_can_preflight_json_and_idempotency_key(self):
        with patch.dict(os.environ, {}, clear=True):
            for origin in (ORIGIN, "http://127.0.0.1:5173"):
                status, headers = await self.request(
                    "OPTIONS", {**PREFLIGHT, "Origin": origin}
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["access-control-allow-origin"], origin)
                self.assertIn("POST", headers["access-control-allow-methods"])
                self.assertIn(
                    "idempotency-key", headers["access-control-allow-headers"].lower()
                )

    async def test_unlisted_origin_is_not_allowed(self):
        status, headers = await self.request(
            "OPTIONS", {**PREFLIGHT, "Origin": "https://other.example"}
        )
        self.assertEqual(status, 400)
        self.assertNotIn("access-control-allow-origin", headers)

    async def test_unsupported_method_and_header_are_not_allowed(self):
        for override in (
            {"Access-Control-Request-Method": "DELETE"},
            {"Access-Control-Request-Headers": "authorization"},
        ):
            status, _ = await self.request("OPTIONS", {**PREFLIGHT, **override})
            self.assertEqual(status, 400)

    async def test_post_success_and_validation_error_have_cors_headers(self):
        request = {
            "origin": "MOW",
            "destination": "TYO",
            "date_from": "2027-06-01",
            "date_to": "2027-06-04",
        }
        for body, expected in ((request, 202), ({}, 422)):
            status, headers = await self.request(
                "POST",
                {"Origin": ORIGIN, "Content-Type": "application/json"},
                body=body,
            )
            self.assertEqual(status, expected)
            self.assertEqual(headers["access-control-allow-origin"], ORIGIN)
            self.assertIn("Location", headers["access-control-expose-headers"])
            self.assertNotIn("access-control-allow-credentials", headers)
            if expected == 202:
                self.assertEqual(headers["location"], f"/api/v1/queries/{QUERY_ID}")

    async def test_pending_result_exposes_retry_after(self):
        status, headers = await self.request(
            "GET", {"Origin": ORIGIN}, path=f"/api/v1/queries/{QUERY_ID}/result"
        )
        self.assertEqual(status, 202)
        self.assertEqual(headers["retry-after"], "2")
        self.assertEqual(headers["access-control-allow-origin"], ORIGIN)
        self.assertIn("Retry-After", headers["access-control-expose-headers"])

    async def test_custom_origins_replace_defaults_and_empty_disables_cors(self):
        custom = "https://frontend.example"
        with patch.dict(os.environ, {"UBAS_CORS_ORIGINS": f" {custom}/, "}):
            status, headers = await self.request(
                "OPTIONS", {**PREFLIGHT, "Origin": custom}
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers["access-control-allow-origin"], custom)
            status, headers = await self.request("OPTIONS", PREFLIGHT)
            self.assertEqual(status, 400)
            self.assertNotIn("access-control-allow-origin", headers)
        with patch.dict(os.environ, {"UBAS_CORS_ORIGINS": ""}):
            status, headers = await self.request("OPTIONS", PREFLIGHT)
            self.assertEqual(status, 400)
            self.assertNotIn("access-control-allow-origin", headers)
