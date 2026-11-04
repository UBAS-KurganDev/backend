"""Optimized top ten versus an independent exhaustive route enumeration."""

import random
import unittest
from datetime import UTC, timedelta
from decimal import Decimal
from itertools import product

from planner_fixtures import (
    NOW,
    REQUEST,
    START,
    Cities,
    NoVisas,
    Provider,
    Topology,
    offer,
    plan,
    profile,
    segment,
)

from ubas_backend.routing.candidates import CandidateBuilder
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.costs import CostCalculator
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.planner import IncompleteSearchError, RoutePlanner
from ubas_backend.routing.search import RouteSearch


def exhaustive(offers, profiles, threshold=60):
    scores = {item.city_code: item.tourism_score for item in profiles}
    found = []
    # Independent enumeration: no production builder, bound, index or selector.
    for count in range(1, 5):
        for tickets in product(offers, repeat=count):
            parts = [part for ticket in tickets for part in ticket.segments]
            if len(parts) > 4:
                continue
            if (
                parts[0].origin.city_code != "AAA"
                or parts[-1].destination.city_code != "ZZZ"
            ):
                continue
            if not REQUEST.date_from <= parts[0].departure_at.date() <= REQUEST.date_to:
                continue
            if not REQUEST.date_from <= parts[-1].arrival_at.date() <= REQUEST.date_to:
                continue
            valid = True
            for first, second in zip(parts, parts[1:], strict=False):
                hours = (
                    second.departure_at.astimezone(UTC)
                    - first.arrival_at.astimezone(UTC)
                ).total_seconds() / 3600
                if first.destination.city_code != second.origin.city_code or hours <= 0:
                    valid = False
                    break
                if (
                    hours >= 12
                    and scores.get(first.destination.city_code, 0) < threshold
                ):
                    valid = False
                    break
            if valid:
                found.append(
                    sum(
                        (
                            item.total_price.amount if item.total_price else Decimal(0)
                            for item in tickets
                        ),
                        Decimal(0),
                    )
                )
    return sorted(found)[:10]


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_random_graphs_match_independent_exhaustive_search(self):
        cities = ("AAA", "BBB", "CCC", "ZZZ")
        for seed in range(25):
            rng = random.Random(seed)
            profiles = [profile(code, rng.choice((0, 59, 60, 90))) for code in cities]
            offers = []
            for index in range(9):
                first, last = rng.sample(cities, 2)
                offers.append(
                    offer(
                        index,
                        [
                            segment(
                                first,
                                last,
                                rng.randint(0, 95),
                                rng.choice((1, 2, 3)),
                                identity=str(index),
                            )
                        ],
                        rng.randint(0, 100),
                    )
                )
            # Stress atomic prices and duplicate multiplicity.
            offers += [
                offer(
                    "through", [segment("AAA", "BBB", 0), segment("BBB", "ZZZ", 6)], 30
                ),
                offer("direct", [segment("AAA", "ZZZ", 1)], 50),
            ]
            offers.append(offers[-1])
            actual = await plan(offers, profiles=profiles, visas=NoVisas())
            self.assertEqual(
                [item.route.total_cost.amount for item in actual.routes],
                exhaustive(offers, profiles),
                f"seed={seed}",
            )

    async def test_prunes_only_against_ten_real_routes(self):
        # Cheap relaxed connection is chronologically impossible.
        offers = [
            offer("prefix", [segment("AAA", "BBB", 10)], 0),
            offer("impossible", [segment("BBB", "ZZZ", 0)], 0),
            offer("expensive", [segment("BBB", "ZZZ", 14)], 1000),
        ]
        offers += [offer(i, [segment("AAA", "ZZZ", 1)], 69 + i) for i in range(10)]
        result = await plan(offers, visas=NoVisas())
        self.assertEqual(
            [r.route.total_cost.amount for r in result.routes], list(range(69, 79))
        )

    async def test_price_pruning_is_complete_even_at_candidate_budget(self):
        offers = tuple(offer(i, [segment("AAA", "ZZZ")], i + 1) for i in range(100))
        config = PlannerConfig(max_candidates=10)
        diagnostics = SearchDiagnostics()
        result = await RouteSearch(
            config, CandidateBuilder(config, {}, CostCalculator(config), NoVisas())
        ).run(offers, REQUEST, diagnostics, NOW)
        self.assertTrue(diagnostics.complete)
        self.assertEqual(len(result), 10)
        self.assertEqual(diagnostics.evaluated, 10)
        self.assertGreater(diagnostics.rejected["price_bound"], 0)

    async def test_budget_exhaustion_never_publishes_partial_cards(self):
        offers = [offer(i, [segment("AAA", "ZZZ")], i + 1) for i in range(12)]
        for changes in (
            {"max_candidates": 1},
            {"max_search_nodes": 1},
            {"max_frontier": 1},
            {"max_provider_queries": 1},
            {"max_total_offers": 1},
            {"max_offers_per_search": 1},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(IncompleteSearchError),
            ):
                await plan(offers, config=PlannerConfig(**changes))

    async def test_topology_limit_is_not_reported_as_complete(self):
        offers = [
            offer("a", [segment("AAA", "BBB")]),
            offer("b", [segment("AAA", "CCC")]),
        ]
        with self.assertRaisesRegex(IncompleteSearchError, "topology_city_limit"):
            await plan(offers, config=PlannerConfig(max_hubs=1))

    async def test_paths_with_repeated_city_remain_searchable(self):
        offers = [
            offer("a", [segment("AAA", "BBB", 0)], 1),
            offer("b", [segment("BBB", "AAA", 3)], 1),
            offer("c", [segment("AAA", "ZZZ", 6)], 1),
        ]
        result = await plan(offers, visas=NoVisas())
        self.assertEqual([r.route.total_cost.amount for r in result.routes], [1, 3])

    async def test_multi_stop_through_ticket_does_not_need_topology(self):
        item = offer(
            "through",
            [
                segment("AAA", "BBB", 0),
                segment("BBB", "CCC", 20),
                segment("CCC", "ZZZ", 40),
            ],
            100,
        )
        result = await RoutePlanner(
            Provider([item]),
            Topology(),
            Cities([profile("BBB"), profile("CCC")]),
            visa_costs=NoVisas(),
        ).plan(REQUEST, now=NOW)
        self.assertEqual(len(result.routes[0].route.stopovers), 2)

    async def test_local_endpoint_dates_and_utc_chronology(self):
        from planner_fixtures import airport

        # UTC departure before the window is valid on the local departure date.
        start = segment(airport("AAA", timezone="Pacific/Kiritimati"), "BBB", -10, 1)
        end = segment("BBB", airport("ZZZ", timezone="Etc/GMT+12"), 24, 1)
        request = REQUEST.model_copy(update={"date_to": START.date()})
        result = await plan(
            [offer("t", [start, end])],
            request=request,
            profiles=[profile("BBB")],
            visas=NoVisas(),
        )
        self.assertEqual(len(result.routes), 1)
        late = end.model_copy(update={"arrival_at": START + timedelta(days=2)})
        result = await plan(
            [offer("t", [start, late])],
            request=request,
            profiles=[profile("BBB")],
            visas=NoVisas(),
        )
        self.assertEqual(result.routes, ())
