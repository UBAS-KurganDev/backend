"""Cancellation-safe collection with disjoint adaptive date partitions."""

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.ports import FlightProvider, FlightProviderError
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
) -> tuple[FlightQuery, ...]:
    pairs = [(request.origin, request.destination)]
    pairs.extend(
        (origin, destination)
        for origin in city_codes
        for destination in city_codes
        if origin != destination and (origin, destination) != pairs[0]
    )
    return tuple(
        FlightQuery(
            origin=origin,
            destination=destination,
            # An intermediate local date can lie outside the endpoint local window.
            departure_from=request.date_from - timedelta(days=2),
            departure_to=request.date_to + timedelta(days=2),
            passengers=1,
            currency="RUB",
            limit=config.max_offers_per_query,
        )
        for origin, destination in pairs
    )


def bisect_dates(query: FlightQuery) -> tuple[FlightQuery, FlightQuery]:
    middle = query.departure_from + (query.departure_to - query.departure_from) // 2
    return (
        query.model_copy(update={"departure_to": middle}),
        query.model_copy(update={"departure_from": middle + timedelta(days=1)}),
    )


class TicketSearch:
    def __init__(self, provider: FlightProvider, config: PlannerConfig) -> None:
        self.provider = provider
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_queries)

    async def _search(self, query: FlightQuery) -> FlightSearchResult | None:
        async with self._semaphore:
            try:
                async with asyncio.timeout(self.config.query_timeout_seconds):
                    return await self.provider.search(query)
            except FlightProviderError, TimeoutError:
                return None

    async def collect(
        self,
        queries: tuple[FlightQuery, ...],
        request: RouteRequest,
        diagnostics: SearchDiagnostics,
        *,
        now: datetime | None = None,
    ) -> tuple[TicketOffer, ...]:
        roots = tuple(dict.fromkeys(queries))
        pending = deque((index, query) for index, query in enumerate(roots))
        counts = [0] * len(roots)
        offers: list[TicketOffer] = []
        calls = 0
        while pending:
            remaining = self.config.max_provider_queries - calls
            if remaining <= 0:
                diagnostics.partial("provider_query_limit")
                break
            batch = [
                pending.popleft()
                for _ in range(
                    min(remaining, len(pending), self.config.max_concurrent_queries)
                )
            ]
            calls += len(batch)
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._search(query)) for _, query in batch]
            for (root, query), task in zip(batch, tasks, strict=True):
                response = task.result()
                if response is None:
                    diagnostics.partial(
                        f"provider_unavailable:{query.origin}:{query.destination}"
                    )
                    continue
                diagnostics.warnings.extend(response.warnings)
                incomplete = not response.complete or len(response.offers) > query.limit
                if (
                    incomplete
                    and self.config.refine_date_ranges
                    and query.departure_from < query.departure_to
                ):
                    # Replace the parent response, do not union it with children:
                    # repeated transport observations must not manufacture duplicates.
                    pending.extend((root, child) for child in bisect_dates(query))
                    continue
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
                    if counts[root] >= self.config.max_offers_per_search:
                        diagnostics.partial("offers_per_search_limit")
                        break
                    if len(offers) >= self.config.max_total_offers:
                        diagnostics.partial("total_offer_limit")
                        return tuple(offers)
                    # Keep all distinct response occurrences, even identical offers.
                    offers.append(offer)
                    counts[root] += 1
            await asyncio.sleep(0)
        return tuple(offers)
