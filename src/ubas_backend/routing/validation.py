"""Pure offer checks; tourism and costs belong to complete itineraries."""

from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from ubas_backend.routing.schemas import FlightQuery, RouteRequest, TicketOffer


class OfferRejection(StrEnum):
    CONTRACT = "offer_contract"
    PAST = "offer_past"


def matches_query(offer: TicketOffer, query: FlightQuery) -> bool:
    first, last = offer.segments[0], offer.segments[-1]
    departure = first.departure_at.astimezone(ZoneInfo(first.origin.timezone)).date()
    return (
        first.origin.city_code == query.origin
        and last.destination.city_code == query.destination
        and len(offer.segments) <= query.max_segments
        and departure == query.departure_date
        and offer.passengers == query.passengers
        and offer.cabin == "economy"
    )


def offer_rejection(
    offer: TicketOffer,
    query: FlightQuery,
    request: RouteRequest,
    now: datetime,
) -> OfferRejection | None:
    if not matches_query(offer, query) or offer.fetched_at.astimezone(UTC) > now:
        return OfferRejection.CONTRACT
    if offer.segments[0].departure_at.astimezone(UTC) <= now:
        return OfferRejection.PAST
    # OPEN-07: no agreed cache-age rule; retain source timestamps, no revalidation.
    return None
