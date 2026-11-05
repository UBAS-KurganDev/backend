"""Translate authoritative IATA city mappings; never derive codes from city names."""

from typing import Any

from ubas_backend.topology.models import City, ReferenceAirport


class Geography:
    def __init__(self, airports: Any, cities: Any) -> None:
        if not isinstance(airports, list) or not isinstance(cities, list):
            raise ValueError("Reference sources must return arrays")
        self._airports = self._index(
            [
                item
                for item in airports
                if isinstance(item, dict) and item.get("iata_type") == "airport"
            ]
        )
        self._cities = self._index(cities)

    @staticmethod
    def _index(items: list[Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                raise ValueError("Invalid reference entry")
            code = item["code"]
            if code in result:
                raise ValueError(f"Duplicate reference code: {code}")
            result[code] = item
        return result

    def airport(self, code: str) -> ReferenceAirport:
        raw = self._airports.get(code)
        if raw is None:
            raise ValueError(f"Unknown airport mapping: {code}")
        return ReferenceAirport.model_validate(
            {
                "code": code,
                "city_code": raw.get("city_code"),
                "timezone": raw.get("time_zone"),
                "flightable": raw.get("flightable"),
            }
        )

    def city(self, code: str) -> City:
        raw = self._cities.get(code)
        if raw is None:
            raise ValueError(f"Unknown city geography: {code}")
        return City.model_validate(
            {
                "code": code,
                "name": raw.get("name"),
                "country_code": raw.get("country_code"),
                "timezone": raw.get("time_zone"),
            }
        )

    def airports_of(self, code: str) -> tuple[ReferenceAirport, ...]:
        self.city(code)
        airports = tuple(
            self.airport(key)
            for key, raw in sorted(self._airports.items())
            if raw.get("city_code") == code and raw.get("flightable") is True
        )
        if not airports:
            raise ValueError(f"No flightable airports for city: {code}")
        return airports
