"""Create a NEW illustrative catalog, never modify the configured working catalog.

Only geography is reference data. Edges illustrate topology, scores are unknown (0),
visas/hotels are absent. This is not a source of tourism or visa information.
"""

import argparse
from pathlib import Path

from ubas_backend.routing.schemas import CityProfile
from ubas_backend.routing.storage import CatalogData, FlightLink, SqliteCatalog

_CITIES = (
    ("SVX", "Екатеринбург", "RU", "Asia/Yekaterinburg"),
    ("MOW", "Москва", "RU", "Europe/Moscow"),
    ("AYT", "Анталья", "TR", "Europe/Istanbul"),
    ("IST", "Стамбул", "TR", "Europe/Istanbul"),
    ("EVN", "Ереван", "AM", "Asia/Yerevan"),
    ("TBS", "Тбилиси", "GE", "Asia/Tbilisi"),
    ("ALA", "Алматы", "KZ", "Asia/Almaty"),
    ("TAS", "Ташкент", "UZ", "Asia/Tashkent"),
    ("HRG", "Хургада", "EG", "Africa/Cairo"),
    ("BKK", "Бангкок", "TH", "Asia/Bangkok"),
)

_LINKS = (
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


def create_example(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create also protects against a concurrent seed invocation.
    with path.open("xb"):
        pass
    catalog = SqliteCatalog(path)
    try:
        catalog.initialize()
        catalog.upsert(
            CatalogData(
                cities=tuple(
                    CityProfile(
                        city_code=code,
                        name=name,
                        country_code=country,
                        timezone=timezone,
                        tourism_score=0,
                    )
                    for code, name, country, timezone in _CITIES
                ),
                links=tuple(
                    FlightLink(origin=origin, destination=destination)
                    for origin, destination in _LINKS
                ),
            )
        )
    finally:
        catalog.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    args = parser.parse_args()
    create_example(args.catalog)
    print(f"Illustrative catalog created: {args.catalog.resolve()}")


if __name__ == "__main__":
    main()
