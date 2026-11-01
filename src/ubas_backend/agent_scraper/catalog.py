"""Засев SQLite-каталога UBAS данными под скрапер agent.ru.

Каталог (`UBAS_CATALOG_PATH`) — обязательная зависимость worker: из него
планировщик берёт топологию связей и профили городов с визовыми правилами.
Скрипт заполняет каталог направлениями, которые реально ищет скрапер, и
справочными DEMO-визами: они намеренно помечены как неактуальные советы,
перед продакшеном их нужно подтвердить источниками (инвариант AGENTS.md).

Запуск: uv run --env-file .env python -m ubas_backend.agent_scraper.catalog
"""

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ubas_backend.routing.schemas import CityProfile, Money, VisaPolicy
from ubas_backend.routing.storage import CatalogData, FlightLink, SqliteCatalog

# Направления, которые отдаёт скрапер; наличие связи ≠ наличие рейса на дату.
# Исходящие связи хабов (MOW, IST) нужны планировщику, чтобы находить
# города-транзита: connecting_cities пересекает исходящие A и входящие C.
_LINKS: tuple[tuple[str, str], ...] = (
    ("SVX", "MOW"),
    ("SVX", "IST"),
    ("SVX", "AYT"),
    ("SVX", "EVN"),
    ("SVX", "ALA"),
    ("SVX", "TAS"),
    ("MOW", "AYT"),
    ("MOW", "IST"),
    ("MOW", "EVN"),
    ("MOW", "TBS"),
    ("MOW", "ALA"),
    ("MOW", "TAS"),
    ("MOW", "HRG"),
    ("MOW", "BKK"),
    ("IST", "AYT"),
    ("ALA", "IST"),
    ("EVN", "IST"),
    ("TAS", "IST"),
)

_CITIES: tuple[tuple[str, str, str, str, str], ...] = (
    # код, имя, страна, таймзона, туристическая оценка
    ("AYT", "Анталья", "TR", "Europe/Istanbul", "0.90"),
    ("IST", "Стамбул", "TR", "Europe/Istanbul", "0.95"),
    ("EVN", "Ереван", "AM", "Asia/Yerevan", "0.75"),
    ("TBS", "Тбилиси", "GE", "Asia/Tbilisi", "0.80"),
    ("ALA", "Алматы", "KZ", "Asia/Almaty", "0.70"),
    ("TAS", "Ташкент", "UZ", "Asia/Tashkent", "0.70"),
    ("HRG", "Хургада", "EG", "Africa/Cairo", "0.65"),
    ("BKK", "Бангкок", "TH", "Asia/Bangkok", "0.90"),
)

_VISA_VALID_DAYS = 90
_DEMO_SOURCE = "DEMO seed data for agent-scraper; verify before production use"


def _visa(today: date, valid_until: date) -> VisaPolicy:
    return VisaPolicy(
        citizenship="RU",
        status="allowed",
        kind="visa_free",
        fee_per_person=Money(amount=Decimal(0)),
        ease=1.0,
        processing_days=0,
        max_stay_hours=72,
        verified_at=today,
        valid_until=valid_until,
        source=_DEMO_SOURCE,
    )


def _cities() -> tuple[CityProfile, ...]:
    today = datetime.now(UTC).date()
    valid_until = today + timedelta(days=_VISA_VALID_DAYS)
    return tuple(
        CityProfile(
            city_code=code,
            name=name,
            country_code=country,
            timezone=timezone,
            tourism_score=float(tourism),
            hotel_per_night=Money(amount=Decimal("6000")),
            visas=(_visa(today, valid_until),),
        )
        for code, name, country, timezone, tourism in _CITIES
    )


def main() -> None:
    raw_path = os.getenv("UBAS_CATALOG_PATH", "./var/catalog.sqlite3")
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    catalog = SqliteCatalog(path)
    catalog.initialize()
    catalog.upsert(
        CatalogData(
            cities=_cities(),
            links=tuple(
                FlightLink(origin=origin, destination=destination)
                for origin, destination in _LINKS
            ),
        )
    )
    catalog.close()
    print(f"Каталог заполнен: {path.resolve()}")


if __name__ == "__main__":
    main()
