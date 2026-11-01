"""Чистый маппинг ответов agent.ru в доменные контракты backend.

На входе — разобранный JSON, перехваченный скрапером (или полученный из
кэша); на выходе — валидированные модели `routing.schemas`. Модуль не
выполняет I/O, поэтому легко покрывается регрессионными тестами.

Контракт `TicketOffer` требует-aware datetime, город IATA, цену в валюте
запроса. Чего agent.ru не гарантирует — выбрасывается с типизированной
причиной; частично распознанный ответ отдаётся с предупреждениями.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ubas_backend.agent_scraper.airports import airport_for_code
from ubas_backend.routing.schemas import (
    Airport,
    FlightSegment,
    Money,
    TicketOffer,
)

PROVIDER_NAME = "agent-ru"

# agent.ru отдаёт время строками; TTL цены неизвестен, берём консервативно.
_OFFER_TTL = timedelta(minutes=30)

_CABIN_BY_CODE: dict[str, str] = {
    "E": "economy",
    "W": "premium_economy",
    "B": "business",
    "F": "first",
}


class MappingFailure(Exception):
    """Типизированная причина, по которой ответ не стал предложением."""


def _airport(code: Any) -> Airport | None:
    if not isinstance(code, str):
        return None
    return airport_for_code(code.strip().upper())


def _aware_datetime(day: Any, clock: Any, airport: Airport | None) -> datetime | None:
    """Склеивает '2026-11-05' и '14:30:00' в aware datetime по зоне аэропорта."""
    if not isinstance(day, str) or not isinstance(clock, str):
        return None
    try:
        naive = datetime.combine(
            date.fromisoformat(day[: len("2026-11-05")]), time.fromisoformat(clock)
        )
    except ValueError:
        return None
    zone = airport.timezone if airport else "UTC"
    try:
        return naive.replace(tzinfo=ZoneInfo(zone))
    except ZoneInfoNotFoundError, ValueError:
        return None


def _money(amount: Any, currency: str) -> Money | None:
    try:
        value = Decimal(str(amount))
    except InvalidOperation, ValueError:
        return None
    if not value.is_finite() or value < 0:
        return None
    return Money(amount=value, currency=currency)


def _carrier(offer: dict[str, Any], fallback: str) -> str:
    for source in (offer.get("marketing_airline_code"), fallback):
        if isinstance(source, str) and len(source) >= 2:
            return source[:3].upper()
    return "XX"


def _segments(offer: dict[str, Any], fallback_carrier: str) -> Iterator[FlightSegment]:
    for segment in offer.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        origin = _airport(segment.get("departure_airport"))
        destination = _airport(segment.get("arrival_airport"))
        if origin is None or destination is None:
            raise MappingFailure("unknown_airport")
        departure = _aware_datetime(
            segment.get("departure_date"), segment.get("departure_time"), origin
        )
        arrival = _aware_datetime(
            segment.get("arrival_date") or segment.get("departure_date"),
            segment.get("arrival_time"),
            destination,
        )
        if departure is None or arrival is None:
            raise MappingFailure("missing_schedule")
        yield FlightSegment(
            origin=origin,
            destination=destination,
            departure_at=departure,
            arrival_at=arrival,
            marketing_carrier=_carrier(segment, fallback_carrier),
            operating_carrier=_carrier(segment, fallback_carrier),
            flight_number=str(
                segment.get("flight_number") or segment.get("flightNumber") or "0"
            ),
        )


def map_group(
    group: dict[str, Any], *, now: datetime, passengers: int, currency: str
) -> Iterator[TicketOffer]:
    """Преобразует одну группу авиакомпании из ответа agent.ru в офферы."""
    carrier = str(group.get("carrier_code") or group.get("airCompany") or "XX")
    for index, offer in enumerate(group.get("offers") or []):
        if not isinstance(offer, dict):
            continue
        try:
            segments = tuple(_segments(offer, carrier))
        except MappingFailure:
            continue
        if not segments:
            continue
        price = next(
            (
                money
                for key in ("min_price", "price")
                if (money := _money(offer.get(key), currency)) is not None
            ),
            None,
        )
        if price is None:
            continue
        yield TicketOffer(
            provider=PROVIDER_NAME,
            offer_id=f"{carrier}:{index}:{len(segments)}",
            segments=segments,
            total_price=price,
            passengers=passengers,
            checked_baggage_included=None,
            fetched_at=now,
            expires_at=now + _OFFER_TTL,
        )


def map_response(
    data: dict[str, Any], *, now: datetime, passengers: int
) -> tuple[TicketOffer, ...]:
    """Преобразует перехваченный ответ `api/air/offers` в кортеж офферов.

    agent.ru возвращает группы авиакомпаний в `offers` и односторонние
    предложения в `ow_offers`; обе секции маппятся в плоский список.
    """
    currency = str(data.get("currency") or "RUB").upper()
    now = now.astimezone(UTC)
    results: list[TicketOffer] = []
    groups: list[Any] = list(data.get("offers") or [])
    ow = data.get("ow_offers")
    if isinstance(ow, list):
        groups.extend(ow)
    for group in groups:
        if isinstance(group, dict):
            results.extend(
                map_group(
                    group,
                    now=now,
                    passengers=passengers,
                    currency=currency,
                )
            )
    return tuple(results)


def cabin_from_code(code: str | None) -> str:
    """Класс обслуживания agent.ru ('E'/'W'/'B'/'F') в контракт backend."""
    return _CABIN_BY_CODE.get((code or "E").upper(), "economy")
