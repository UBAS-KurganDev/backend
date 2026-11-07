"""Translate source purchase options, never carrier/group minimum prices.

Observed agent.ru structure:
offers/ow_offers -> carrier.offers -> group.segments (purchase alternatives)
-> flights_info (physical flights of that one alternative).
"""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ubas_backend.agent_scraper.airports import airport_for_code
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.routing.schemas import (
    Airport,
    FlightSearchResult,
    FlightSegment,
    Money,
    TicketOffer,
)


class MappingFailure(ValueError):
    pass


type AirportLookup = Callable[[str], Airport | None]


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MappingFailure("missing_field")
    return value.strip()


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MappingFailure("invalid_collection")
    return value


def _airport(value: Any, lookup: AirportLookup) -> Airport:
    airport = lookup(_text(value))
    if airport is None:
        raise MappingFailure("unknown_airport")
    return airport


def _schedule(flight: dict[str, Any], side: str, airport: Airport) -> datetime:
    day = date.fromisoformat(_text(flight.get(f"{side}_date")))
    clock = time.fromisoformat(_text(flight.get(f"{side}_local_time")))
    if clock.tzinfo is not None:
        raise MappingFailure("nonlocal_schedule")
    naive = datetime.combine(day, clock)
    zone = ZoneInfo(airport.timezone)
    value = naive.replace(tzinfo=zone)
    # Reject nonexistent/ambiguous local times instead of inventing a DST fold.
    if (
        value.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
        or value.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset()
    ):
        raise MappingFailure("ambiguous_schedule")
    return value


def _is_economy_ticket(flights: list[dict[str, Any]]) -> bool:
    cabins = {
        value.strip().casefold() if isinstance(value, str) else None
        for flight in flights
        for value in (flight.get("service_class"),)
    }
    # A mixed-cabin ticket is outside the economy scope as a whole.
    if cabins.intersection({"бизнес", "business"}):
        return False
    if not cabins.issubset({"эконом", "economy"}):
        raise MappingFailure("unsupported_cabin")
    return True


def _segment(flight: dict[str, Any], lookup: AirportLookup) -> FlightSegment:
    if flight.get("techstop") or flight.get("vehicle_type") != "aircraft":
        raise MappingFailure("unsupported_flight")
    origin = _airport(flight.get("departure_airport"), lookup)
    destination = _airport(flight.get("arrival_airport"), lookup)
    return FlightSegment(
        origin=origin,
        destination=destination,
        departure_at=_schedule(flight, "departure", origin),
        arrival_at=_schedule(flight, "arrival", destination),
        marketing_carrier=_text(flight.get("marketing_airline_code")),
        operating_carrier=_text(flight.get("operating_airline_code")),
        flight_number=_text(flight.get("flight_number")),
    )


def _price(value: Any, currency: str) -> Money | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise MappingFailure("invalid_price") from error
    return Money(amount=amount, currency=currency)


def _baggage(flights: list[dict[str, Any]]) -> tuple[bool | None, str]:
    states: list[bool | None] = []
    descriptions: list[str] = []
    for flight in flights:
        raw = flight.get("baggage")
        text = raw.strip() if isinstance(raw, str) else ""
        descriptions.append(text or "unknown")
        normalized = text.upper().replace(" ", "")
        if normalized in ("NO", "0PC", "0KG"):
            states.append(False)
        elif re.fullmatch(r"[1-9][0-9]*(?:PC|KG)", normalized):
            states.append(True)
        else:
            states.append(None)
    included = False if False in states else (True if all(states) else None)
    return included, "; ".join(descriptions)


def _ticket(
    raw: dict[str, Any],
    request: SearchRequest,
    captured: CapturedOffers,
    currency: str,
    occurrence: str,
    lookup: AirportLookup,
    max_segments: int,
) -> TicketOffer | None:
    if raw.get("dir_number") != 1:
        raise MappingFailure("unsupported_direction")
    identifier = _text(raw.get("buy_id"))
    flights = _objects(raw.get("flights_info"))
    if not flights:
        raise MappingFailure("missing_flights")
    if len(flights) > max_segments:
        return None  # Outside the supported MVP, not a malformed supported ticket.
    # agent.ru labels these bus alternatives as aircraft. Never invent an airport.
    if any(
        flight.get(side) == "СТЗ"
        for flight in flights
        for side in ("departure_airport", "arrival_airport")
    ):
        return None
    if not _is_economy_ticket(flights):
        return None
    details = _objects(raw.get("price_details"))
    if (
        len(details) != 1
        or details[0].get("passenger_type") != "adult"
        or details[0].get("qty") != 1
    ):
        raise MappingFailure("passengers_mismatch")
    segments = tuple(_segment(flight, lookup) for flight in flights)
    expected_origin = _airport(request.origin, lookup)
    expected_destination = _airport(request.destination, lookup)
    if (
        segments[0].origin.city_code != expected_origin.city_code
        or segments[-1].destination.city_code != expected_destination.city_code
        or segments[0].departure_at.date() != request.depart_date
    ):
        raise MappingFailure("query_mismatch")
    # Source expands airport searches to the whole city. Partition by requested
    # airports: the provider queries each airport pair, preserving genuine duplicates.
    if (
        segments[0].origin.code != request.origin
        or segments[-1].destination.code != request.destination
    ):
        return None
    price = _price(raw.get("price"), currency)
    checked, description = _baggage(flights)
    # Hash source IDs: do not expose anonymous search/session tokens to the client.
    offer_id = hashlib.sha256(f"{identifier}:{occurrence}".encode()).hexdigest()
    return TicketOffer(
        provider="agent-ru",
        offer_id=offer_id,
        segments=segments,
        total_price=price,
        unknown_costs=("ticket_price",) if price is None else (),
        passengers=1,
        checked_baggage_included=checked,
        baggage_description=description,
        # A buy_id does not prove a carrier guarantee for a connection.
        protected_connection=False,
        fetched_at=captured.fetched_at,
    )


def map_response(
    captured: CapturedOffers,
    request: SearchRequest,
    *,
    airport_lookup: AirportLookup = airport_for_code,
    max_segments: int = 4,
) -> FlightSearchResult:
    data = captured.data
    if data.get("status") != "Ready":
        return FlightSearchResult(complete=False, warnings=("agent_not_ready",))
    offers: list[TicketOffer] = []
    warnings: set[str] = set()
    try:
        currency = _text(data.get("currency"))
        for section in ("offers", "ow_offers"):
            for group_index, group in enumerate(_objects(data.get(section))):
                for index, alternatives in enumerate(_objects(group.get("offers"))):
                    for number, raw in enumerate(
                        _objects(alternatives.get("segments"))
                    ):
                        try:
                            ticket = _ticket(
                                raw,
                                request,
                                captured,
                                currency,
                                f"{section}:{group_index}:{index}:{number}",
                                airport_lookup,
                                max_segments,
                            )
                            if ticket is not None:
                                offers.append(ticket)
                        except MappingFailure as error:
                            warnings.add(f"agent_mapping:{error}")
                        except ValidationError, ValueError:
                            warnings.add("agent_mapping:invalid_ticket")
    except MappingFailure as error:
        warnings.add(f"agent_mapping:{error}")
    return FlightSearchResult(
        offers=tuple(offers), complete=not warnings, warnings=tuple(sorted(warnings))
    )
