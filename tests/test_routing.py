"""Regression checks for planner behavior and structured cancellation."""

import asyncio
import contextlib
import io
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.demo import main as demo
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.offers import TicketSearch, build_queries
from ubas_backend.routing.schemas import Money, PlanResult, RouteRequest
from ubas_backend.routing.scoring import WeightedScorer
from ubas_backend.routing.validation import OfferRejection, offer_rejection
from ubas_backend.runtime import _load_provider


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await demo()
        self.result = PlanResult.model_validate_json(output.getvalue())
        self.route = self.result.routes[0].route
        departure = self.route.tickets[0].segments[0].departure_at
        self.request = RouteRequest(
            origin="MOW",
            destination="TYO",
            date_from=departure.date(),
            date_to=(departure + timedelta(days=3)).date(),
        )
        self.queries = build_queries(self.request, self.route.stopover, PlannerConfig())

    async def test_demo_preserves_both_ticket_modes(self):
        self.assertTrue(self.result.complete)
        self.assertEqual(len(self.result.routes), 2)
        self.assertEqual(
            {route.route.metrics.self_transfer for route in self.result.routes},
            {False, True},
        )
        self.assertEqual(
            {route.route.total_cost.amount for route in self.result.routes},
            {Decimal("36000"), Decimal("42000")},
        )

    async def test_offer_checks_keep_distinct_rejection_reasons(self):
        offer = self.route.tickets[0]
        query = next(query for query in self.queries if query.stopover_city)
        now = self.result.generated_at
        self.assertIsNone(offer_rejection(offer, query, self.request, now))
        wrong_query = query.model_copy(update={"destination": "SIN"})
        self.assertEqual(
            offer_rejection(offer, wrong_query, self.request, now),
            OfferRejection.CONTRACT,
        )
        expired = offer.model_copy(update={"expires_at": now})
        self.assertEqual(
            offer_rejection(expired, query, self.request, now),
            OfferRejection.EXPIRED_OR_PAST,
        )
        no_baggage = offer.model_copy(update={"checked_baggage_included": None})
        baggage_request = self.request.model_copy(
            update={"checked_baggage_required": True}
        )
        self.assertEqual(
            offer_rejection(no_baggage, query, baggage_request, now),
            OfferRejection.BAGGAGE,
        )

    async def test_demo_worker_factory_refreshes_expired_quotes(self):
        provider = _load_provider("ubas_backend.routing.demo:create_flight_provider")
        query = next(query for query in self.queries if query.stopover_city)
        first_time = self.result.generated_at
        later = first_time + timedelta(minutes=21)
        with patch("ubas_backend.routing.demo.datetime") as clock:
            clock.now.side_effect = [first_time, later]
            first = await provider.search(query)
            refreshed = await provider.search(query)
        self.assertEqual(len(first.offers), 1)
        self.assertEqual(len(refreshed.offers), 1)
        self.assertLess(first.offers[0].expires_at, later)
        self.assertGreater(refreshed.offers[0].expires_at, later)
        self.assertEqual(refreshed.offers[0].fetched_at, later)
        self.assertEqual(refreshed.warnings, ("synthetic_demo_data",))

    async def test_demo_worker_factory_does_not_invent_unsupported_offers(self):
        provider = _load_provider("ubas_backend.routing.demo:create_flight_provider")
        query = next(query for query in self.queries if query.stopover_city)
        for update in (
            {"currency": "USD"},
            {"passengers": 2},
            {"destination": "SIN"},
            {
                "departure_from": self.request.date_to + timedelta(days=1),
                "departure_to": self.request.date_to + timedelta(days=2),
            },
        ):
            response = await provider.search(query.model_copy(update=update))
            self.assertEqual(response.offers, ())
            self.assertIn("synthetic_demo_data", response.warnings)

    async def test_equivalent_currencies_have_equivalent_scores(self):
        config = PlannerConfig(price_references={"RUB": 60000.0, "USD": 600.0})
        usd = self.route.model_copy(
            update={
                "total_cost": Money(
                    amount=self.route.total_cost.amount / 100, currency="USD"
                )
            }
        )
        scorer = WeightedScorer()
        self.assertAlmostEqual(
            scorer.score(self.route, config).total, scorer.score(usd, config).total
        )

    async def test_unexpected_provider_error_cancels_sibling_searches(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        failing_query, hanging_query = self.queries[:2]

        class Provider:
            async def search(self, query):
                if query == failing_query:
                    await started.wait()
                    raise ValueError("Malformed provider payload")
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        search = TicketSearch(Provider(), PlannerConfig())
        with self.assertRaises(ExceptionGroup):
            await search.collect(
                (failing_query, hanging_query), self.request, SearchDiagnostics()
            )
        self.assertTrue(cancelled.is_set())
