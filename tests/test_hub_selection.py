"""Bounded coverage is explicit; completeness is checked inside that coverage."""

import asyncio
import random
import unittest

from planner_fixtures import (
    NOW,
    REQUEST,
    Cities,
    Geography,
    NoVisas,
    Provider,
    Topology,
    offer,
    profile,
    segment,
)
from test_planner_search import exhaustive

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.hubs import discover_cities, select_cities
from ubas_backend.routing.planner import IncompleteSearchError, RoutePlanner
from ubas_backend.routing.ports import CitySourceError, FlightProviderError
from ubas_backend.routing.schemas import PlanResult


class Graph(Topology):
    def __init__(self, links):
        self.links = set(links)


class HubSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_top_thirty_consider_all_profiles_and_exclude_dead_ends(self):
        hubs = tuple(f"B{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(35))
        graph = Graph(
            [(a, b) for city in hubs for a, b in (("AAA", city), (city, "ZZZ"))]
            + [("AAA", "XXX")]
        )
        profiles = [profile(city, i) for i, city in enumerate(hubs)] + [
            profile("XXX", 100)
        ]
        provider = Provider(())
        planner = RoutePlanner(
            provider,
            graph,
            Cities(profiles),
            geography=Geography(),
            config=PlannerConfig(max_provider_queries=6000),
        )
        result = await planner.plan(
            REQUEST.model_copy(update={"date_to": REQUEST.date_from}), now=NOW
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.search_scope.candidate_hubs, 35)
        self.assertEqual(result.search_scope.selected_hubs, tuple(reversed(hubs[5:])))
        self.assertTrue(result.search_scope.limited)
        self.assertEqual(len(provider.queries), 181)
        self.assertEqual(
            (provider.queries[0].origin, provider.queries[0].destination),
            ("AAA", "ZZZ"),
        )
        self.assertTrue(
            all(
                q.origin in result.searched_cities
                and q.destination in result.searched_cities
                for q in provider.queries
            )
        )
        self.assertEqual(
            PlanResult.model_validate_json(result.model_dump_json()), result
        )

    async def test_missing_scores_use_direct_connections_then_degree_then_iata(self):
        graph = Graph(
            [
                ("AAA", "BBB"),
                ("BBB", "ZZZ"),
                ("AAA", "CCC"),
                ("CCC", "ZZZ"),
                ("AAA", "DDD"),
                ("DDD", "ZZZ"),
                ("DDD", "CCC"),
            ]
        )
        cfg, diagnostics = PlannerConfig(max_hubs=1), SearchDiagnostics()
        candidates = await discover_cities(graph, REQUEST, cfg, diagnostics)
        selected = await select_cities(graph, candidates, {}, cfg, diagnostics)
        self.assertEqual(selected, ("AAA", "ZZZ", "CCC"))
        graph.links.remove(("DDD", "CCC"))
        graph.links.add(("DDD", "BBB"))
        graph.links.add(("DDD", "CCC"))
        graph.links.add(("BBB", "CCC"))
        selected = await select_cities(graph, candidates, {}, cfg, diagnostics)
        self.assertEqual(selected[-1], "BBB")
        # Tourism has priority over degree among eligible one-stop hubs.
        selected = await select_cities(
            graph, candidates, {"DDD": profile("DDD", 1)}, cfg, diagnostics
        )
        self.assertEqual(selected[-1], "DDD")

    async def test_through_ticket_outside_selected_hubs_and_exhaustive_results(self):
        for seed in range(15):
            rng = random.Random(seed)
            offers = [
                offer("direct", [segment("AAA", "ZZZ", 0)], rng.randint(1, 100)),
                offer("ab", [segment("AAA", "BBB", 0)], rng.randint(1, 100)),
                offer("bc", [segment("BBB", "ZZZ", 4)], rng.randint(1, 100)),
                offer("ac", [segment("AAA", "CCC", 0)], 1),
                offer("cz", [segment("CCC", "ZZZ", 4)], 1),
                offer(
                    "through", [segment("AAA", "CCC", 0), segment("CCC", "ZZZ", 4)], 3
                ),
            ]
            profiles = [profile("BBB", 90), profile("CCC", 70)]
            result = await RoutePlanner(
                Provider(offers),
                Topology(offers),
                Cities(profiles),
                config=PlannerConfig(max_hubs=1),
                visa_costs=NoVisas(),
                geography=Geography(),
            ).plan(REQUEST, now=NOW)
            self.assertEqual(result.searched_cities, ("AAA", "ZZZ", "BBB"))
            retained = [
                item
                for item in offers
                if item.segments[0].origin.city_code in result.searched_cities
                and item.segments[-1].destination.city_code in result.searched_cities
            ]
            self.assertEqual(
                [r.route.total_cost.amount for r in result.routes],
                exhaustive(retained, profiles),
                seed,
            )
            self.assertTrue(
                any(r.route.tickets[0].offer_id == "through" for r in result.routes)
            )

    async def test_tourism_source_failure_falls_back_but_ticket_failure_still_fails(
        self,
    ):
        graph = Graph([("AAA", "BBB"), ("BBB", "ZZZ"), ("AAA", "CCC"), ("CCC", "ZZZ")])

        class MissingCities:
            async def get_many(self, codes):
                raise CitySourceError("offline")

        provider = Provider(())
        planner = RoutePlanner(
            provider,
            graph,
            MissingCities(),
            config=PlannerConfig(max_hubs=1),
            geography=Geography(),
        )
        result = await planner.plan(REQUEST, now=NOW)
        self.assertTrue(result.complete)
        self.assertEqual(result.search_scope.selected_hubs, ("BBB",))
        self.assertIn("missing_tourism_scores_use_zero", result.warnings)

        async def broken(query):
            raise FlightProviderError("offline")

        provider.search = broken
        with self.assertRaises(IncompleteSearchError):
            await planner.plan(REQUEST, now=NOW)

    async def test_empty_topology_and_no_truncation_have_explicit_scope(self):
        result = await RoutePlanner(Provider(()), Graph(()), Cities()).plan(
            REQUEST, now=NOW
        )
        self.assertFalse(result.search_scope.limited)
        self.assertEqual(result.search_scope.candidate_hubs, 0)
        old = result.model_dump()
        old.pop("search_scope")
        self.assertIsNone(PlanResult.model_validate(old).search_scope)

    async def test_discovery_cancellation_reaches_child_tasks(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class SlowGraph:
            async def destinations(self, origins):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        task = asyncio.create_task(
            discover_cities(SlowGraph(), REQUEST, PlannerConfig(), SearchDiagnostics())
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
