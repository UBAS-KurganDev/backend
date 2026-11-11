"""Pure itinerary validation and cost/card construction."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.costs import CostCalculator
from ubas_backend.routing.ports import VisaCostResolver
from ubas_backend.routing.schemas import (
    CityProfile,
    CostComponent,
    FlightSegment,
    Money,
    RouteCandidate,
    RouteRequest,
    Stopover,
    TicketOffer,
)


class CandidateRejection(StrEnum):
    STRUCTURE = "route_structure"
    DATES = "trip_dates"
    CHRONOLOGY = "connection_chronology"
    AIRPORT_CHANGE_TOO_SHORT = "airport_change_too_short"
    TOURISM = "stopover_tourism"
    PAST = "departure_past"


def local_date(segment: FlightSegment, *, arrival: bool = False) -> datetime:
    airport = segment.destination if arrival else segment.origin
    instant = segment.arrival_at if arrival else segment.departure_at
    return instant.astimezone(ZoneInfo(airport.timezone))


class CandidateBuilder:
    def __init__(
        self,
        config: PlannerConfig,
        profiles: dict[str, CityProfile],
        costs: CostCalculator,
        visas: VisaCostResolver,
    ) -> None:
        self.config = config
        self.profiles = profiles
        self.costs = costs
        self.visas = visas

    def stopovers(
        self,
        tickets: tuple[TicketOffer, ...],
    ) -> tuple[Stopover, ...] | CandidateRejection:
        indexed = [
            (i, segment)
            for i, ticket in enumerate(tickets)
            for segment in ticket.segments
        ]
        result = []
        for (left_ticket, first), (right_ticket, second) in zip(
            indexed, indexed[1:], strict=False
        ):
            if first.destination.city_code != second.origin.city_code:
                return CandidateRejection.STRUCTURE
            seconds = (
                second.departure_at.astimezone(UTC) - first.arrival_at.astimezone(UTC)
            ).total_seconds()
            if seconds <= 0:
                return CandidateRejection.CHRONOLOGY
            airport_change = first.destination.code != second.origin.code
            if airport_change and seconds < 2 * 3600:
                return CandidateRejection.AIRPORT_CHANGE_TOO_SHORT
            profile = self.profiles.get(first.destination.city_code)
            score = profile.tourism_score if profile else 0
            if seconds >= 12 * 3600 and score < self.config.tourism_threshold:
                return CandidateRejection.TOURISM
            result.append(
                Stopover(
                    city_code=first.destination.city_code,
                    arrival_airport=first.destination,
                    departure_airport=second.origin,
                    arrival_at=first.arrival_at,
                    departure_at=second.departure_at,
                    duration_hours=seconds / 3600,
                    tourism_score=score,
                    airport_change=airport_change,
                    self_transfer=left_ticket != right_ticket,
                )
            )
        return tuple(result)

    def build(
        self,
        tickets: tuple[TicketOffer, ...],
        request: RouteRequest,
        now: datetime,
        occurrence: tuple[int, ...] = (),
    ) -> RouteCandidate | CandidateRejection:
        segments = tuple(segment for ticket in tickets for segment in ticket.segments)
        if not 1 <= len(segments) <= 2:
            return CandidateRejection.STRUCTURE
        first, last = segments[0], segments[-1]
        if (
            first.origin.city_code != request.origin
            or last.destination.city_code != request.destination
        ):
            return CandidateRejection.STRUCTURE
        if not (
            request.date_from <= local_date(first).date() <= request.date_to
            and request.date_from
            <= local_date(last, arrival=True).date()
            <= request.date_to
        ):
            return CandidateRejection.DATES
        if first.departure_at.astimezone(UTC) <= now:
            return CandidateRejection.PAST
        stops = self.stopovers(tickets)
        if isinstance(stops, CandidateRejection):
            return stops
        costs: list[CostComponent] = []
        for index, ticket in enumerate(tickets):
            costs.append(self.costs.component(f"ticket:{index}", ticket.total_price))
            costs.extend(
                self.costs.component(
                    f"ticket:{index}:{reason}", None, unknown_reason=reason
                )
                for reason in ticket.unknown_costs
            )
        expenses = self.visas.expenses(segments, self.profiles)
        if len({item.key for item in expenses}) != len(expenses):
            raise ValueError("Visa resolver returned duplicate expense keys")
        costs.extend(
            self.costs.component(
                item.key, item.amount, visa=True, unknown_reason=item.unknown_reason
            )
            for item in expenses
        )
        if len({item.key for item in costs}) != len(costs):
            raise ValueError("Expense keys must be unique across tickets and visas")
        ticket_cost = sum(
            (item.rub_amount or Decimal(0) for item in costs if item.kind == "ticket"),
            Decimal(0),
        )
        visa_cost = sum(
            (item.rub_amount or Decimal(0) for item in costs if item.kind == "visa"),
            Decimal(0),
        )
        unknown = tuple(item.key for item in costs if item.unknown_reason is not None)
        warnings = ["hotel_and_ground_transport_not_included"]
        if unknown:
            warnings.append("full_cost_unknown")
        warnings.extend(
            f"airport_change:{i}" for i, stop in enumerate(stops) if stop.airport_change
        )
        if any(stop.self_transfer for stop in stops):
            warnings.append("separate_tickets")
        if any(
            ticket.baggage_description is None
            and ticket.checked_baggage_included is None
            for ticket in tickets
        ):
            warnings.append("baggage_conditions_unknown")
        # Occurrence indexes preserve duplicates while keeping distinct card IDs.
        identity = "|".join(
            [*(ticket.model_dump_json() for ticket in tickets), str(occurrence)]
        )
        return RouteCandidate(
            id=hashlib.sha256(identity.encode()).hexdigest()[:24],
            kind="direct" if len(segments) == 1 else "stopover",
            tickets=tickets,
            stopovers=stops,
            costs=tuple(costs),
            full_cost_known=not unknown,
            unknown_costs=unknown,
            ticket_cost=Money(amount=ticket_cost),
            visa_cost=Money(amount=visa_cost),
            total_cost=Money(amount=ticket_cost + visa_cost),
            total_trip_hours=(
                last.arrival_at.astimezone(UTC) - first.departure_at.astimezone(UTC)
            ).total_seconds()
            / 3600,
            warnings=tuple(warnings),
        )
