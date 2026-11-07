"""Cancellation-safe collection of planner-generated single-date queries."""

import asyncio
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.ports import (
    FlightProvider,
    FlightProviderError,
)
from ubas_backend.routing.schemas import (
    FlightQuery,
    FlightSearchResult,
    RouteRequest,
    TicketOffer,
)
from ubas_backend.routing.validation import OfferRejection, offer_rejection


def build_queries(
    request: RouteRequest,
    city_codes: tuple[str, ...],
    config: PlannerConfig,
) -> Iterator[FlightQuery]:
    pairs = [(request.origin, request.destination, 2)]
    for hub in city_codes:
        if hub not in (request.origin, request.destination):
            pairs.extend(((request.origin, hub, 1), (hub, request.destination, 1)))
    for origin, destination, max_segments in pairs:
        # Only intermediate departures need conservative timezone padding.
        padding = 0 if origin == request.origin else 2
        first = request.date_from - timedelta(days=padding)
        days = (request.date_to - request.date_from).days + 1 + 2 * padding
        for offset in range(days):
            yield FlightQuery(
                origin=origin,
                destination=destination,
                departure_date=first + timedelta(days=offset),
                passengers=1,
                currency="RUB",
                limit=config.max_offers_per_query,
                max_segments=max_segments,
            )


def unique_queries(queries: Iterable[FlightQuery]) -> Iterator[FlightQuery]:
    seen: set[FlightQuery] = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            yield query


class TicketSearch:
    def __init__(self, provider: FlightProvider, config: PlannerConfig) -> None:
        self.provider = provider
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_queries)

    async def _search(
        self, query: FlightQuery, diagnostics: SearchDiagnostics
    ) -> FlightSearchResult | None:
        async with self._semaphore:
            diagnostics.queries_started += 1
            try:
                async with asyncio.timeout(self.config.query_timeout_seconds):
                    response = await self.provider.search(query)
                diagnostics.queries_finished += 1
                diagnostics.offers_received += len(response.offers)
                if not response.complete or len(response.offers) > query.limit:
                    diagnostics.queries_incomplete += 1
                return response
            except FlightProviderError, TimeoutError:
                diagnostics.queries_finished += 1
                diagnostics.queries_failed += 1
                return None
            except asyncio.CancelledError:
                diagnostics.queries_cancelled += 1
                raise
            except Exception:
                diagnostics.queries_finished += 1
                diagnostics.queries_failed += 1
                raise

    async def collect(
        self,
        queries: Iterable[FlightQuery],
        request: RouteRequest,
        diagnostics: SearchDiagnostics,
        *,
        now: datetime | None = None,
    ) -> tuple[TicketOffer, ...]:
        initial = iter(unique_queries(queries))
        # The per-search offer budget remains shared by all dates of a city pair.
        counts: dict[tuple[str, str], int] = {}
        offers: list[TicketOffer] = []
        calls = 0
        stopped = False

        async def consume() -> None:
            nonlocal calls, stopped
            while not stopped:
                # No await between claiming a query and reserving its budget.
                query = next(initial, None)
                if query is None:
                    return
                if calls >= self.config.max_provider_queries:
                    diagnostics.partial("provider_query_limit")
                    stopped = True
                    return
                calls += 1
                response = await self._search(query, diagnostics)
                root = (query.origin, query.destination)
                if response is None:
                    diagnostics.partial(
                        f"provider_unavailable:{query.origin}:{query.destination}"
                    )
                    continue
                diagnostics.warnings.extend(response.warnings)
                incomplete = not response.complete or len(response.offers) > query.limit
                if incomplete:
                    diagnostics.partial(
                        f"incomplete_provider:{query.origin}:{query.destination}"
                    )
                current = now or datetime.now(UTC)
                for offer in response.offers:
                    rejection = offer_rejection(offer, query, request, current)
                    if rejection is not None:
                        diagnostics.rejected[rejection.value] += 1
                        if rejection is OfferRejection.CONTRACT:
                            diagnostics.partial("provider_contract_mismatch")
                        continue
                    if counts.get(root, 0) >= self.config.max_offers_per_search:
                        diagnostics.partial("offers_per_search_limit")
                        break
                    if len(offers) >= self.config.max_total_offers:
                        diagnostics.partial("total_offer_limit")
                        stopped = True
                        return
                    # Keep all distinct response occurrences, even identical offers.
                    offers.append(offer)
                    diagnostics.offers_accepted += 1
                    counts[root] = counts.get(root, 0) + 1
                await asyncio.sleep(0)

        async with asyncio.TaskGroup() as group:
            for _ in range(self.config.max_concurrent_queries):
                group.create_task(consume())
        return tuple(offers)
