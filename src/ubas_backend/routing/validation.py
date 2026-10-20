"""Pure ticket checks; no logging, database access or diagnostics mutation."""

from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from ubas_backend.routing.schemas import FlightQuery, RouteRequest, TicketOffer


class OfferRejection(StrEnum):
    CONTRACT = "offer_contract"
    EXPIRED_OR_PAST = "offer_expired_or_past"
    BAGGAGE = "offer_baggage"


def matches_query(offer: TicketOffer, query: FlightQuery) -> bool:
    first = offer.segments[0]
    expected_cities = (
        (query.origin, query.stopover_city, query.destination)
        if query.stopover_city is not None
        else (query.origin, query.destination)
    )
    actual_cities = (
        first.origin.city_code,
        *(segment.destination.city_code for segment in offer.segments),
    )
    departure_date = first.departure_at.astimezone(
        ZoneInfo(first.origin.timezone)
    ).date()
    return (
        actual_cities == expected_cities
        and query.departure_from <= departure_date <= query.departure_to
        and offer.passengers == query.passengers
        and offer.total_price.currency == query.currency
    )


def offer_rejection(
    offer: TicketOffer,
    query: FlightQuery,
    request: RouteRequest,
    now: datetime,
) -> OfferRejection | None:
    if not matches_query(offer, query):
        return OfferRejection.CONTRACT
    if (
        offer.expires_at.astimezone(UTC) <= now
        or offer.fetched_at.astimezone(UTC) > now
        or offer.segments[0].departure_at.astimezone(UTC) <= now
    ):
        return OfferRejection.EXPIRED_OR_PAST
    if request.checked_baggage_required and offer.checked_baggage_included is not True:
        return OfferRejection.BAGGAGE
    return None
