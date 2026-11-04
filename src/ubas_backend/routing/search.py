"""Best-first search with an admissible price bound and UTC departure indexes."""

import asyncio
import heapq
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from ubas_backend.routing.candidates import (
    CandidateBuilder,
    CandidateRejection,
    local_date,
)
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.costs import CostCalculator
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.schemas import RankedRoute, RouteRequest, TicketOffer
from ubas_backend.routing.selection import CheapestResults


@dataclass(frozen=True, slots=True)
class Path:
    indices: tuple[int, ...]
    tickets: tuple[TicketOffer, ...]
    segment_count: int
    ticket_cost: Decimal

    @property
    def city(self) -> str:
        return self.tickets[-1].segments[-1].destination.city_code

    @property
    def arrival(self) -> datetime:
        return self.tickets[-1].segments[-1].arrival_at.astimezone(UTC)


def price_bounds(
    offers: tuple[TicketOffer, ...],
    destination: str,
    costs: CostCalculator,
) -> list[dict[str, Decimal]]:
    """Minimum ticket cost with <= r flight segments, ignoring all times/visas.

    Relaxing constraints can only decrease the minimum. An atomic through ticket
    consumes all its segments but charges its fare once. Missing prices are zero.
    """
    bounds: list[dict[str, Decimal]] = [{destination: Decimal(0)}]
    for remaining in range(1, 5):
        current = dict(bounds[-1])
        for offer in offers:
            size = len(offer.segments)
            if size > remaining:
                continue
            origin = offer.segments[0].origin.city_code
            target = offer.segments[-1].destination.city_code
            tail = bounds[remaining - size].get(target)
            if tail is not None:
                price = costs.ticket_amount(offer) + tail
                current[origin] = min(current.get(origin, Decimal("Infinity")), price)
        bounds.append(current)
    return bounds


class RouteSearch:
    def __init__(self, config: PlannerConfig, builder: CandidateBuilder) -> None:
        self.config = config
        self.builder = builder

    async def run(
        self,
        offers: tuple[TicketOffer, ...],
        request: RouteRequest,
        diagnostics: SearchDiagnostics,
        now: datetime,
    ) -> tuple[RankedRoute, ...]:
        by_city: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for index, offer in enumerate(offers):
            by_city[offer.segments[0].origin.city_code].append(
                (offer.segments[0].departure_at.astimezone(UTC), index)
            )
        for items in by_city.values():
            items.sort()
        departures = {
            city: [item[0] for item in items] for city, items in by_city.items()
        }
        bounds = price_bounds(offers, request.destination, self.builder.costs)
        prices = [self.builder.costs.ticket_amount(offer) for offer in offers]
        frontier: list[tuple[Decimal, int, Path]] = []
        sequence = count()
        results = CheapestResults()

        def push(path: Path) -> bool:
            tail = bounds[4 - path.segment_count].get(path.city)
            if tail is None:
                diagnostics.rejected["unreachable_destination"] += 1
                return True
            bound = path.ticket_cost + tail
            # Ties have no prescribed secondary ordering, but strict comparison
            # retains them until enough actual complete candidates have been found.
            if bound > results.threshold:
                diagnostics.rejected["price_bound"] += 1
                return True
            stops = self.builder.stopovers(path.tickets)
            if isinstance(stops, CandidateRejection):
                diagnostics.rejected[stops.value] += 1
                return True
            if len(frontier) >= self.config.max_frontier:
                diagnostics.partial("frontier_limit")
                return False
            heapq.heappush(frontier, (bound, next(sequence), path))
            return True

        for _, index in by_city.get(request.origin, []):
            offer = offers[index]
            if (
                request.date_from
                <= local_date(offer.segments[0]).date()
                <= request.date_to
            ):
                push(Path((index,), (offer,), len(offer.segments), prices[index]))
            if index % 64 == 0:
                await asyncio.sleep(0)
            if not diagnostics.complete:
                break
        nodes = 0
        while frontier and diagnostics.complete:
            lower, _, path = heapq.heappop(frontier)
            if lower > results.threshold:
                diagnostics.rejected["price_bound"] += 1 + len(frontier)
                break
            if nodes >= self.config.max_search_nodes:
                diagnostics.partial("search_node_limit")
                break
            nodes += 1
            if nodes % 64 == 0:
                await asyncio.sleep(0)
            if path.city == request.destination:
                if diagnostics.evaluated >= self.config.max_candidates:
                    diagnostics.partial("candidate_limit")
                    break
                diagnostics.evaluated += 1
                candidate = self.builder.build(path.tickets, request, now, path.indices)
                if isinstance(candidate, CandidateRejection):
                    diagnostics.rejected[candidate.value] += 1
                else:
                    results.add(candidate)
            if path.segment_count == 4:
                continue
            items = by_city.get(path.city, [])
            start = bisect_right(departures.get(path.city, []), path.arrival)
            for offset in range(start, len(items)):
                _, index = items[offset]
                offer = offers[index]
                size = path.segment_count + len(offer.segments)
                if size <= 4:
                    admitted = push(
                        Path(
                            (*path.indices, index),
                            (*path.tickets, offer),
                            size,
                            path.ticket_cost + prices[index],
                        )
                    )
                    if not admitted:
                        break
                if offset % 64 == 0:
                    await asyncio.sleep(0)
        return results.routes
