"""Форматирование и вывод предложений авиаперелётов в консоль."""

import json
from collections.abc import Iterator
from typing import Any

_SEPARATOR = "\u2500" * 78


def _time(value: str | None) -> str:
    """Обрезает '05:25:00' до '05:25'."""
    return value[:5] if value and len(value) >= 5 else value or "??:??"


def _money(amount: Any, currency: str = "RUB") -> str:
    """47 285 -> '47 285 руб.'"""
    try:
        return f"{int(amount):,}".replace(",", " ") + " руб."
    except TypeError, ValueError:
        return f"{amount} {currency}"


def _city(segment: dict[str, Any], side: str) -> str:
    """Город + код аэропорта: 'Екатеринбург (SVX)'."""
    city = segment.get(f"{side}_city_name")
    airport = segment.get(f"{side}_airport")
    parts = [p for p in (city, f"({airport})" if airport else None) if p]
    return " ".join(parts) or "-"


def _airline_name(segment: dict[str, Any], group: dict[str, Any]) -> str:
    fi = (segment.get("flights_info") or [{}])[0]
    return (
        fi.get("marketing_airline_name")
        or group.get("carrier_name")
        or group.get("carrier_code")
        or "-"
    )


def _iter_rows(
    data: dict[str, Any],
) -> Iterator[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Итерация по (dir_number, segment, airline_group, offer)."""
    for group in data.get("offers", []):
        if not isinstance(group, dict):
            continue
        for offer in group.get("offers") or []:
            if not isinstance(offer, dict):
                continue
            for segment in offer.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                yield segment.get("dir_number") or 1, segment, group, offer


def _format_raw(value: Any) -> str:
    """Сериализация в читаемый JSON для raw-вывода."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _print_raw_block(title: str, value: Any) -> None:
    print(f"\n  === {title} (RAW) ===")
    for line in _format_raw(value).splitlines():
        print(f"    {line}")


def _print_header(data: dict[str, Any], rendered: int) -> None:
    currency = data.get("currency", "RUB")
    total = sum(
        1
        for group in data.get("offers", [])
        if isinstance(group, dict)
        for offer in group.get("offers") or []
        if isinstance(offer, dict)
        for _ in offer.get("segments") or []
    )
    print(_SEPARATOR)
    print(
        f"Найдено предложений: {total} из них отображено {rendered}, валюта: {currency}"
    )
    print(_SEPARATOR)


def _print_airline_header(group: dict[str, Any]) -> None:
    name = group.get("carrier_name") or group.get("carrier_code") or "-"
    code = group.get("carrier_code") or ""
    print(f"\nАвиакомпания: {name} {f'({code})' if code else ''}".rstrip())


def _print_dir_header(previous_dir: int | None, dir_number: int) -> None:
    if previous_dir is not None and dir_number != previous_dir:
        print("\n  Обратный перелёт:")


def print_offers(data: Any, show_raw: bool = True) -> None:
    """Печатает предложения таблицей, а нераспознанные части — в raw-виде."""
    if not data:
        print("[ ] Нет данных для отображения.")
        return

    if not isinstance(data, dict):
        _print_raw_block("Ответ целиком (неожиданная структура)", data)
        return

    offers = data.get("offers")
    if offers is not None and not isinstance(offers, list):
        _print_raw_block("Поле 'offers' (неожиданный формат)", offers)
        return

    handled_groups: set[int] = set()
    handled_offers: set[int] = set()
    rendered = 0

    prev_dir: int | None = None
    current_group_id: object = None

    for dir_number, segment, group, offer in _iter_rows(data):
        handled_groups.add(id(group))
        handled_offers.add(id(offer))
        rendered += 1

        if current_group_id != id(group):
            _print_airline_header(group)
            current_group_id = id(group)

        _print_dir_header(prev_dir, dir_number)

        date = segment.get("departure_date") or "-"
        dep_city = _city(segment, "departure")
        arr_city = _city(segment, "arrival")
        dep_time = _time(segment.get("departure_time"))
        arr_time = _time(segment.get("arrival_time"))
        price = offer.get("min_price") or offer.get("price") or segment.get("price")
        airline = _airline_name(segment, group)

        print(
            f"  {date}  {dep_time:>5}  {dep_city:<22} -> {arr_city:<22}  {arr_time:>5}"
            f"  {_money(price, data.get('currency', 'RUB')):<14} {airline}"
        )

        prev_dir = dir_number

    _print_header(data, rendered)
    print(_SEPARATOR)

    if not show_raw or not data:
        return

    raw_printed = False

    for group in data.get("offers", []):
        if not isinstance(group, dict):
            _print_raw_block("Группа авиакомпании (неожиданный формат)", group)
            raw_printed = True
            continue
        if id(group) not in handled_groups:
            carrier = group.get("carrier_code") or group.get("carrier_name") or "?"
            _print_raw_block(
                f"Авиакомпания {carrier} — нет распознанных сегментов",
                group,
            )
            raw_printed = True
            continue
        for offer in group.get("offers") or []:
            if not isinstance(offer, dict) or id(offer) in handled_offers:
                continue
            _print_raw_block(
                f"Вариант {group.get('carrier_code') or '?'}"
                " — нет распознанных сегментов",
                offer,
            )
            raw_printed = True

    ow = data.get("ow_offers")
    if ow not in (None, [], {}):
        _print_raw_block("ow_offers (односторонние предложения)", ow)
        raw_printed = True

    if raw_printed:
        print()
        print(_SEPARATOR)


def print_offers_raw(data: Any) -> None:
    """Печатает весь ответ скрапера в raw-виде (JSON)."""
    if not data:
        print("[ ] Нет данных для отображения.")
        return
    print(_format_raw(data))
