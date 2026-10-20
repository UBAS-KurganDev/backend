"""Regression checks for planner behavior and structured cancellation."""

import asyncio
import contextlib
import io
import unittest
from datetime import timedelta
from decimal import Decimal

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.demo import main as demo
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.offers import TicketSearch, build_queries
from ubas_backend.routing.schemas import Money, PlanResult, RouteRequest
from ubas_backend.routing.scoring import WeightedScorer
from ubas_backend.routing.validation import OfferRejection, offer_rejection


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
