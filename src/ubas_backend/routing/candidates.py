"""Pure route construction and explicit rejection reasons."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.policy import visa_for
from ubas_backend.routing.ports import DaytimeCalculator
from ubas_backend.routing.schemas import (
    CityProfile,
    FlightSegment,
    Money,
    RouteCandidate,
    RouteRequest,
    TicketOffer,
    VisitMetrics,
)


class CandidateRejection(StrEnum):
    DATES = "trip_dates"
    DURATION = "stopover_duration"
    TRANSFER = "transfer_policy"
    VISA_MISSING = "visa_missing"
    VISA_TIMING = "visa_timing"
    DAYTIME = "insufficient_daytime"
    BUDGET = "budget"


def _within_dates(
    segment: FlightSegment, request: RouteRequest, *, arrival: bool
) -> bool:
    instant = segment.arrival_at if arrival else segment.departure_at
    airport = segment.destination if arrival else segment.origin
    local_date = instant.astimezone(ZoneInfo(airport.timezone)).date()
    return request.date_from <= local_date <= request.date_to


def _warnings(
    tickets: tuple[TicketOffer, ...], metrics: VisitMetrics
) -> tuple[str, ...]:
    warnings = [
        "hotel_and_ground_transport_not_included",
        "recheck_fares_and_entry",
    ]
    if metrics.self_transfer:
        warnings.append("unprotected_connection")
    if metrics.airport_change:
        warnings.append("airport_change")
    if any(ticket.checked_baggage_included is None for ticket in tickets):
        warnings.append("checked_baggage_unknown")
    elif not all(ticket.checked_baggage_included for ticket in tickets):
        warnings.append("checked_baggage_not_included")
    return tuple(warnings)


class CandidateBuilder:
    def __init__(self, config: PlannerConfig, daytime: DaytimeCalculator) -> None:
        self.config = config
        self.daytime = daytime

    def build(
        self,
        tickets: tuple[TicketOffer, ...],
        hub: CityProfile,
        request: RouteRequest,
        now: datetime,
    ) -> RouteCandidate | CandidateRejection:
        first, second = tuple(
            segment for ticket in tickets for segment in ticket.segments
        )
        if not _within_dates(first, request, arrival=False) or not _within_dates(
            second, request, arrival=True
        ):
            return CandidateRejection.DATES
        stop_hours = (
            second.departure_at.astimezone(UTC) - first.arrival_at.astimezone(UTC)
        ).total_seconds() / 3600
        if (
            not self.config.min_stopover_hours
            <= stop_hours
            <= self.config.max_stopover_hours
        ):
            return CandidateRejection.DURATION
        metrics = self._metrics(first, second, tickets, hub)
        rejection = self._visit_rejection(metrics, hub)
        if rejection is not None:
            return rejection
        visa = visa_for(hub, request)
        if visa is None or visa.fee_per_person is None:
            return CandidateRejection.VISA_MISSING
        exit_date = second.departure_at.astimezone(ZoneInfo(hub.timezone)).date()
        if (
            visa.valid_until < exit_date
            or metrics.stopover_hours > visa.max_stay_hours
            or first.departure_at.astimezone(UTC)
            < now + timedelta(days=visa.processing_days)
        ):
            return CandidateRejection.VISA_TIMING
        ticket_cost = sum((ticket.total_price.amount for ticket in tickets), Decimal(0))
        visa_cost = visa.fee_per_person.amount * request.passengers
        total = ticket_cost + visa_cost
        if request.budget is not None and total > request.budget:
            return CandidateRejection.BUDGET
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
            metrics=metrics,
            warnings=_warnings(tickets, metrics),
        )

    def _visit_rejection(
        self, metrics: VisitMetrics, hub: CityProfile
    ) -> CandidateRejection | None:
        if metrics.self_transfer and not self.config.allow_self_transfer:
            return CandidateRejection.TRANSFER
        if metrics.airport_change and not self.config.allow_airport_change:
            return CandidateRejection.TRANSFER
        if (
            metrics.daytime_hours
            < max(self.config.min_daytime_hours, hub.minimum_visit_hours)
            or metrics.daytime_fraction <= self.config.min_daytime_fraction
        ):
            return CandidateRejection.DAYTIME
        return None

    def _metrics(
        self,
        first: FlightSegment,
        second: FlightSegment,
        tickets: tuple[TicketOffer, ...],
        hub: CityProfile,
    ) -> VisitMetrics:
        stop_start = first.arrival_at.astimezone(UTC)
        stop_end = second.departure_at.astimezone(UTC)
        self_transfer = len(tickets) > 1 or not tickets[0].protected_connection
        airport_change = first.destination.code != second.origin.code
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
        return VisitMetrics(
            stopover_hours=(stop_end - stop_start).total_seconds() / 3600,
            available_hours=available_hours,
            daytime_hours=daytime_hours,
            nighttime_hours=available_hours - daytime_hours,
            daytime_fraction=daytime_hours / available_hours if available_hours else 0,
            total_trip_hours=(
                second.arrival_at.astimezone(UTC) - first.departure_at.astimezone(UTC)
            ).total_seconds()
            / 3600,
            self_transfer=self_transfer,
            airport_change=airport_change,
        )
