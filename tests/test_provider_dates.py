"""Single-date queries preserve coverage, budgets and complete results."""

import unittest
from datetime import timedelta

from planner_fixtures import (
    NOW,
    REQUEST,
    START,
    Cities,
    Geography,
    NoVisas,
    Provider,
    Topology,
    offer,
    segment,
)
from test_planner_search import exhaustive

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.offers import (
    TicketSearch,
    build_queries,
)
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult


def query(offset=0):
    return FlightQuery(
        origin="AAA",
        destination="ZZZ",
        passengers=1,
        limit=10,
        departure_date=START.date() + timedelta(days=offset),
    )


class SingleDayProvider:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        return FlightSearchResult()


class ProviderDatesTests(unittest.IsolatedAsyncioTestCase):
    def test_only_star_pairs_and_correct_segment_limits(self):
        queries = tuple(
            build_queries(REQUEST, ("AAA", "ZZZ", "BBB", "CCC"), PlannerConfig())
        )
        self.assertEqual(
            {(q.origin, q.destination) for q in queries},
            {
                ("AAA", "ZZZ"),
                ("AAA", "BBB"),
                ("BBB", "ZZZ"),
                ("AAA", "CCC"),
                ("CCC", "ZZZ"),
            },
        )
        for query in queries:
            self.assertEqual(
                query.max_segments,
                2 if (query.origin, query.destination) == ("AAA", "ZZZ") else 1,
            )

    def test_generated_dates_cover_padded_window_once_per_pair(self):
        for days in range(1, 31):
            request = REQUEST.model_copy(
                update={"date_to": START.date() + timedelta(days=days - 1)}
            )
            queries = tuple(build_queries(request, ("AAA", "ZZZ"), PlannerConfig()))
            actual = [q.departure_date for q in queries if q.origin == "AAA"]
            self.assertEqual(
                actual, [START.date() + timedelta(days=i) for i in range(days)]
            )
            self.assertEqual(len(queries), len(set(queries)))

    async def test_exact_budget_completes_but_unsearched_day_does_not(self):
        for budget, complete in ((2, False), (3, True)):
            provider = SingleDayProvider()
            diagnostics = SearchDiagnostics()
            await TicketSearch(
                provider, PlannerConfig(max_provider_queries=budget)
            ).collect(
                (query(0), query(1), query(2), query(0)), REQUEST, diagnostics, now=NOW
            )
            self.assertEqual(len(provider.queries), budget)
            self.assertEqual(diagnostics.complete, complete)
            self.assertEqual(
                "provider_query_limit" in diagnostics.warnings, not complete
            )

    async def test_thirty_hubs_budget_is_spent_on_supported_queries(self):
        provider = SingleDayProvider()
        config = PlannerConfig()
        request = REQUEST.model_copy(
            update={"date_to": REQUEST.date_from + timedelta(days=3)}
        )
        cities = ("AAA", "ZZZ") + tuple(
            f"B{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(30)
        )
        roots = build_queries(request, cities, config)
        diagnostics = SearchDiagnostics()
        await TicketSearch(provider, config).collect(
            roots, request, diagnostics, now=NOW
        )
        self.assertEqual(len(provider.queries), 364)
        self.assertEqual(diagnostics.warnings, [])
        self.assertTrue(diagnostics.complete)

    async def test_root_offer_budget_is_shared_by_all_date_partitions(self):
        class Offers(SingleDayProvider):
            async def search(self, query):
                offset = (query.departure_date - START.date()).days * 24
                item = offer(str(offset), [segment("AAA", "ZZZ", offset)])
                return FlightSearchResult(offers=(item,))

        diagnostics = SearchDiagnostics()
        found = await TicketSearch(
            Offers(), PlannerConfig(max_offers_per_search=2)
        ).collect((query(0), query(1), query(2)), REQUEST, diagnostics, now=NOW)
        self.assertEqual(len(found), 2)
        self.assertIn("offers_per_search_limit", diagnostics.warnings)

    async def test_planner_matches_exhaustive_routes_with_daily_provider(self):
        offers = [offer(i, [segment("AAA", "ZZZ", i * 24)], 100 - i) for i in range(6)]

        offers.append(offers[0])  # Real source duplicates must survive partitioning.
        provider = Provider(offers)
        result = await RoutePlanner(
            provider,
            Topology(offers),
            Cities([]),
            geography=Geography(),
            visa_costs=NoVisas(),
        ).plan(REQUEST, now=NOW)
        self.assertEqual(
            [r.route.total_cost.amount for r in result.routes], exhaustive(offers, [])
        )
        self.assertTrue(all(hasattr(q, "departure_date") for q in provider.queries))

    async def test_incomplete_day_is_not_split_or_retried(self):
        class Incomplete(SingleDayProvider):
            async def search(self, query):
                self.queries.append(query)
                return FlightSearchResult(complete=False)

        provider = Incomplete()
        diagnostics = SearchDiagnostics()
        await TicketSearch(provider, PlannerConfig()).collect(
            (query(),), REQUEST, diagnostics, now=NOW
        )
        self.assertEqual(len(provider.queries), 1)
        self.assertFalse(diagnostics.complete)
        self.assertIn("incomplete_provider:AAA:ZZZ", diagnostics.warnings)

    def test_old_range_contract_is_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            FlightQuery.model_validate(
                {
                    "origin": "AAA",
                    "destination": "ZZZ",
                    "departure_from": "2027-05-04",
                    "departure_to": "2027-05-05",
                    "passengers": 1,
                    "limit": 10,
                }
            )
