"""Distributed scraper nodes: HTTP contract, balancing, retries and bounds."""

import asyncio
import json
import os
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.node_api import create_node_app
from ubas_backend.agent_scraper.nodes import NodePool, NodeSearchService
from ubas_backend.agent_scraper.provider import (
    AgentFlightProvider,
    create_flight_provider,
)
from ubas_backend.agent_scraper.scraper_agent import TransientScrapeError
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.ports import FlightProviderError

NOW = datetime.now(UTC)
DAY = date(2026, 11, 18)


def captured() -> CapturedOffers:
    return CapturedOffers(
        fetched_at=NOW,
        data={"status": "Ready", "currency": "RUB", "offers": [], "ow_offers": []},
    )


def request(**changes: object) -> SearchRequest:
    return SearchRequest.model_validate(
        {"origin": "SVX", "destination": "AYT", "depart_date": DAY, **changes}
    )


def node_settings(**changes: object) -> Settings:
    defaults: dict[str, object] = {
        "redis_url": None,
        "node_urls": ("http://node-a:8101", "http://node-b:8102"),
        "node_concurrency": 1,
        "node_max_attempts": 2,
        "node_timeout_seconds": 5,
    }
    return Settings.model_validate({**defaults, **changes})


def mock_transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


class NodeSettingsTests(unittest.TestCase):
    def test_urls_are_split_normalized_and_validated(self):
        settings = node_settings(
            node_urls=" http://127.0.0.1:8101 , https://node-b:8443/ "
        )
        self.assertEqual(
            settings.node_urls,
            ("http://127.0.0.1:8101", "https://node-b:8443"),
        )
        self.assertEqual(node_settings(node_urls="").node_urls, ())
        self.assertIsNone(node_settings(node_token="").node_token)
        for invalid in (
            "ftp://node-a",
            "node-a:8101",
            "http://node-a:8101/path",
            "http://node-a:8101/?token=x",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                node_settings(node_urls=invalid)

    def test_node_pool_requires_at_least_one_url(self):
        with self.assertRaises(ValueError):
            NodePool(node_settings(node_urls=()))


class NodeAppTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, app) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://node"
        )

    async def test_search_returns_captured_offers_and_health(self):
        class Service:
            calls: list[SearchRequest] = []

            async def get_offers(self, body: SearchRequest) -> CapturedOffers:
                self.calls.append(body)
                return captured()

            async def aclose(self) -> None:
                pass

        service = Service()
        app = create_node_app(node_settings(), service)
        async with (
            app.router.lifespan_context(app),
            await self.client(app) as client,
        ):
            health = await client.get("/healthz")
            self.assertEqual(health.json(), {"status": "ok"})
            response = await client.post(
                "/v1/search", json=request().model_dump(mode="json")
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CapturedOffers.model_validate(response.json()).fetched_at, NOW)
        self.assertEqual(service.calls, [request()])

    async def test_source_errors_have_stable_statuses(self):
        class Service:
            def __init__(self, error: Exception) -> None:
                self.error = error

            async def get_offers(self, body: SearchRequest) -> CapturedOffers:
                raise self.error

            async def aclose(self) -> None:
                pass

        for error, expected in (
            (TransientScrapeError("agent_search_failed"), 503),
            (FlightProviderError("agent_unsupported_year"), 422),
        ):
            with self.subTest(error=error):
                app = create_node_app(node_settings(), Service(error))
                async with (
                    app.router.lifespan_context(app),
                    await self.client(app) as client,
                ):
                    response = await client.post(
                        "/v1/search", json=request().model_dump(mode="json")
                    )
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.json()["detail"], str(error))

    async def test_optional_shared_token_is_enforced(self):
        class Service:
            async def get_offers(self, body: SearchRequest) -> CapturedOffers:
                return captured()

            async def aclose(self) -> None:
                pass

        settings = node_settings(node_token="shared")
        app = create_node_app(settings, Service())
        async with (
            app.router.lifespan_context(app),
            await self.client(app) as client,
        ):
            payload = request().model_dump(mode="json")
            denied = await client.post("/v1/search", json=payload)
            allowed = await client.post(
                "/v1/search", json=payload, headers={"X-UBAS-Node-Token": "shared"}
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)


class NodePoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pairs_are_spread_over_nodes_with_bounded_concurrency(self):
        active = {"node-a": 0, "node-b": 0}
        peak = {"node-a": 0, "node-b": 0}
        calls = {"node-a": 0, "node-b": 0}

        async def handler(http_request: httpx.Request) -> httpx.Response:
            host = http_request.url.host
            calls[host] += 1
            active[host] += 1
            peak[host] = max(peak[host], active[host])
            await asyncio.sleep(0.05)
            active[host] -= 1
            return httpx.Response(
                200, content=captured().model_dump_json().encode("utf-8")
            )

        settings = node_settings()
        pool = NodePool(settings, transport=mock_transport(handler))
        try:
            results = await asyncio.gather(
                *(
                    pool.scrape(
                        request(depart_date=DAY + timedelta(days=index)), settings
                    )
                    for index in range(4)
                )
            )
        finally:
            await pool.aclose()
        self.assertEqual(len(results), 4)
        self.assertEqual(calls, {"node-a": 2, "node-b": 2})
        self.assertEqual(peak, {"node-a": 1, "node-b": 1})

    async def test_transient_failure_retries_on_another_node(self):
        calls = {"node-a": 0, "node-b": 0}

        async def handler(http_request: httpx.Request) -> httpx.Response:
            host = http_request.url.host
            calls[host] += 1
            if host == "node-a":
                return httpx.Response(503, json={"detail": "agent_search_failed"})
            return httpx.Response(
                200, content=captured().model_dump_json().encode("utf-8")
            )

        settings = node_settings()
        pool = NodePool(settings, transport=mock_transport(handler))
        try:
            result = await pool.scrape(request(), settings)
        finally:
            await pool.aclose()
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(calls, {"node-a": 1, "node-b": 1})

    async def test_exhausted_transient_and_permanent_failures_are_distinct(self):
        async def unavailable(http_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        settings = node_settings()
        pool = NodePool(settings, transport=mock_transport(unavailable))
        try:
            with self.assertRaises(TransientScrapeError):
                await pool.scrape(request(), settings)
        finally:
            await pool.aclose()

        calls = 0

        async def rejected(http_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(422, json={"detail": "agent_unsupported_year"})

        pool = NodePool(settings, transport=mock_transport(rejected))
        try:
            with self.assertRaises(FlightProviderError):
                await pool.scrape(request(), settings)
        finally:
            await pool.aclose()
        self.assertEqual(calls, 1)

    async def test_invalid_payload_is_permanent(self):
        calls = 0

        async def invalid(http_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=b'{"status": "Ready"}')

        settings = node_settings()
        pool = NodePool(settings, transport=mock_transport(invalid))
        try:
            with self.assertRaises(FlightProviderError):
                await pool.scrape(request(), settings)
        finally:
            await pool.aclose()
        self.assertEqual(calls, 1)

    async def test_transport_timeout_is_transient(self):
        calls = 0

        async def timeout(http_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("slow", request=http_request)

        settings = node_settings()
        pool = NodePool(settings, transport=mock_transport(timeout))
        try:
            with self.assertRaises(TransientScrapeError):
                await pool.scrape(request(), settings)
        finally:
            await pool.aclose()
        self.assertEqual(calls, 2)


class NodeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_pairs_share_coordinator_cache(self):
        calls = 0

        async def handler(http_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, content=captured().model_dump_json().encode("utf-8")
            )

        settings = node_settings(node_urls=("http://node-a:8101",))
        service = NodeSearchService(settings, transport=mock_transport(handler))
        try:
            results = await asyncio.gather(
                service.get_offers(request()), service.get_offers(request())
            )
        finally:
            await service.aclose()
        self.assertEqual(results[0].fetched_at, NOW)
        self.assertEqual(calls, 1)


class NodePayloadTests(unittest.TestCase):
    def test_captured_payload_is_json_roundtrippable(self):
        payload = json.loads(captured().model_dump_json())
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["data"]["status"], "Ready")


class FactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_switches_between_local_and_node_services(self):
        with patch.dict(os.environ, {"UBAS_AGENT_NODE_URLS": ""}):
            local = create_flight_provider()
            try:
                self.assertIsInstance(local._service, FlightSearchService)
            finally:
                await local.aclose()

        with patch.dict(
            os.environ,
            {"UBAS_AGENT_NODE_URLS": ("http://127.0.0.1:8101,http://127.0.0.1:8102")},
        ):
            distributed = create_flight_provider()
            try:
                self.assertIsInstance(distributed, AgentFlightProvider)
                self.assertIsInstance(distributed._service, NodeSearchService)
            finally:
                await distributed.aclose()
