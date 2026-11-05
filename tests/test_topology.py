"""Topology collection, transactional replacement and exhaustive hop checks."""

import asyncio
import json
import random
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.sql.dml import Insert

from ubas_backend.routing.catalog_models import FlightLinkRow
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.hubs import discover_cities
from ubas_backend.routing.schemas import CityProfile, RouteRequest
from ubas_backend.routing.storage import (
    CatalogData,
    FlightLink,
    SqliteCatalog,
    SqliteCityRepository,
    SqliteTopologyRepository,
)
from ubas_backend.routing.topology import cities_on_paths
from ubas_backend.topology.__main__ import main, save_snapshot
from ubas_backend.topology.collector import collect
from ubas_backend.topology.geography import Geography
from ubas_backend.topology.models import Destination, Observation, Snapshot
from ubas_backend.topology.source import (
    FlyWhereTo,
    SourceError,
    parse_destinations,
    read_body,
    rpc_result,
)


def geography():
    airports = [
        {
            "code": airport,
            "city_code": city,
            "iata_type": "airport",
            "time_zone": "UTC",
            "flightable": True,
        }
        for airport, city in (
            ("SVO", "MOW"),
            ("VKO", "MOW"),
            ("SVX", "SVX"),
            ("ALA", "ALA"),
        )
    ]
    cities = [
        {"code": code, "name": code, "country_code": "RU", "time_zone": "UTC"}
        for code in ("MOW", "SVX", "ALA")
    ]
    return Geography(airports, cities)


class Source:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def destinations(self, code):
        self.calls.append(code)
        if code == self.fail:
            raise SourceError("unavailable")
        targets = {"SVO": ("SVX", "ALA"), "VKO": ("SVX",), "SVX": ("SVO",)}.get(
            code, ()
        )
        return Observation(
            origin=code,
            fetched_at=datetime.now(UTC),
            destinations=tuple(
                Destination(airport=airport, kinds=("scheduled",))
                for airport in targets
            ),
        )


async def snapshot():
    return await collect(geography(), Source(), ("MOW", "SVX"))


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_city_airports_directed_deduplicated_edges(self):
        source = Source()
        result = await collect(geography(), source, ("MOW", "SVX", "MOW"))
        self.assertEqual(source.calls, ["SVO", "SVX", "VKO"])
        self.assertEqual(
            {(x.origin, x.destination) for x in result.catalog_data().links},
            {("MOW", "SVX"), ("MOW", "ALA"), ("SVX", "MOW")},
        )
        self.assertTrue(all(x.tourism_score == 0 for x in result.catalog_data().cities))
        self.assertTrue(all(x.visas == () for x in result.catalog_data().cities))

    async def test_limit_before_network_and_failure_never_returns_snapshot(self):
        source = Source()
        with self.assertRaisesRegex(ValueError, "budget"):
            await collect(geography(), source, ("MOW",), max_airports=1)
        self.assertEqual(source.calls, [])
        with self.assertRaises(SourceError):
            await collect(geography(), Source(fail="VKO"), ("MOW",))
        with self.assertRaises(ValueError):
            await collect(geography(), Source(), ("ZZZ",))

    async def test_missing_airport_in_snapshot_is_rejected(self):
        data = (await snapshot()).model_dump(mode="json")
        data["observations"] = data["observations"][:-1]
        with self.assertRaises(ValidationError):
            Snapshot.model_validate(data)

    async def test_atomic_snapshot_publication_does_not_overwrite(self):
        data = await snapshot()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            save_snapshot(data, path)
            self.assertEqual(Snapshot.model_validate_json(path.read_bytes()), data)
            with self.assertRaises(FileExistsError):
                save_snapshot(data, path)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    async def test_cancellation_reaches_source(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class SlowSource:
            async def destinations(self, code):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        task = asyncio.create_task(collect(geography(), SlowSource(), ("MOW",)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def payload(self):
        return {
            "origin": {"iata": "SVX"},
            "kindFilter": None,
            "destinationCount": 2,
            "destinations": [
                {"iata": "SVO", "kinds": ["scheduled"]},
                {"iata": "ALA", "kinds": ["seasonal", "suspended"]},
            ],
        }

    async def test_http_initialization_and_sse_result(self):
        calls = []

        def handle(request):
            message = json.loads(request.content)
            calls.append(message["method"])
            if message["method"] == "notifications/initialized":
                return httpx.Response(202)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-03-26"}
            else:
                self.assertEqual(message["params"]["arguments"]["origin"], "SVX")
                result = {
                    "content": [{"type": "text", "text": json.dumps(self.payload())}]
                }
            body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f": comment\nevent: message\ndata: {body}\n\n",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = FlyWhereTo(client, interval_seconds=0)
            await source.initialize()
            result = await source.destinations("SVX")
            self.assertEqual(len(result.destinations), 2)
            self.assertIn("suspended", result.destinations[1].kinds)
            await source.aclose()
        self.assertEqual(
            calls, ["initialize", "notifications/initialized", "tools/call"]
        )

    async def test_rate_limit_and_size_budget_fail(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "60"})
            )
        ) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await FlyWhereTo(client, interval_seconds=0).initialize()
        response = httpx.Response(
            200, content=b"12345", request=httpx.Request("GET", "https://example.com")
        )
        with self.assertRaises(SourceError):
            await read_body(response, 4)

    async def test_invalid_origin_count_filter_and_shape_are_rejected(self):
        for patch_data in (
            {"origin": None},
            {"origin": {"iata": "MOW"}},
            {"destinationCount": 1},
            {"kindFilter": "scheduled"},
            {"destinations": [None, None]},
        ):
            with self.subTest(patch_data=patch_data), self.assertRaises(SourceError):
                parse_destinations(
                    {**self.payload(), **patch_data}, "SVX", datetime.now(UTC)
                )

    async def test_unknown_service_kind_is_not_silently_dropped(self):
        data = self.payload()
        data["destinations"][0]["kinds"] = ["unknown-new-kind"]
        with self.assertRaises(ValidationError):
            parse_destinations(data, "SVX", datetime.now(UTC))

    async def test_rpc_errors_wrong_ids_and_html_not_empty_success(self):
        for message in (
            {"id": 1, "error": {}},
            {"id": 2, "result": {}},
            {"id": 1, "result": {"isError": True}},
        ):
            with self.assertRaises(SourceError):
                rpc_result(json.dumps(message).encode(), "application/json", 1)
        with self.assertRaises(SourceError):
            rpc_result(b"<html>denied</html>", "text/html", 1)
        body = b'data: {"id": 1,\ndata: "result": {"value": 2}}\n\n'
        self.assertEqual(rpc_result(body, "text/event-stream", 1), {"value": 2})


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_replace_covered_origins_preserves_profiles_and_other_links(self):
        result = await snapshot()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            catalog = SqliteCatalog(path)
            catalog.initialize()
            existing = CityProfile(
                city_code="MOW",
                name="Existing",
                country_code="RU",
                timezone="Europe/Moscow",
                tourism_score=85,
            )
            catalog.upsert(
                CatalogData(
                    cities=(existing,),
                    links=(
                        FlightLink(origin="MOW", destination="ZZZ"),
                        FlightLink(origin="ALA", destination="SVX"),
                    ),
                )
            )
            catalog.replace_topology(result.catalog_data(), result.covered_cities)
            catalog.replace_topology(result.catalog_data(), result.covered_cities)
            topology, cities = (
                SqliteTopologyRepository(path),
                SqliteCityRepository(path),
            )
            try:
                self.assertEqual(await topology.destinations(("MOW",)), ("ALA", "SVX"))
                self.assertEqual(await topology.destinations(("ALA",)), ("SVX",))
                self.assertEqual((await cities.get_many(("MOW",)))[0], existing)
            finally:
                topology.close()
                cities.close()
                catalog.close()

    async def test_failure_rolls_back_deletions_and_profile_inserts(self):
        result = await snapshot()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            catalog = SqliteCatalog(path)
            catalog.initialize()
            catalog.upsert(
                CatalogData(
                    cities=(),
                    links=(FlightLink(origin="MOW", destination="ZZZ"),),
                )
            )

            def fail_insert(conn, clause, multiparams, params, options):
                if (
                    isinstance(clause, Insert)
                    and clause.table.name == FlightLinkRow.__tablename__
                ):
                    raise RuntimeError("simulated write failure")

            event.listen(catalog._engine, "before_execute", fail_insert)
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                catalog.replace_topology(result.catalog_data(), result.covered_cities)
            topology, cities = (
                SqliteTopologyRepository(path),
                SqliteCityRepository(path),
            )
            try:
                self.assertEqual(await topology.destinations(("MOW",)), ("ZZZ",))
                self.assertEqual(await cities.get_many(("MOW", "SVX")), ())
            finally:
                topology.close()
                cities.close()
                catalog.close()

    async def test_successful_empty_list_removes_only_covered_origin_links(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            catalog = SqliteCatalog(path)
            catalog.initialize()
            catalog.upsert(
                CatalogData(
                    cities=(),
                    links=(
                        FlightLink(origin="MOW", destination="SVX"),
                        FlightLink(origin="SVX", destination="MOW"),
                    ),
                )
            )
            catalog.replace_topology(CatalogData(cities=(), links=()), ("MOW",))
            topology = SqliteTopologyRepository(path)
            try:
                self.assertEqual(await topology.destinations(("MOW",)), ())
                self.assertEqual(await topology.destinations(("SVX",)), ("MOW",))
            finally:
                topology.close()
                catalog.close()


class CliTests(unittest.TestCase):
    def test_failed_download_leaves_existing_catalog_and_snapshot_untouched(self):
        async def fail(*args, **kwargs):
            raise SourceError("upstream failed")

        with tempfile.TemporaryDirectory() as folder:
            db, snapshot_path = (
                Path(folder) / "catalog.sqlite3",
                Path(folder) / "new.json",
            )
            db.write_bytes(b"existing-file")
            with (
                patch(
                    "sys.argv",
                    [
                        "topology",
                        "--cities",
                        "MOW",
                        "--snapshot",
                        str(snapshot_path),
                        "--catalog",
                        str(db),
                    ],
                ),
                patch("ubas_backend.topology.__main__.download", fail),
                self.assertRaises(SourceError),
            ):
                main()
            self.assertEqual(db.read_bytes(), b"existing-file")
            self.assertFalse(snapshot_path.exists())


class HopBoundTests(unittest.IsolatedAsyncioTestCase):
    def test_matches_independent_exhaustive_walk_enumeration(self):
        nodes = ("AAA", "BBB", "CCC", "DDD", "ZZZ")
        for seed in range(50):
            rng = random.Random(seed)
            edges = {
                (a, b) for a in nodes for b in nodes if a != b and rng.random() < 0.35
            }
            paths = [("AAA",)]
            found = set()
            for _ in range(4):
                paths = [
                    path + (destination,)
                    for path in paths
                    for origin, destination in edges
                    if origin == path[-1]
                ]
                for path in paths:
                    if path[-1] == "ZZZ":
                        found.update(path[1:-1])
            expected = found - {"AAA", "ZZZ"}
            self.assertEqual(
                set(cities_on_paths(edges, "AAA", "ZZZ", 4)), expected, seed
            )

    async def test_dead_ends_do_not_consume_hub_budget_but_real_candidates_do(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalog.sqlite3"
            catalog = SqliteCatalog(path)
            catalog.initialize()
            catalog.upsert(
                CatalogData(
                    cities=(),
                    links=tuple(
                        FlightLink(origin=a, destination=b)
                        for a, b in (
                            ("AAA", "BBB"),
                            ("BBB", "ZZZ"),
                            ("AAA", "CCC"),
                            ("AAA", "DDD"),
                            ("AAA", "EEE"),
                        )
                    ),
                )
            )
            repo = SqliteTopologyRepository(path)
            req = RouteRequest(
                origin="AAA",
                destination="ZZZ",
                date_from="2027-01-01",
                date_to="2027-01-02",
            )
            try:
                diagnostics = SearchDiagnostics()
                cities = await discover_cities(
                    repo, req, PlannerConfig(max_hubs=1), diagnostics
                )
                self.assertTrue(diagnostics.complete)
                self.assertEqual(cities, ("AAA", "ZZZ", "BBB"))
                catalog.upsert(
                    CatalogData(
                        cities=(), links=(FlightLink(origin="CCC", destination="ZZZ"),)
                    )
                )
                diagnostics = SearchDiagnostics()
                await discover_cities(repo, req, PlannerConfig(max_hubs=1), diagnostics)
                self.assertFalse(diagnostics.complete)
            finally:
                repo.close()
                catalog.close()

    async def test_sqlite_planner_matches_exhaustive_ticket_combinations(self):
        from planner_fixtures import (
            NOW,
            REQUEST,
            Cities,
            NoVisas,
            Provider,
            offer,
            profile,
            segment,
        )
        from test_planner_search import exhaustive

        from ubas_backend.routing.planner import RoutePlanner

        nodes = ("AAA", "BBB", "CCC", "ZZZ")
        with tempfile.TemporaryDirectory() as folder:
            for seed in range(15):
                rng = random.Random(seed)
                offers = []
                for index in range(8):
                    first, last = rng.sample(nodes, 2)
                    offers.append(
                        offer(
                            index,
                            [segment(first, last, rng.randint(0, 90), 2)],
                            rng.randint(0, 100),
                        )
                    )
                offers.extend(
                    [
                        offer(
                            "through",
                            [segment("AAA", "BBB", 0), segment("BBB", "ZZZ", 5)],
                            30,
                        ),
                        offer("direct", [segment("AAA", "ZZZ", 1)], 40),
                    ]
                )
                path = Path(folder) / f"{seed}.sqlite3"
                catalog = SqliteCatalog(path)
                catalog.initialize()
                edges = {
                    (part.origin.city_code, part.destination.city_code)
                    for item in offers
                    for part in item.segments
                }
                catalog.upsert(
                    CatalogData(
                        cities=(),
                        links=tuple(
                            FlightLink(origin=a, destination=b) for a, b in edges
                        ),
                    )
                )
                catalog.close()
                topology = SqliteTopologyRepository(path)
                profiles = [profile(code, 80) for code in nodes]
                try:
                    result = await RoutePlanner(
                        Provider(offers),
                        topology,
                        Cities(profiles),
                        visa_costs=NoVisas(),
                    ).plan(REQUEST, now=NOW)
                    self.assertEqual(
                        [item.route.total_cost.amount for item in result.routes],
                        exhaustive(offers, profiles),
                    )
                finally:
                    topology.close()


if __name__ == "__main__":
    unittest.main()
