"""FlightProvider adapter: city airports, disjoint dates, honest completeness."""

import asyncio
from datetime import UTC, date, datetime
from itertools import product
from zoneinfo import ZoneInfo

from ubas_backend.agent_scraper.airports import AirportDirectory, default_directory
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.models import SearchRequest
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.ports import FlightProviderError
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult, TicketOffer


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
        service: FlightSearchService | None = None,
        *,
        airports: AirportDirectory | None = None,
    ) -> None:
        self._service = service or FlightSearchService()
        self._airports = airports or default_directory()

    async def _search_airports(self, request: SearchRequest) -> FlightSearchResult:
        captured = await self._service.get_offers(request)
        return map_response(
            captured, request, airport_lookup=self._airports.airport_for_code
        )

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        if query.passengers != 1 or query.currency != "RUB":
            raise FlightProviderError("agent_unsupported_query")
        if query.departure_from != query.departure_to:
            # TicketSearch replaces this response with disjoint single-day requests.
            return FlightSearchResult(
                complete=False, warnings=("agent_requires_single_day",)
            )
        origins = self._airports.airports_for_city(query.origin)
        destinations = self._airports.airports_for_city(query.destination)
        if not origins or not destinations:
            raise FlightProviderError("agent_unsupported_city")
        today = datetime.now(UTC).astimezone(ZoneInfo(origins[0].timezone)).date()
        if query.departure_from < today:
            return FlightSearchResult()
        # The source URL has no year. Do not silently search the wrong year.
        if _next_occurrence(query.departure_from, today) != query.departure_from:
            raise FlightProviderError("agent_unsupported_year")
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self._search_airports(
                            SearchRequest(
                                origin=origin.code,
                                destination=destination.code,
                                depart_date=query.departure_from,
                            )
                        )
                    )
                    for origin, destination in product(origins, destinations)
                ]
        except ExceptionGroup as error:
            raise FlightProviderError("agent_airport_search_failed") from error
        offers: list[TicketOffer] = []
        warnings: set[str] = set()
        complete = True
        for task in tasks:
            result = task.result()
            complete &= result.complete
            warnings.update(result.warnings)
            offers.extend(
                offer
                for offer in result.offers
                if len(offer.segments) <= query.max_segments
            )
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


def create_flight_provider() -> AgentFlightProvider:
    settings = Settings()
    airports = None
    if settings.topology_snapshot_path is not None:
        from ubas_backend.topology.models import Snapshot

        snapshot = Snapshot.model_validate_json(
            settings.topology_snapshot_path.read_bytes()
        )
        airports = AirportDirectory(
            snapshot.airports,
            frozenset(
                airport.code
                for airport in snapshot.airports
                if airport.flightable and airport.city_code in snapshot.covered_cities
            ),
            fallback=default_directory(),
        )
    return AgentFlightProvider(
        FlightSearchService(settings=settings), airports=airports
    )
