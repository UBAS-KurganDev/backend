"""MVP orchestration; successful responses contain only completed searches."""

import asyncio
from datetime import UTC, datetime

from ubas_backend.routing.candidates import CandidateBuilder
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.costs import CatalogVisaCosts, CostCalculator
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.hubs import discover_cities, select_cities
from ubas_backend.routing.offers import TicketSearch, build_queries
from ubas_backend.routing.policy import InvalidRouteRequest, RequestPolicy
from ubas_backend.routing.ports import (
    CityGeography,
    CityRepository,
    CitySourceError,
    FlightProvider,
    TopologyRepository,
    VisaCostResolver,
)
from ubas_backend.routing.schemas import (
    CityProfile,
    PlanResult,
    RouteRequest,
    SearchScope,
)
from ubas_backend.routing.search import RouteSearch


class IncompleteSearchError(Exception):
    """No partial cards may be published as a successful MVP result."""

    def __init__(self, warnings: tuple[str, ...]) -> None:
        self.warnings = warnings
        super().__init__("Search did not finish: " + ", ".join(warnings))


class RoutePlanner:
    def __init__(
        self,
        flights: FlightProvider,
        topology: TopologyRepository,
        cities: CityRepository,
        *,
        config: PlannerConfig | None = None,
        visa_costs: VisaCostResolver | None = None,
        geography: CityGeography | None = None,
    ) -> None:
        self.config = config or PlannerConfig()
        self.policy = RequestPolicy(self.config)
        self.topology = topology
        self.cities = cities
        self.geography = geography
        self.visas = visa_costs or CatalogVisaCosts()
        self.search = TicketSearch(flights, self.config)

    async def _profiles(
        self,
        codes: tuple[str, ...],
        diagnostics: SearchDiagnostics,
    ) -> dict[str, CityProfile]:
        try:
            async with asyncio.timeout(self.config.query_timeout_seconds):
                profiles = await self.cities.get_many(codes)
        except CitySourceError, TimeoutError:
            profiles = ()
        found = {
            profile.city_code: profile
            for profile in profiles
            if profile.city_code in codes
        }
        if set(codes) - found.keys():
            diagnostics.warnings.append("missing_tourism_scores_use_zero")
        return found

    async def plan(
        self,
        request: RouteRequest,
        *,
        now: datetime | None = None,
    ) -> PlanResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(UTC)
        self.policy.validate(request, now=current)
        try:
            async with asyncio.timeout(self.config.search_timeout_seconds):
                return await self._plan(request, current, now)
        except TimeoutError as error:
            raise IncompleteSearchError(("search_timeout",)) from error

    async def _plan(
        self,
        request: RouteRequest,
        current: datetime,
        fixed_now: datetime | None,
    ) -> PlanResult:
        diagnostics = SearchDiagnostics()
        if self.geography is not None:
            zone = await self.geography.timezone(request.origin)
            if zone is None:
                raise InvalidRouteRequest("Unknown origin city timezone.")
            self.policy.validate(request, now=current, origin_timezone=zone)
        candidates = await discover_cities(
            self.topology, request, self.config, diagnostics
        )
        if not diagnostics.complete:
            raise IncompleteSearchError(tuple(diagnostics.warnings))
        profiles = await self._profiles(candidates, diagnostics)
        cities = await select_cities(
            self.topology, candidates, profiles, self.config, diagnostics
        )
        if not diagnostics.complete:
            raise IncompleteSearchError(tuple(diagnostics.warnings))
        origin = profiles.get(request.origin)
        if origin:
            self.policy.validate(request, now=current, origin_timezone=origin.timezone)
        offers = await self.search.collect(
            build_queries(request, cities, self.config),
            request,
            diagnostics,
            now=fixed_now,
        )
        if not diagnostics.complete:
            raise IncompleteSearchError(tuple(diagnostics.warnings))
        if origin is None:
            origin_airports = {
                offer.segments[0].origin
                for offer in offers
                if offer.segments[0].origin.city_code == request.origin
            }
            for airport in origin_airports:
                self.policy.validate(
                    request, now=current, origin_timezone=airport.timezone
                )
        missing = {
            segment.destination.city_code
            for offer in offers
            for segment in offer.segments[:-1]
        } - set(candidates)
        if missing:
            profiles.update(await self._profiles(tuple(sorted(missing)), diagnostics))
        builder = CandidateBuilder(
            self.config, profiles, CostCalculator(self.config), self.visas
        )
        ranked = await RouteSearch(self.config, builder).run(
            offers, request, diagnostics, fixed_now or datetime.now(UTC)
        )
        if not diagnostics.complete:
            raise IncompleteSearchError(tuple(diagnostics.warnings))
        return PlanResult(
            routes=ranked,
            searched_cities=cities,
            search_scope=SearchScope(
                candidate_hubs=len(candidates) - 2,
                selected_hubs=cities[2:],
                max_hubs=self.config.max_hubs,
                limited=len(candidates) > len(cities),
            ),
            evaluated_candidates=diagnostics.evaluated,
            rejected=dict(diagnostics.rejected),
            complete=True,
            warnings=tuple(dict.fromkeys(diagnostics.warnings)),
            generated_at=fixed_now or datetime.now(UTC),
        )
