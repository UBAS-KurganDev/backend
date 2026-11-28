"""FlightProvider adapter: city airports, disjoint dates, honest completeness."""

import asyncio
import logging
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from ubas_backend.agent_scraper.airports import AirportDirectory, default_directory
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.http_transport import run_http_scraper
from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.models import SearchRequest
from ubas_backend.agent_scraper.nodes import NodeSearchService
from ubas_backend.agent_scraper.ports import OfferService
from ubas_backend.agent_scraper.scraper_agent import run_agent_scraper
from ubas_backend.agent_scraper.service import (
    FlightSearchService,
    Scrape,
)
from ubas_backend.routing.ports import FlightProviderError
from ubas_backend.routing.schemas import (
    Airport,
    FlightQuery,
    FlightSearchResult,
    TicketOffer,
)

logger = logging.getLogger(__name__)


def _next_occurrence(day: date, today: date) -> date | None:
    # February 29 may be up to 8 years away, e.g. 2096 -> 2104.
    for year in range(today.year, today.year + 9):
        try:
            candidate = day.replace(year=year)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


class AgentFlightProvider:
    def __init__(
        self,
        service: OfferService | None = None,
        *,
        airports: AirportDirectory | None = None,
        settings: Settings | None = None,
    ) -> None:
        resolved = settings or Settings()
        if service is not None:
            self._service = service
        else:
            scrape: Scrape = (
                run_http_scraper if resolved.transport == "http" else run_agent_scraper
            )
            self._service = FlightSearchService(settings=resolved, scrape=scrape)
        self._airports = airports or default_directory()

    async def _search_airports(
        self, request: SearchRequest, max_segments: int
    ) -> FlightSearchResult:
        try:
            captured = await self._service.get_offers(request)
        except FlightProviderError:
            logger.warning(
                "agent_airport_pair_failed origin=%s destination=%s date=%s",
                request.origin,
                request.destination,
                request.depart_date,
            )
            raise
        return map_response(
            captured,
            request,
            airport_lookup=self._airports.airport_for_code,
            max_segments=max_segments,
        )

    def _resolve(self, code: str) -> tuple[Airport, ...]:
        """City airports for a city code, or the single requested airport."""
        airports = self._airports.airports_for_city(code)
        if airports:
            return airports
        single = self._airports.airport_for_code(code)
        return (single,) if single else ()

    def _select(
        self,
        airports: tuple[Airport, ...],
        codes: tuple[str, ...],
    ) -> tuple[Airport, ...]:
        """Narrow a city's airports to the ones the request asked for."""
        if not codes:
            return airports
        wanted = set(codes)
        return tuple(airport for airport in airports if airport.code in wanted)

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        if query.passengers != 1 or query.currency != "RUB":
            raise FlightProviderError("agent_unsupported_query")
        origins = self._resolve(query.origin)
        destinations = self._resolve(query.destination)
        if not query.city_search:
            # Airport mode may narrow the city to requested airports.
            origins = self._select(origins, query.origin_airports)
            destinations = self._select(destinations, query.destination_airports)
        if not origins or not destinations:
            raise FlightProviderError("agent_unsupported_city")
        today = datetime.now(UTC).astimezone(ZoneInfo(origins[0].timezone)).date()
        if query.departure_date < today:
            return FlightSearchResult()
        # The source URL has no year. Do not silently search the wrong year.
        if _next_occurrence(query.departure_date, today) != query.departure_date:
            raise FlightProviderError("agent_unsupported_year")
        # City-level search covers every airport combination in one request:
        # the source aggregates across airports of the city codes itself.
        # Airport mode keeps the explicit airport-pair expansion instead.
        pairs: tuple[SearchRequest, ...]
        if query.city_search:
            pairs = (
                SearchRequest(
                    origin=query.origin,
                    destination=query.destination,
                    depart_date=query.departure_date,
                ),
            )
        else:
            pairs = tuple(
                SearchRequest(
                    origin=origin.code,
                    destination=destination.code,
                    depart_date=query.departure_date,
                )
                for origin in origins
                for destination in destinations
            )
        try:
            results = await asyncio.gather(
                *(
                    self._search_airports(request, query.max_segments)
                    for request in pairs
                ),
                return_exceptions=True,
            )
        except FlightProviderError as error:
            logger.warning(
                "agent_city_pair_failed origin=%s destination=%s date=%s",
                query.origin,
                query.destination,
                query.departure_date,
            )
            raise FlightProviderError("agent_airport_search_failed") from error
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures and query.city_search:
            # City search is all-or-nothing: any failure means no coverage.
            raise FlightProviderError("agent_airport_search_failed") from failures[0]
        offers: list[TicketOffer] = []
        warnings: set[str] = set()
        complete = True
        for result in results:
            if isinstance(result, BaseException):
                complete = False
                warnings.add("incomplete_provider")
                continue
            complete &= result.complete
            warnings.update(result.warnings)
            offers.extend(
                offer
                for offer in result.offers
                if len(offer.segments) <= query.max_segments
            )
        origin_codes = {airport.code for airport in origins}
        destination_codes = {airport.code for airport in destinations}
        offers = [
            offer
            for offer in offers
            if offer.segments[0].origin.code in origin_codes
            and offer.segments[-1].destination.code in destination_codes
        ]
        if len(offers) > query.limit:
            complete = False
            warnings.add("agent_offer_limit")
        return FlightSearchResult(
            offers=tuple(offers[: query.limit]),
            complete=complete,
            warnings=tuple(sorted(warnings)),
        )

    async def aclose(self) -> None:
        await self._service.aclose()


def _airport_directory(settings: Settings) -> AirportDirectory | None:
    if settings.topology_snapshot_path is None:
        return None
    from ubas_backend.topology.models import Snapshot

    snapshot = Snapshot.model_validate_json(
        settings.topology_snapshot_path.read_bytes()
    )
    return AirportDirectory(
        snapshot.airports,
        frozenset(
            airport.code
            for airport in snapshot.airports
            if airport.flightable and airport.city_code in snapshot.covered_cities
        ),
        fallback=default_directory(),
    )


def create_flight_provider() -> AgentFlightProvider:
    settings = Settings()
    airports = _airport_directory(settings)
    service: OfferService
    if settings.transport == "http":
        # Pure-HTTP transport is in-process; browser nodes are not involved.
        service = FlightSearchService(settings=settings, scrape=run_http_scraper)
    else:
        service = (
            NodeSearchService(settings)
            if settings.node_urls
            else FlightSearchService(settings=settings)
        )
    return AgentFlightProvider(service, airports=airports)
