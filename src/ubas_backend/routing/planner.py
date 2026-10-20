"""Search orchestration; filtering, construction and ranking live in components."""

import asyncio
from collections import deque
from datetime import UTC, datetime

from ubas_backend.routing.candidates import CandidateBuilder, CandidateRejection
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.daytime import LocalDaytimeCalculator
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.hubs import HubSelector
from ubas_backend.routing.offers import TicketSearch, build_queries, ticket_combinations
from ubas_backend.routing.policy import RequestPolicy
from ubas_backend.routing.ports import (
    CityRepository,
    DaytimeCalculator,
    FlightProvider,
    RouteScorer,
    TopologyRepository,
)
from ubas_backend.routing.schemas import PlanResult, RankedRoute, RouteRequest
from ubas_backend.routing.scoring import WeightedScorer
from ubas_backend.routing.selection import select_routes


class RoutePlanner:
    def __init__(
        self,
        flights: FlightProvider,
        topology: TopologyRepository,
        cities: CityRepository,
        *,
        config: PlannerConfig | None = None,
        scorer: RouteScorer | None = None,
        daytime: DaytimeCalculator | None = None,
    ) -> None:
        self.config = config or PlannerConfig()
        self.scorer = scorer or WeightedScorer()
        self.policy = RequestPolicy(self.config)
        self.hubs = HubSelector(topology, cities, self.config)
        self.search = TicketSearch(flights, self.config)
        self.candidates = CandidateBuilder(
            self.config, daytime or LocalDaytimeCalculator()
        )

    async def plan(
        self, request: RouteRequest, *, now: datetime | None = None
    ) -> PlanResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(UTC)
        self.policy.validate(request)
        diagnostics = SearchDiagnostics()
        hubs = await self.hubs.select(request, current, diagnostics)
        queries_by_city = {
            hub.city_code: build_queries(request, hub, self.config) for hub in hubs
        }
        queries = tuple(query for items in queries_by_city.values() for query in items)
        offers = await self.search.collect(queries, request, diagnostics, now=now)
        current = now.astimezone(UTC) if now else datetime.now(UTC)
        iterators = deque(
            (
                hub,
                ticket_combinations(
                    queries_by_city[hub.city_code], offers, self.config
                ),
            )
            for hub in hubs
        )
        ranked: list[RankedRoute] = []
        while iterators:
            hub, combinations = iterators.popleft()
            tickets = next(combinations, None)
            if tickets is None:
                continue
            if diagnostics.evaluated >= self.config.max_candidates:
                diagnostics.partial("candidate_limit")
                break
            iterators.append((hub, combinations))
            diagnostics.evaluated += 1
            # Bound CPU stretches so heartbeat and cancellation can make progress.
            if diagnostics.evaluated % 64 == 0:
                await asyncio.sleep(0)
            candidate = self.candidates.build(tickets, hub, request, current)
            if isinstance(candidate, CandidateRejection):
                diagnostics.rejected[candidate.value] += 1
                continue
            ranked.append(
                RankedRoute(
                    route=candidate, score=self.scorer.score(candidate, self.config)
                )
            )
        return PlanResult(
            routes=select_routes(
                ranked,
                limit=request.limit,
                max_per_city=self.config.max_results_per_city,
            ),
            searched_cities=tuple(hub.city_code for hub in hubs),
            evaluated_candidates=diagnostics.evaluated,
            rejected=dict(diagnostics.rejected),
            complete=diagnostics.complete,
            warnings=tuple(dict.fromkeys(diagnostics.warnings)),
            generated_at=current,
        )
