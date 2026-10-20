"""HTTP boundary regressions, driven directly through ASGI."""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from ubas_backend.main import create_app
from ubas_backend.query_manager.ports import QueryRepository
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.policy import RequestPolicy
from ubas_backend.settings import Settings


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, *, key=None, date_to="2027-06-04", currency="RUB"):
        app = create_app()
        repo = AsyncMock(spec=QueryRepository)
        app.state.query_manager = QueryManager(repo, RequestPolicy(PlannerConfig()))
        body = json.dumps(
            {
                "origin": "MOW",
                "destination": "TYO",
                "date_from": "2027-06-01",
                "date_to": date_to,
                "currency": currency,
            }
        ).encode()
        headers = [(b"content-type", b"application/json")]
        if key is not None:
            headers.append((b"idempotency-key", key.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/queries",
            "raw_path": b"/api/v1/queries",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "server": ("localhost", 8000),
            "client": ("localhost", 123),
        }
        messages = []

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        repo.submit.assert_not_called()
        return next(m["status"] for m in messages if m["type"] == "http.response.start")

    async def test_empty_idempotency_key_is_422(self):
        self.assertEqual(await self.post(key=""), 422)

    async def test_excessive_date_range_is_422_before_enqueue(self):
        self.assertEqual(await self.post(date_to="2027-08-01"), 422)

    async def test_unconfigured_currency_is_422(self):
        self.assertEqual(await self.post(currency="USD"), 422)

    def test_embedded_worker_loads_catalog_but_migrations_do_not_require_it(self):
        with patch.dict(
            os.environ,
            {
                "UBAS_DATABASE_URL": "postgresql://unused",
                "UBAS_CATALOG_PATH": "./catalog.sqlite3",
                "UBAS_EMBEDDED_WORKER": "true",
                "UBAS_FLIGHT_PROVIDER_FACTORY": "module:factory",
            },
            clear=True,
        ):
            self.assertIsNotNone(
                Settings.from_environment(need_planner=None).catalog_path
            )
            self.assertIsNone(
                Settings.from_environment(need_planner=False).catalog_path
            )
