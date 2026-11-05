"""Source-shaped regressions; tests never contact agent.ru or a real Redis."""

import asyncio
import copy
import json
import os
import tempfile
import unittest
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from ubas_backend.agent_scraper.airports import airport_for_code, airports_for_city
from ubas_backend.agent_scraper.cache import ResponseCache
from ubas_backend.agent_scraper.catalog import create_example
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.provider import AgentFlightProvider, _next_occurrence
from ubas_backend.agent_scraper.scraper_agent import (
    ResponseAccumulator,
    run_agent_scraper,
)
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.offers import TicketSearch
from ubas_backend.routing.planner import IncompleteSearchError
from ubas_backend.routing.ports import FlightProviderError
from ubas_backend.routing.schemas import CityProfile, FlightQuery, RouteRequest
from ubas_backend.routing.storage import (
    CatalogData,
    SqliteCatalog,
    SqliteCityRepository,
)
from ubas_backend.runtime import create_planner
from ubas_backend.settings import Settings as RuntimeSettings

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
DAY = date(2026, 11, 18)


def pages():
    return json.loads(
        (Path(__file__).parent / "fixtures/agent-offers.json").read_text("utf-8")
    )


def page_url(index=0):
    token = f"&next_token=page-{index}" if index else ""
    return f"https://www.agent.ru/api/air/offers?request_id=fixture-search{token}"


def captured():
    accumulator = ResponseAccumulator()
    for index, page in enumerate(pages()):
        accumulator.add(page_url(index), page)
    return CapturedOffers(fetched_at=NOW, data=accumulator.data)


def request(**changes):
    return SearchRequest.model_validate(
        {"origin": "SVX", "destination": "AYT", "depart_date": DAY, **changes}
    )


def query(**changes):
    return FlightQuery.model_validate(
        {
            "origin": "SVX",
            "destination": "AYT",
            "departure_from": DAY,
            "departure_to": DAY,
            "passengers": 1,
            "limit": 100,
            **changes,
        }
    )


def empty():
    return CapturedOffers(
        fetched_at=datetime.now(UTC),
        data={"status": "Ready", "currency": "RUB", "offers": [], "ow_offers": []},
    )


def one_ticket():
    value = captured()
    group = value.data["offers"][0]
    group["offers"] = group["offers"][:1]
    group["offers"][0]["segments"] = group["offers"][0]["segments"][:1]
    value.data["offers"] = [group]
    return value


def raw_ticket(value):
    return value.data["offers"][0]["offers"][0]["segments"][0]


class MappingTests(unittest.TestCase):
    def test_live_shape_preserves_individual_price_and_all_flights(self):
        result = map_response(captured(), request())
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(len(result.offers), 20)
        self.assertEqual(
            [offer.total_price.amount for offer in result.offers[:2]],
            [Decimal(17846), Decimal(40637)],
        )
        first = result.offers[0]
        self.assertEqual(len(first.segments), 2)
        self.assertEqual(first.segments[0].destination.city_code, "MOW")
        self.assertEqual(first.segments[1].origin.code, "VKO")
        self.assertEqual(first.segments[0].departure_at.utcoffset(), timedelta(hours=5))
        self.assertEqual(first.fetched_at, NOW)
        self.assertIsNone(first.expires_at)
        self.assertFalse(first.checked_baggage_included)
        self.assertNotIn("fixture-search", first.offer_id)
        self.assertEqual(len({offer.offer_id for offer in result.offers}), 20)

    def test_four_flights_stay_one_ticket(self):
        value = one_ticket()
        raw = raw_ticket(value)
        legs = (("SVX", "VKO"), ("VKO", "IST"), ("IST", "DXB"), ("DXB", "AYT"))
        template = raw["flights_info"][0]
        raw["flights_info"] = []
        for index, (origin, destination) in enumerate(legs):
            flight = copy.deepcopy(template)
            flight.update(
                departure_airport=origin,
                arrival_airport=destination,
                departure_date=(DAY + timedelta(days=index)).isoformat(),
                arrival_date=(DAY + timedelta(days=index)).isoformat(),
                departure_local_time="06:00",
                arrival_local_time="13:00",
            )
            raw["flights_info"].append(flight)
        result = map_response(value, request())
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(len(result.offers), 1)
        self.assertEqual(len(result.offers[0].segments), 4)
        self.assertEqual(result.offers[0].total_price.amount, Decimal(17846))

    def test_missing_price_is_unknown_not_group_minimum(self):
        value = one_ticket()
        del raw_ticket(value)["price"]
        result = map_response(value, request())
        self.assertTrue(result.complete)
        self.assertIsNone(result.offers[0].total_price)
        self.assertIn("ticket_price", result.offers[0].unknown_costs)

    def test_duplicate_alternatives_are_retained(self):
        value = one_ticket()
        value.data["offers"][0]["offers"][0]["segments"].append(
            copy.deepcopy(raw_ticket(value))
        )
        result = map_response(value, request())
        self.assertEqual(len(result.offers), 2)
        self.assertNotEqual(result.offers[0].offer_id, result.offers[1].offer_id)

    def test_invalid_ticket_never_becomes_successful_empty_result(self):
        mutations = (
            ("departure_airport", "ZZZ"),
            ("arrival_date", None),
            ("arrival_local_time", "00:00"),
            ("operating_airline_code", None),
            ("service_class", "Business"),
            ("techstop", True),
        )
        for key, val in mutations:
            with self.subTest(key=key):
                value = one_ticket()
                raw_ticket(value)["flights_info"][0][key] = val
                result = map_response(value, request())
                self.assertFalse(result.complete)
                self.assertEqual(result.offers, ())

    def test_bad_price_passengers_and_date_mark_incomplete(self):
        for key, val in (
            ("price", "-1"),
            ("price_details", [{"passenger_type": "adult", "qty": 2}]),
        ):
            value = one_ticket()
            raw_ticket(value)[key] = val
            self.assertFalse(map_response(value, request()).complete)
        self.assertFalse(
            map_response(
                one_ticket(), request(depart_date=DAY + timedelta(days=1))
            ).complete
        )

    def test_ow_offers_and_empty_ready(self):
        value = one_ticket()
        value.data["ow_offers"] = value.data.pop("offers")
        value.data["offers"] = []
        self.assertEqual(len(map_response(value, request()).offers), 1)
        self.assertTrue(map_response(empty(), request()).complete)
        self.assertFalse(
            map_response(CapturedOffers(fetched_at=NOW, data={}), request()).complete
        )


class PaginationTests(unittest.TestCase):
    def test_complete_chain_and_exact_replay(self):
        accumulator = ResponseAccumulator()
        packets = pages()
        self.assertFalse(accumulator.add(page_url(), packets[0]))
        self.assertFalse(accumulator.add(page_url(1), packets[1]))
        count = len(accumulator.data["offers"])
        self.assertFalse(accumulator.add(page_url(1), packets[1]))
        self.assertEqual(len(accumulator.data["offers"]), count)
        self.assertTrue(accumulator.add(page_url(2), packets[2]))
        self.assertEqual(accumulator.data["status"], "Ready")

    def test_missing_page_conflicting_replay_and_cross_search_rejected(self):
        packets = pages()
        with self.assertRaises(FlightProviderError):
            ResponseAccumulator().add(page_url(1), packets[1])
        accumulator = ResponseAccumulator()
        accumulator.add(page_url(), packets[0])
        with self.assertRaises(FlightProviderError):
            accumulator.add(page_url(), {**packets[0], "currency": "USD"})
        with self.assertRaises(FlightProviderError):
            accumulator.add(page_url(1), {**packets[1], "request_id": "different"})


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_date_keys_singleflight_and_original_timestamp(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        value = empty()

        async def scrape(req, settings):
            calls.append(req)
            started.set()
            await release.wait()
            return value

        service = FlightSearchService(settings=Settings(redis_url=None), scrape=scrape)
        first = asyncio.create_task(service.get_offers(request()))
        await started.wait()
        second = asyncio.create_task(service.get_offers(request()))
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await first, await second)
        self.assertEqual(len(calls), 1)
        again = await service.get_offers(request())
        self.assertEqual(again.fetched_at, value.fetched_at)
        await service.get_offers(request(depart_date=DAY + timedelta(days=1)))
        self.assertEqual(len(calls), 2)
        self.assertFalse(service._flights)
        await service.aclose()

    async def test_cancellation_stops_owner_and_waiter_can_retry(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        calls = 0

        async def scrape(req, settings):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return empty()

        service = FlightSearchService(settings=Settings(redis_url=None), scrape=scrape)
        owner = asyncio.create_task(service.get_offers(request()))
        await started.wait()
        waiter = asyncio.create_task(service.get_offers(request()))
        await asyncio.sleep(0)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await asyncio.wait_for(waiter, 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(calls, 2)
        self.assertFalse(service._flights)
        await service.aclose()

    async def test_cancelled_waiter_does_not_cancel_owner(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def scrape(req, settings):
            started.set()
            await release.wait()
            return empty()

        service = FlightSearchService(settings=Settings(redis_url=None), scrape=scrape)
        owner = asyncio.create_task(service.get_offers(request()))
        await started.wait()
        waiter = asyncio.create_task(service.get_offers(request()))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        await owner
        self.assertFalse(service._flights)
        await service.aclose()

    async def test_concurrency_is_bounded(self):
        active = peak = 0
        release, started = asyncio.Event(), asyncio.Event()

        async def scrape(req, settings):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
            try:
                await release.wait()
                return empty()
            finally:
                active -= 1

        service = FlightSearchService(
            settings=Settings(redis_url=None, max_concurrency=2), scrape=scrape
        )
        tasks = [
            asyncio.create_task(
                service.get_offers(request(depart_date=DAY + timedelta(days=index)))
            )
            for index in range(5)
        ]
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)
        await service.aclose()


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_future_values_eviction_and_nonready(self):
        cache = ResponseCache(
            Settings(redis_url=None, cache_ttl_seconds=60, cache_max_entries=1)
        )
        old = empty().model_copy(
            update={"fetched_at": datetime.now(UTC) - timedelta(days=1)}
        )
        await cache.set("old", old)
        self.assertIsNone(await cache.get("old"))
        future = empty().model_copy(
            update={"fetched_at": datetime.now(UTC) + timedelta(days=1)}
        )
        await cache.set("future", future)
        self.assertIsNone(await cache.get("future"))
        await cache.set("one", empty())
        await cache.set("two", empty())
        self.assertIsNone(await cache.get("one"))
        self.assertIsNotNone(await cache.get("two"))
        unfinished = empty()
        unfinished.data["status"] = "InProcess"
        await cache.set("unfinished", unfinished)
        self.assertIsNone(await cache.get("unfinished"))
        await cache.aclose()

    async def test_optional_redis_failure_falls_back_to_memory_and_closes(self):
        client = AsyncMock()
        client.get.side_effect = RedisConnectionError("offline")
        client.set.side_effect = RedisConnectionError("offline")
        with patch(
            "ubas_backend.agent_scraper.cache.Redis.from_url", return_value=client
        ):
            cache = ResponseCache(Settings(redis_url="redis://localhost"))
            self.assertIsNone(await cache.get("key"))
            value = empty()
            await cache.set("key", value)
            self.assertEqual((await cache.get("key")).fetched_at, value.fetched_at)
            await cache.aclose()
        client.aclose.assert_awaited_once()

    async def test_unversioned_redis_data_is_ignored(self):
        client = AsyncMock()
        client.get.return_value = '{"status":"Ready","offers":[]}'
        with patch(
            "ubas_backend.agent_scraper.cache.Redis.from_url", return_value=client
        ):
            cache = ResponseCache(Settings(redis_url="redis://localhost"))
            self.assertIsNone(await cache.get("key"))
            await cache.aclose()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch("ubas_backend.agent_scraper.provider.datetime")
        self.clock = clock.start()
        self.clock.now.return_value = NOW
        self.addCleanup(clock.stop)

    async def test_ranges_are_refined_to_every_day_by_planner(self):
        service = SimpleNamespace(get_offers=AsyncMock(return_value=empty()))
        provider = AgentFlightProvider(service)
        cfg = PlannerConfig()
        diagnostics = SearchDiagnostics()
        q = query(departure_to=DAY + timedelta(days=2))
        req = RouteRequest(
            origin="SVX",
            destination="AYT",
            date_from=q.departure_from,
            date_to=q.departure_to,
        )
        result = await TicketSearch(provider, cfg).collect(
            (q,), req, diagnostics, now=NOW
        )
        self.assertTrue(diagnostics.complete)
        self.assertEqual(result, ())
        self.assertEqual(
            {call.args[0].depart_date for call in service.get_offers.call_args_list},
            {DAY + timedelta(days=i) for i in range(3)},
        )

    async def test_city_expands_airports_and_error_cancels_siblings(self):
        service = SimpleNamespace(get_offers=AsyncMock(return_value=empty()))
        result = await AgentFlightProvider(service).search(query(origin="MOW"))
        self.assertTrue(result.complete)
        self.assertEqual(
            {call.args[0].origin for call in service.get_offers.call_args_list},
            {"SVO", "DME", "VKO", "ZIA"},
        )
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def search(req):
            if req.origin == "SVO":
                await started.wait()
                raise FlightProviderError("offline")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(FlightProviderError):
            await AgentFlightProvider(SimpleNamespace(get_offers=search)).search(
                query(origin="MOW")
            )
        self.assertTrue(cancelled.is_set())

    async def test_truncation_and_mapping_failure_mark_incomplete(self):
        service = SimpleNamespace(get_offers=AsyncMock(return_value=captured()))
        result = await AgentFlightProvider(service).search(query(limit=1))
        self.assertEqual(len(result.offers), 1)
        self.assertFalse(result.complete)
        self.assertIn("agent_offer_limit", result.warnings)
        result = await AgentFlightProvider(service).search(
            query(
                departure_from=DAY + timedelta(days=1),
                departure_to=DAY + timedelta(days=1),
            )
        )
        self.assertFalse(result.complete)

    async def test_unsupported_city_year_passengers_and_past(self):
        service = SimpleNamespace(get_offers=AsyncMock(return_value=empty()))
        provider = AgentFlightProvider(service)
        for q in (
            query(origin="ZZZ"),
            query(passengers=2),
            query(departure_from=date(2028, 11, 18), departure_to=date(2028, 11, 18)),
        ):
            with self.assertRaises(FlightProviderError):
                await provider.search(q)
        result = await provider.search(
            query(departure_from=date(2026, 1, 1), departure_to=date(2026, 1, 1))
        )
        self.assertTrue(result.complete)
        service.get_offers.assert_not_awaited()


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_and_timeout_close_browser_without_ready(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                started = asyncio.Event()
                page = MagicMock()

                async def goto(*args, event=started, **kwargs):
                    event.set()

                page.goto = AsyncMock(side_effect=goto)
                context = SimpleNamespace(new_page=AsyncMock(return_value=page))
                browser = SimpleNamespace(
                    new_context=AsyncMock(return_value=context), close=AsyncMock()
                )
                playwright = SimpleNamespace(
                    chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
                )
                manager = AsyncMock()
                manager.__aenter__.return_value = playwright
                with patch(
                    "ubas_backend.agent_scraper.scraper_agent.async_playwright",
                    return_value=manager,
                ):
                    task = asyncio.create_task(
                        run_agent_scraper(request(), Settings(timeout_seconds=0.05))
                    )
                    await started.wait()
                    if cancel:
                        task.cancel()
                    with self.assertRaises(
                        asyncio.CancelledError if cancel else FlightProviderError
                    ):
                        await task
                browser.close.assert_awaited_once()


class ModelAndCatalogTests(unittest.TestCase):
    def test_request_date_validation_key_and_year_boundary(self):
        req = request(origin=" svx ", depart_date="2026-11-18")
        self.assertIn("1811svxayt%7Ca1E", req.search_url)
        self.assertIn("2026-11-18:E:1:0:0:RUB", req.cache_key)
        for day in ("2026-02-30", "not-a-date"):
            with self.assertRaises(ValidationError):
                request(depart_date=day)
        self.assertEqual(
            _next_occurrence(date(2027, 1, 1), date(2026, 12, 31)), date(2027, 1, 1)
        )

    def test_airport_registry_timezones_and_city_identity(self):
        self.assertEqual(airport_for_code("LED").timezone, "Europe/Moscow")
        self.assertIsNone(airport_for_code("MOW"))
        self.assertEqual({a.code for a in airports_for_city("TYO")}, {"NRT", "HND"})
        self.assertEqual({a.code for a in airports_for_city("GOI")}, {"GOI", "GOX"})
        self.assertEqual({a.code for a in airports_for_city("NQZ")}, {"NQZ"})
        self.assertEqual({a.code for a in airports_for_city("BAK")}, {"GYD"})
        self.assertEqual(airport_for_code("GYD").city_code, "BAK")
        self.assertEqual(airport_for_code("TSE").city_code, "NQZ")
        self.assertEqual(airports_for_city("GYD"), ())
        from ubas_backend.agent_scraper.airports import _AIRPORTS

        for code in _AIRPORTS:
            self.assertIsNotNone(airport_for_code(code))

    def test_example_seed_never_overwrites_and_does_not_invent_visas(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            create_example(path)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                create_example(path)
            self.assertEqual(path.read_bytes(), before)
            repository = SqliteCityRepository(path)
            try:
                # Read directly via the repository, without textual SQL.
                cities = asyncio.run(repository.get_many(("SVX", "MOW", "ALA")))
                self.assertEqual(len(cities), 3)
                for city in cities:
                    self.assertEqual(city.tourism_score, 0)
                    self.assertEqual(city.visas, ())
            finally:
                repository.close()


class PlannerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_factory_source_mapping_and_planner_result(self):
        # Replace only browser I/O. Use the real factory, service, mapping,
        # SQLite readers, date refinement and planner, including failure handling.
        for malformed in (False, True):
            with (
                self.subTest(malformed=malformed),
                tempfile.TemporaryDirectory() as folder,
            ):
                path = Path(folder) / "catalog.sqlite3"
                catalog = SqliteCatalog(path)
                try:
                    catalog.initialize()
                    catalog.upsert(
                        CatalogData(
                            cities=(
                                CityProfile(
                                    city_code="SVX",
                                    name="Test origin",
                                    country_code="RU",
                                    timezone="Asia/Yekaterinburg",
                                    tourism_score=0,
                                ),
                            ),
                            links=(),
                        )
                    )
                finally:
                    catalog.close()
                calls = []

                async def scrape(req, settings, calls=calls, malformed=malformed):
                    calls.append(req)
                    value = captured() if req == request() else empty()
                    if malformed and req == request():
                        raw_ticket(value)["flights_info"][0]["arrival_date"] = None
                    return value

                cache = ResponseCache(Settings(redis_url=None))
                service = FlightSearchService(
                    settings=Settings(redis_url=None), cache=cache, scrape=scrape
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "UBAS_DATABASE_URL": "postgresql://unused",
                            "UBAS_CATALOG_PATH": str(path),
                            "UBAS_FLIGHT_PROVIDER_FACTORY": (
                                "ubas_backend.agent_scraper.provider:create_flight_provider"
                            ),
                        },
                        clear=True,
                    ),
                    patch("ubas_backend.agent_scraper.provider.datetime") as clock,
                    patch(
                        "ubas_backend.agent_scraper.provider.FlightSearchService",
                        return_value=service,
                    ),
                ):
                    clock.now.return_value = NOW
                    settings = RuntimeSettings.from_environment()
                    async with AsyncExitStack() as resources:
                        planner = create_planner(settings, resources)
                        self.assertIsInstance(
                            planner.search.provider, AgentFlightProvider
                        )
                        route_request = RouteRequest(
                            origin="SVX", destination="AYT", date_from=DAY, date_to=DAY
                        )
                        if malformed:
                            with self.assertRaises(IncompleteSearchError):
                                await planner.plan(route_request, now=NOW)
                        else:
                            result = await planner.plan(route_request, now=NOW)
                            self.assertTrue(result.complete)
                            self.assertGreater(len(result.routes), 0)
                            prices = [r.route.total_cost.amount for r in result.routes]
                            self.assertEqual(prices, sorted(prices))
                            for ranked in result.routes:
                                route = ranked.route
                                self.assertEqual(len(route.tickets), 1)
                                ticket = route.tickets[0]
                                self.assertEqual(ticket.provider, "agent-ru")
                                self.assertEqual(ticket.fetched_at, NOW)
                                self.assertEqual(route.ticket_cost, ticket.total_price)
                                self.assertLessEqual(len(ticket.segments), 4)
                    self.assertFalse(cache._entries)
                    self.assertIn(request(), calls)


if __name__ == "__main__":
    unittest.main()
