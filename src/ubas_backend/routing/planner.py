"""Bounded one-stop search, independent of databases and ticket transports."""

import asyncio
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import Counter, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.daytime import LocalDaytimeCalculator
from ubas_backend.routing.models import (
    CityProfile,
    FlightQuery,
    FlightSearchResult,
    Money,
    PlanResult,
    RankedRoute,
    RouteCandidate,
    RouteRequest,
    TicketOffer,
    VisaPolicy,
    VisitMetrics,
)
from ubas_backend.routing.ports import (
    CityRepository,
    DaytimeCalculator,
    FlightProvider,
    FlightProviderError,
    RouteScorer,
    TopologyRepository,
)
from ubas_backend.routing.scoring import WeightedScorer


@dataclass
class _SearchState:
    rejected: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    evaluated: int = 0

    def partial(self, message: str) -> None:
        self.complete = False
        self.warnings.append(message)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


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
        self.flights = flights
        self.topology = topology
        self.cities = cities
        self.config = config or PlannerConfig()
        self.scorer = scorer or WeightedScorer()
        self.daytime = daytime or LocalDaytimeCalculator()

    async def plan(
        self, request: RouteRequest, *, now: datetime | None = None
    ) -> PlanResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = _utc(current)
        if (request.date_to - request.date_from).days + 1 > self.config.max_search_days:
            raise ValueError("Search date range exceeds max_search_days")
        state = _SearchState()
        hubs = await self._hubs(request, current, state)
        queries = tuple(query for hub in hubs for query in self._queries(request, hub))
        semaphore = asyncio.Semaphore(self.config.max_concurrent_queries)

        async def search(query: FlightQuery) -> tuple[FlightQuery, FlightSearchResult]:
            async with semaphore:
                try:
                    async with asyncio.timeout(self.config.query_timeout_seconds):
                        result = await self.flights.search(query)
                except FlightProviderError, TimeoutError:
                    # Do not expose transport internals, credentials, or provider URLs.
                    result = FlightSearchResult(
                        complete=False,
                        warnings=(
                            f"flight_search_failed:{query.origin}:{query.destination}:"
                            f"{query.stopover_city or 'nonstop'}",
                        ),
                    )
                return query, result

        responses = await asyncio.gather(*(search(query) for query in queries))
        # A real provider obtains quotes during the calls above. Validate against
        # their completion time, not the earlier instant when planning began.
        current = _utc(now) if now is not None else datetime.now(UTC)
        offers_by_query: dict[FlightQuery, tuple[TicketOffer, ...]] = {}
        for query, response in responses:
            state.warnings.extend(response.warnings)
            if not response.complete:
                state.partial(
                    f"incomplete_search:{query.origin}:{query.destination}:"
                    f"{query.stopover_city or 'nonstop'}"
                )
            valid = [
                offer
                for offer in response.offers
                if self._valid_offer(offer, query, current, request, state)
            ]
            valid.sort(key=lambda offer: (offer.total_price.amount, offer.offer_id))
            if len(valid) > query.limit:
                state.partial(f"offer_limit:{query.origin}:{query.destination}")
            offers_by_query[query] = tuple(valid[: query.limit])

        # Round-robin across cities prevents one busy hub consuming the entire budget.
        iterators = deque(
            (hub, self._combinations(request, hub, offers_by_query)) for hub in hubs
        )
        ranked: list[RankedRoute] = []
        while iterators:
            hub, combinations = iterators.popleft()
            tickets = next(combinations, None)
            if tickets is None:
                continue
            if state.evaluated >= self.config.max_candidates:
                state.partial("candidate_limit")
                break
            iterators.append((hub, combinations))
            state.evaluated += 1
            candidate = self._candidate(tickets, hub, request, current, state)
            if candidate is not None:
                ranked.append(
                    RankedRoute(
                        route=candidate,
                        score=self.scorer.score(candidate, self.config),
                    )
                )

        ranked.sort(
            key=lambda item: (
                -item.score.total,
                item.route.total_cost.amount,
                item.route.metrics.total_trip_hours,
                item.route.id,
            )
        )
        selected: list[RankedRoute] = []
        seen: set[str] = set()
        per_city: Counter[str] = Counter()
        for item in ranked:
            route = item.route
            signature = self._signature(route)
            if signature in seen:
                continue
            seen.add(signature)
            if per_city[route.stopover.city_code] >= self.config.max_results_per_city:
                continue
            per_city[route.stopover.city_code] += 1
            selected.append(item)
            if len(selected) == request.limit:
                break
        return PlanResult(
            routes=tuple(selected),
            searched_cities=tuple(hub.city_code for hub in hubs),
            evaluated_candidates=state.evaluated,
            rejected=dict(state.rejected),
            complete=state.complete,
            warnings=tuple(dict.fromkeys(state.warnings)),
            generated_at=current,
        )

    async def _hubs(
        self, request: RouteRequest, now: datetime, state: _SearchState
    ) -> list[CityProfile]:
        connected = set(
            await self.topology.connecting_cities(request.origin, request.destination)
        )
        connected.difference_update(
            (request.origin, request.destination, *request.excluded_cities)
        )
        profiles = await self.cities.get_many(tuple(sorted(connected)))
        found = {profile.city_code for profile in profiles}
        if connected - found:
            state.partial(
                "missing_city_profiles:" + ",".join(sorted(connected - found))
            )
        hubs = []
        for profile in profiles:
            if profile.city_code not in connected:
                continue
            if profile.tourism_score < self.config.min_tourism_score:
                state.rejected["city_tourism"] += 1
                continue
            visa = self._visa(profile, request)
            if (
                visa is None
                or visa.status != "allowed"
                or visa.fee_per_person is None
                or visa.fee_per_person.currency != request.currency
                or visa.verified_at > now.date()
                or visa.valid_until < max(now.date(), request.date_from)
            ):
                state.rejected["city_visa"] += 1
                continue
            hubs.append(profile)
        hubs.sort(key=lambda profile: (-profile.tourism_score, profile.city_code))
        if len(hubs) > self.config.max_hubs:
            state.partial("hub_limit")
        return hubs[: self.config.max_hubs]

    @staticmethod
    def _visa(profile: CityProfile, request: RouteRequest) -> VisaPolicy | None:
        return next(
            (visa for visa in profile.visas if visa.citizenship == request.citizenship),
            None,
        )

    def _queries(
        self, request: RouteRequest, hub: CityProfile
    ) -> tuple[FlightQuery, ...]:
        first = FlightQuery(
            origin=request.origin,
            destination=hub.city_code,
            departure_from=request.date_from,
            departure_to=request.date_to,
            passengers=request.passengers,
            currency=request.currency,
            limit=self.config.max_offers_per_query,
            max_stopover_hours=self.config.max_stopover_hours,
        )
        # Local calendar dates can shift across the date line. Actual endpoints are
        # checked below, so do not prematurely exclude valid onward departures.
        onward = FlightQuery(
            origin=hub.city_code,
            destination=request.destination,
            departure_from=request.date_from - timedelta(days=2),
            departure_to=request.date_to + timedelta(days=2),
            passengers=request.passengers,
            currency=request.currency,
            limit=self.config.max_offers_per_query,
            max_stopover_hours=self.config.max_stopover_hours,
        )
        queries: tuple[FlightQuery, ...] = (
            (first, onward) if self.config.allow_self_transfer else ()
        )
        if self.config.search_through_tickets:
            queries += (
                FlightQuery(
                    origin=request.origin,
                    destination=request.destination,
                    departure_from=request.date_from,
                    departure_to=request.date_to,
                    stopover_city=hub.city_code,
                    max_stopover_hours=self.config.max_stopover_hours,
                    passengers=request.passengers,
                    currency=request.currency,
                    limit=self.config.max_offers_per_query,
                ),
            )
        return queries

    @staticmethod
    def _valid_offer(
        offer: TicketOffer,
        query: FlightQuery,
        now: datetime,
        request: RouteRequest,
        state: _SearchState,
    ) -> bool:
        first, last = offer.segments[0], offer.segments[-1]
        departure_date = first.departure_at.astimezone(
            ZoneInfo(first.origin.timezone)
        ).date()
        expected_segments = 2 if query.stopover_city else 1
        if (
            len(offer.segments) != expected_segments
            or first.origin.city_code != query.origin
            or last.destination.city_code != query.destination
            or (
                query.stopover_city
                and first.destination.city_code != query.stopover_city
            )
            or not query.departure_from <= departure_date <= query.departure_to
            or offer.passengers != query.passengers
            or offer.total_price.currency != query.currency
        ):
            state.rejected["offer_contract"] += 1
            state.partial("provider_contract_mismatch")
            return False
        if (
            _utc(offer.expires_at) <= now
            or _utc(offer.fetched_at) > now
            or _utc(first.departure_at) <= now
        ):
            state.rejected["offer_expired_or_past"] += 1
            return False
        if (
            request.checked_baggage_required
            and offer.checked_baggage_included is not True
        ):
            state.rejected["offer_baggage"] += 1
            return False
        return True

    def _combinations(
        self,
        request: RouteRequest,
        hub: CityProfile,
        offers: dict[FlightQuery, tuple[TicketOffer, ...]],
    ) -> Iterator[tuple[TicketOffer, ...]]:
        queries = self._queries(request, hub)
        through = next((query for query in queries if query.stopover_city), None)
        through_offers = iter(offers[through] if through else ())

        def split_offers() -> Iterator[tuple[TicketOffer, ...]]:
            if not self.config.allow_self_transfer:
                return
            inbound, outbound = (query for query in queries if not query.stopover_city)
            onward = sorted(
                offers[outbound], key=lambda offer: _utc(offer.segments[0].departure_at)
            )
            departures = [_utc(offer.segments[0].departure_at) for offer in onward]
            for first in offers[inbound]:
                arrival = _utc(first.segments[-1].arrival_at)
                start = bisect_left(
                    departures,
                    arrival + timedelta(hours=self.config.min_stopover_hours),
                )
                end = bisect_right(
                    departures,
                    arrival + timedelta(hours=self.config.max_stopover_hours),
                )
                for second in onward[start:end]:
                    yield first, second

        split = split_offers()
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

    def _candidate(
        self,
        tickets: tuple[TicketOffer, ...],
        hub: CityProfile,
        request: RouteRequest,
        now: datetime,
        state: _SearchState,
    ) -> RouteCandidate | None:
        def reject(reason: str) -> None:
            state.rejected[reason] += 1

        first, second = tuple(
            segment for ticket in tickets for segment in ticket.segments
        )
        departure = _utc(first.departure_at)
        arrival = _utc(second.arrival_at)
        local_departure = departure.astimezone(ZoneInfo(first.origin.timezone)).date()
        local_arrival = arrival.astimezone(ZoneInfo(second.destination.timezone)).date()
        if not (
            request.date_from <= local_departure <= request.date_to
            and request.date_from <= local_arrival <= request.date_to
        ):
            reject("trip_dates")
            return None
        stop_start, stop_end = _utc(first.arrival_at), _utc(second.departure_at)
        stop_hours = (stop_end - stop_start).total_seconds() / 3600
        if (
            not self.config.min_stopover_hours
            <= stop_hours
            <= self.config.max_stopover_hours
        ):
            reject("stopover_duration")
            return None
        self_transfer = len(tickets) > 1 or not tickets[0].protected_connection
        airport_change = first.destination.code != second.origin.code
        if (self_transfer and not self.config.allow_self_transfer) or (
            airport_change and not self.config.allow_airport_change
        ):
            reject("transfer_policy")
            return None
        visa = self._visa(hub, request)
        if visa is None or visa.fee_per_person is None:
            reject("visa_missing")
            return None
        exit_date = stop_end.astimezone(ZoneInfo(hub.timezone)).date()
        if (
            visa.valid_until < exit_date
            or stop_hours > visa.max_stay_hours
            or departure < now + timedelta(days=visa.processing_days)
        ):
            reject("visa_timing")
            return None
        visit_start = stop_start + timedelta(hours=self.config.arrival_buffer_hours)
        visit_end = stop_end - timedelta(
            hours=self.config.departure_buffer_hours
            + self.config.self_transfer_buffer_hours * self_transfer
            + self.config.airport_change_buffer_hours * airport_change
        )
        available_hours = max(0, (visit_end - visit_start).total_seconds() / 3600)
        daytime_hours = self.daytime.hours(
            visit_start, visit_end, hub.timezone, self.config
        )
        if not 0 <= daytime_hours <= available_hours:
            raise ValueError("Daytime calculator returned an invalid duration")
        fraction = daytime_hours / available_hours if available_hours else 0
        if (
            daytime_hours < max(self.config.min_daytime_hours, hub.minimum_visit_hours)
            or fraction <= self.config.min_daytime_fraction
        ):
            reject("insufficient_daytime")
            return None
        ticket_cost = sum((ticket.total_price.amount for ticket in tickets), Decimal(0))
        visa_cost = visa.fee_per_person.amount * request.passengers
        total = ticket_cost + visa_cost
        if request.budget is not None and total > request.budget:
            reject("budget")
            return None
        warnings = [
            "hotel_and_ground_transport_not_included",
            "recheck_fares_and_entry",
        ]
        if self_transfer:
            warnings.append("unprotected_connection")
        if airport_change:
            warnings.append("airport_change")
        if any(ticket.checked_baggage_included is None for ticket in tickets):
            warnings.append("checked_baggage_unknown")
        elif not all(ticket.checked_baggage_included for ticket in tickets):
            warnings.append("checked_baggage_not_included")
        identity = json.dumps(
            [(ticket.provider, ticket.offer_id) for ticket in tickets],
            separators=(",", ":"),
        )
        return RouteCandidate(
            id=hashlib.sha256(identity.encode()).hexdigest()[:24],
            tickets=tickets,
            stopover=hub,
            visa=visa,
            ticket_cost=Money(amount=ticket_cost, currency=request.currency),
            visa_cost=Money(amount=visa_cost, currency=request.currency),
            total_cost=Money(amount=total, currency=request.currency),
            metrics=VisitMetrics(
                stopover_hours=stop_hours,
                available_hours=available_hours,
                daytime_hours=daytime_hours,
                nighttime_hours=available_hours - daytime_hours,
                daytime_fraction=fraction,
                total_trip_hours=(arrival - departure).total_seconds() / 3600,
                self_transfer=self_transfer,
                airport_change=airport_change,
            ),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _signature(route: RouteCandidate) -> str:
        return json.dumps(
            [
                [
                    segment.origin.code,
                    segment.destination.code,
                    segment.operating_carrier,
                    segment.flight_number,
                    _utc(segment.departure_at).isoformat(),
                    _utc(segment.arrival_at).isoformat(),
                ]
                for ticket in route.tickets
                for segment in ticket.segments
            ]
            + [
                [
                    route.metrics.self_transfer,
                    [ticket.cabin for ticket in route.tickets],
                    [ticket.checked_baggage_included for ticket in route.tickets],
                ]
            ]
        )
