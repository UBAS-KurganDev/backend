"""MVP acceptance boundaries and failure/cancellation semantics."""

import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal

from planner_fixtures import (
    NOW,
    REQUEST,
    START,
    Cities,
    NoVisas,
    Provider,
    Topology,
    airport,
    offer,
    plan,
    profile,
    segment,
)

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.offers import TicketSearch
from ubas_backend.routing.planner import IncompleteSearchError, RoutePlanner
from ubas_backend.routing.policy import InvalidRouteRequest, RequestPolicy
from ubas_backend.routing.ports import CitySourceError, FlightProviderError
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult, VisaExpense


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_two_three_stops_and_mixed_ticket_packages(self):
        parts = [
            segment("AAA", "BBB", 0),
            segment("BBB", "CCC", 3),
            segment("CCC", "DDD", 6),
            segment("DDD", "ZZZ", 9),
        ]
        for boundary in (1, 2, 3, 4):
            tickets = [offer("first", parts[:boundary], 20)]
            if boundary < 4:
                tickets.append(offer("second", parts[boundary:], 30))
            result = await plan(tickets, visas=NoVisas())
            self.assertEqual(len(result.routes), 1)
            self.assertEqual(len(result.routes[0].route.stopovers), 3)
            self.assertEqual(
                result.routes[0].route.total_cost.amount, 20 if boundary == 4 else 50
            )

    async def test_four_stops_are_excluded(self):
        cities = ("AAA", "BBB", "CCC", "DDD", "EEE", "ZZZ")
        tickets = [
            offer(i, [segment(a, b, i * 3)])
            for i, (a, b) in enumerate(zip(cities, cities[1:], strict=False))
        ]
        self.assertEqual((await plan(tickets, visas=NoVisas())).routes, ())

    async def test_tourism_boundary_and_missing_scores(self):
        for duration, score, expected in (
            (12 - 1 / 60, 0, 1),
            (12, 59, 0),
            (12, 60, 1),
            (12, 61, 1),
            (80, 60, 1),
            (12, None, 0),
            (1, None, 1),
            (0.0001, 0, 1),
        ):
            with self.subTest(duration=duration, score=score):
                item = offer(
                    "t", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 2 + duration)]
                )
                profiles = [profile("BBB", score)] if score is not None else []
                result = await plan([item], profiles=profiles, visas=NoVisas())
                self.assertEqual(len(result.routes), expected)

    async def test_each_long_stop_is_checked(self):
        item = offer(
            "t",
            [
                segment("AAA", "BBB"),
                segment("BBB", "CCC", 14),
                segment("CCC", "ZZZ", 28),
            ],
        )
        result = await plan(
            [item], profiles=[profile("BBB", 90), profile("CCC", 59)], visas=NoVisas()
        )
        self.assertEqual(result.routes, ())

    async def test_airport_change_with_one_second_is_accepted_and_warned(self):
        first = offer("first", [segment("AAA", airport("BBB", "BBX"))])
        second = offer("second", [segment(airport("BBB", "BBY"), "ZZZ", 2 + 1 / 3600)])
        result = await plan([first, second], visas=NoVisas())
        stop = result.routes[0].route.stopovers[0]
        self.assertTrue(stop.airport_change)
        self.assertAlmostEqual(stop.duration_hours * 3600, 1)
        self.assertIn("airport_change:0", result.routes[0].route.warnings)

    async def test_zero_and_negative_connections_are_excluded(self):
        for departure in (1, 2):
            result = await plan(
                [
                    offer("a", [segment("AAA", "BBB")]),
                    offer("b", [segment("BBB", "ZZZ", departure)]),
                ],
                visas=NoVisas(),
            )
            self.assertEqual(result.routes, ())

    async def test_all_airports_of_city_are_considered(self):
        result = await plan(
            [
                offer(
                    "a", [segment(airport("AAA", "AAX"), airport("ZZZ", "ZZX"))], 100
                ),
                offer("b", [segment(airport("AAA", "AAY"), airport("ZZZ", "ZZY"))], 50),
            ]
        )
        self.assertEqual([r.route.total_cost.amount for r in result.routes], [50, 100])

    async def test_visa_cost_not_permission_and_no_hotel_cost(self):
        city = profile("BBB", 80, fee=3000)
        # unavailable, expired, 999-day processing, 1-hour stay: none are filters.
        result = await plan(
            [offer("t", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 90)], 20000)],
            profiles=[city],
        )
        route = result.routes[0].route
        self.assertEqual(route.total_cost.amount, 23000)
        self.assertEqual(route.ticket_cost.amount, 20000)
        self.assertEqual(route.visa_cost.amount, 3000)

    async def test_unknown_cost_is_flagged_and_sorted_without_penalty(self):
        result = await plan(
            [
                offer(
                    "unknown", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 4)], 20000
                ),
                offer("known", [segment("AAA", "ZZZ")], 21000),
            ]
        )
        first = result.routes[0].route
        self.assertEqual(first.total_cost.amount, 20000)
        self.assertFalse(first.full_cost_known)
        self.assertTrue(first.unknown_costs)
        self.assertIn("full_cost_unknown", first.warnings)
        self.assertTrue(result.routes[1].route.full_cost_known)

    async def test_currency_conversion_and_missing_rate(self):
        result = await plan(
            [offer("usd", [segment("AAA", "ZZZ")], 100, "USD")],
            config=PlannerConfig(rub_rates={"RUB": Decimal(1), "USD": Decimal(90)}),
        )
        self.assertEqual(result.routes[0].route.total_cost.amount, 9000)
        missing = await plan([offer("usd", [segment("AAA", "ZZZ")], 100, "USD")])
        self.assertEqual(missing.routes[0].route.total_cost.amount, 0)
        self.assertFalse(missing.routes[0].route.full_cost_known)

    async def test_missing_ticket_price_preserves_card_and_original_fetch_time(self):
        item = offer("unknown", [segment("AAA", "ZZZ")], None)
        result = await plan([item])
        self.assertFalse(result.routes[0].route.full_cost_known)
        self.assertEqual(result.routes[0].route.tickets[0].fetched_at, NOW)
        self.assertEqual(result.routes[0].route.total_cost.amount, 0)

    async def test_source_expiry_does_not_invent_an_unagreed_cache_age_policy(self):
        item = offer("cache", [segment("AAA", "ZZZ")])
        item = item.model_copy(
            update={
                "fetched_at": NOW - timedelta(days=2),
                "expires_at": NOW - timedelta(days=1),
            }
        )
        result = await plan([item])
        self.assertEqual(len(result.routes), 1)
        self.assertEqual(result.routes[0].route.tickets[0].fetched_at, item.fetched_at)

    async def test_route_aware_visa_resolver_counts_whole_route_expense_once(self):
        class Visas:
            def expenses(self, segments, profiles):
                return (
                    VisaExpense(
                        key="one-multi-entry",
                        amount=profile("BBB", fee=3000).visas[0].fee_per_person,
                    ),
                )

        result = await plan(
            [
                offer(
                    "t",
                    [
                        segment("AAA", "BBB"),
                        segment("BBB", "CCC", 4),
                        segment("CCC", "ZZZ", 8),
                    ],
                    20000,
                )
            ],
            visas=Visas(),
        )
        self.assertEqual(result.routes[0].route.total_cost.amount, 23000)

    async def test_tourism_source_failure_uses_zero_for_short_and_long_stops(self):
        class Unavailable:
            async def get_many(self, codes):
                raise CitySourceError("offline")

        for stop, expected in ((1, 1), (12, 0)):
            item = offer("t", [segment("AAA", "BBB"), segment("BBB", "ZZZ", 2 + stop)])
            result = await RoutePlanner(
                Provider([item]),
                Topology([item]),
                Unavailable(),
                visa_costs=NoVisas(),
            ).plan(REQUEST, now=NOW)
            self.assertEqual(len(result.routes), expected)
            self.assertTrue(result.complete)

    async def test_empty_success_and_provider_failure_are_different(self):
        self.assertEqual((await plan([])).routes, ())

        class Broken:
            async def search(self, query):
                raise FlightProviderError("offline")

        with self.assertRaises(IncompleteSearchError):
            await RoutePlanner(Broken(), Topology(), Cities()).plan(REQUEST, now=NOW)

    def test_request_window_fixed_profile_and_local_today(self):
        policy = RequestPolicy(PlannerConfig())
        policy.validate(REQUEST, now=NOW)
        with self.assertRaises(InvalidRouteRequest):
            policy.validate(
                REQUEST.model_copy(
                    update={"date_to": START.date() + timedelta(days=30)}
                ),
                now=NOW,
            )
        for update in (
            {"passengers": 2},
            {"currency": "USD"},
            {"limit": 5},
            {"budget": Decimal(100)},
        ):
            with self.assertRaises(InvalidRouteRequest):
                policy.validate(REQUEST.model_copy(update=update), now=NOW)
        at = NOW.replace(hour=0)
        local_yesterday = at.date() - timedelta(days=1)
        request = REQUEST.model_copy(
            update={"date_from": local_yesterday, "date_to": local_yesterday}
        )
        policy.validate(request, now=at, origin_timezone="America/Los_Angeles")
        with self.assertRaises(InvalidRouteRequest):
            policy.validate(request, now=at, origin_timezone="Asia/Tokyo")

    async def test_refinement_replaces_parent_and_preserves_real_duplicates(self):
        item = offer("same", [segment("AAA", "ZZZ")])

        class Provider:
            async def search(self, query):
                if query.departure_from != query.departure_to:
                    return FlightSearchResult(offers=(), complete=False)
                return FlightSearchResult(
                    offers=(item, item) if query.departure_from == START.date() else ()
                )

        query = FlightQuery(
            origin="AAA",
            destination="ZZZ",
            departure_from=START.date(),
            departure_to=START.date() + timedelta(days=2),
            passengers=1,
            limit=10,
        )
        diagnostics = SearchDiagnostics()
        result = await TicketSearch(Provider(), PlannerConfig()).collect(
            (query,),
            REQUEST,
            diagnostics,
            now=NOW,
        )
        self.assertEqual(len(result), 2)
        self.assertTrue(diagnostics.complete)

    async def test_cancellation_reaches_all_running_provider_calls(self):
        started = asyncio.Event()
        running = 0
        cancelled = 0

        class Blocking:
            async def search(self, query):
                nonlocal running, cancelled
                running += 1
                if running == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled += 1

        task = asyncio.create_task(
            RoutePlanner(
                Blocking(),
                Topology(),
                Cities(),
                config=PlannerConfig(max_concurrent_queries=2),
            ).plan(REQUEST, now=NOW)
        )
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(cancelled, 2)
