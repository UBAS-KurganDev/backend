"""Bounded provider searches and chronological ticket combinations."""

import asyncio
from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.ports import FlightProvider, FlightProviderError
from ubas_backend.routing.schemas import (
    CityProfile,
    FlightQuery,
    FlightSearchResult,
    RouteRequest,
    TicketOffer,
)
from ubas_backend.routing.validation import OfferRejection, offer_rejection


def build_queries(
    request: RouteRequest, hub: CityProfile, config: PlannerConfig
) -> tuple[FlightQuery, ...]:
    first = FlightQuery(
        origin=request.origin,
        destination=hub.city_code,
        departure_from=request.date_from,
        departure_to=request.date_to,
        passengers=request.passengers,
        currency=request.currency,
        limit=config.max_offers_per_query,
        max_stopover_hours=config.max_stopover_hours,
    )
    # Include calendar shifts across time zones; final endpoints are checked later.
    onward = first.model_copy(
        update={
            "origin": hub.city_code,
            "destination": request.destination,
            "departure_from": request.date_from - timedelta(days=2),
            "departure_to": request.date_to + timedelta(days=2),
        }
    )
    queries: tuple[FlightQuery, ...] = (
        (first, onward) if config.allow_self_transfer else ()
    )
    if config.search_through_tickets:
        queries += (
            first.model_copy(
                update={
                    "destination": request.destination,
                    "stopover_city": hub.city_code,
                }
            ),
        )
    return queries


class TicketSearch:
    def __init__(self, provider: FlightProvider, config: PlannerConfig) -> None:
        self.provider = provider
        self.config = config
        # Shared across requests in this worker, rather than recreated per plan.
        self._semaphore = asyncio.Semaphore(config.max_concurrent_queries)

    async def _search(self, query: FlightQuery) -> FlightSearchResult:
        async with self._semaphore:
            try:
                async with asyncio.timeout(self.config.query_timeout_seconds):
                    return await self.provider.search(query)
            except FlightProviderError, TimeoutError:
                return FlightSearchResult(
                    complete=False,
                    warnings=(
                        f"flight_search_failed:{query.origin}:{query.destination}:"
                        f"{query.stopover_city or 'nonstop'}",
                    ),
                )

    async def collect(
        self,
        queries: tuple[FlightQuery, ...],
        request: RouteRequest,
        diagnostics: SearchDiagnostics,
        *,
        now: datetime | None = None,
    ) -> dict[FlightQuery, tuple[TicketOffer, ...]]:
        async with asyncio.TaskGroup() as group:
            tasks = {
                query: group.create_task(self._search(query))
                for query in dict.fromkeys(queries)
            }
        current = now or datetime.now(UTC)
        offers: dict[FlightQuery, tuple[TicketOffer, ...]] = {}
        for query, task in tasks.items():
            response = task.result()
            diagnostics.warnings.extend(response.warnings)
            if not response.complete:
                diagnostics.partial(
                    f"incomplete_search:{query.origin}:{query.destination}:"
                    f"{query.stopover_city or 'nonstop'}"
                )
            valid: list[TicketOffer] = []
            for offer in response.offers:
                rejection = offer_rejection(offer, query, request, current)
                if rejection is None:
                    valid.append(offer)
                    continue
                diagnostics.rejected[rejection.value] += 1
                if rejection is OfferRejection.CONTRACT:
                    diagnostics.partial("provider_contract_mismatch")
            valid.sort(key=lambda offer: (offer.total_price.amount, offer.offer_id))
            if len(valid) > query.limit:
                diagnostics.partial(f"offer_limit:{query.origin}:{query.destination}")
            offers[query] = tuple(valid[: query.limit])
        return offers


def _split_combinations(
    inbound: tuple[TicketOffer, ...],
    outbound: tuple[TicketOffer, ...],
    config: PlannerConfig,
) -> Iterator[tuple[TicketOffer, ...]]:
    onward = sorted(
        outbound, key=lambda offer: offer.segments[0].departure_at.astimezone(UTC)
    )
    departures = [offer.segments[0].departure_at.astimezone(UTC) for offer in onward]
    for first in inbound:
        arrival = first.segments[-1].arrival_at.astimezone(UTC)
        start = bisect_left(
            departures, arrival + timedelta(hours=config.min_stopover_hours)
        )
        end = bisect_right(
            departures, arrival + timedelta(hours=config.max_stopover_hours)
        )
        for second in onward[start:end]:
            yield first, second


def ticket_combinations(
    queries: tuple[FlightQuery, ...],
    offers: dict[FlightQuery, tuple[TicketOffer, ...]],
    config: PlannerConfig,
) -> Iterator[tuple[TicketOffer, ...]]:
    through = next((query for query in queries if query.stopover_city), None)
    through_offers = iter(offers[through] if through else ())
    split: Iterator[tuple[TicketOffer, ...]] = iter(())
    if config.allow_self_transfer:
        inbound, outbound = (query for query in queries if not query.stopover_city)
        split = _split_combinations(offers[inbound], offers[outbound], config)
    through_done = split_done = False
    while not (through_done and split_done):
        if not through_done:
            offer = next(through_offers, None)
            if offer is None:
                through_done = True
            else:
                yield (offer,)
        if not split_done:
            pair = next(split, None)
            if pair is None:
                split_done = True
            else:
                yield pair
