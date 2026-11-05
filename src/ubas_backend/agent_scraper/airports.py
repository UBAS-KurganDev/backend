"""Справочник аэропортов скрапера agent.ru для доменных контрактов backend.

Контракт backend требует для каждого сегмента код аэропорта, код города
и IANA-таймзону. agent.ru оперирует только кодами аэропортов, поэтому
соответствия хранятся здесь. Код города не подменяется кодом аэропорта
(инвариант AGENTS.md): у аэропортов одного города он общий.

Расширяйте таблицу по мере появления новых направлений в запросах.
"""

from ubas_backend.routing.schemas import Airport

_AIRPORTS: dict[str, tuple[str, str]] = {
    # Код аэропорта: (код города, IANA timezone)
    "SVX": ("SVX", "Asia/Yekaterinburg"),
    "LED": ("LED", "Europe/Moscow"),
    "AER": ("AER", "Europe/Moscow"),
    "KZN": ("KZN", "Europe/Moscow"),
    "OVB": ("OVB", "Asia/Novosibirsk"),
    "UFA": ("UFA", "Asia/Yekaterinburg"),
    "KRR": ("KRR", "Europe/Moscow"),
    "MRV": ("MRV", "Europe/Moscow"),
    "MMK": ("MMK", "Europe/Moscow"),
    "SVO": ("MOW", "Europe/Moscow"),
    "DME": ("MOW", "Europe/Moscow"),
    "VKO": ("MOW", "Europe/Moscow"),
    "ZIA": ("MOW", "Europe/Moscow"),
    "AYT": ("AYT", "Europe/Istanbul"),
    "IST": ("IST", "Europe/Istanbul"),
    "SAW": ("IST", "Europe/Istanbul"),
    "ALA": ("ALA", "Asia/Almaty"),
    "NQZ": ("NQZ", "Asia/Almaty"),
    "TSE": ("NQZ", "Asia/Almaty"),
    "TAS": ("TAS", "Asia/Tashkent"),
    "EVN": ("EVN", "Asia/Yerevan"),
    "TBS": ("TBS", "Asia/Tbilisi"),
    "GYD": ("BAK", "Asia/Baku"),
    "DXB": ("DXB", "Asia/Dubai"),
    "DWC": ("DXB", "Asia/Dubai"),
    "HRG": ("HRG", "Africa/Cairo"),
    "SSH": ("SSH", "Africa/Cairo"),
    "CAI": ("CAI", "Africa/Cairo"),
    "BKK": ("BKK", "Asia/Bangkok"),
    "DMK": ("BKK", "Asia/Bangkok"),
    "HKT": ("HKT", "Asia/Bangkok"),
    "PEK": ("BJS", "Asia/Shanghai"),
    "PKX": ("BJS", "Asia/Shanghai"),
    "HND": ("TYO", "Asia/Tokyo"),
    "NRT": ("TYO", "Asia/Tokyo"),
    "CAN": ("CAN", "Asia/Shanghai"),
    "DEL": ("DEL", "Asia/Kolkata"),
    "GOI": ("GOI", "Asia/Kolkata"),
    "GOX": ("GOI", "Asia/Kolkata"),
    "MSQ": ("MSQ", "Europe/Minsk"),
}

_cache: dict[str, Airport] = {}

# Historical codes can be decoded, but must not create duplicate/obsolete searches.
_RETIRED_CODES = frozenset({"TSE"})


def airport_for_code(code: str) -> Airport | None:
    """Возвращает Airport по коду или None, если код не известен."""
    cached = _cache.get(code)
    if cached is not None:
        return cached
    entry = _AIRPORTS.get(code)
    if entry is None:
        return None
    city_code, timezone = entry
    airport = Airport(code=code, city_code=city_code, timezone=timezone)
    _cache[code] = airport
    return airport


def airports_for_city(city_code: str) -> tuple[Airport, ...]:
    """Supported catalog coverage; never treat an arbitrary city as an airport."""
    return tuple(
        airport
        for code, (city, _) in _AIRPORTS.items()
        if city == city_code
        and code not in _RETIRED_CODES
        and (airport := airport_for_code(code)) is not None
    )


class AirportDirectory:
    """Read-only lookup; only fully collected cities may be searched."""

    def __init__(
        self,
        airports: tuple[Airport, ...],
        searchable_airports: frozenset[str],
        *,
        fallback: AirportDirectory | None = None,
    ) -> None:
        self._fallback = fallback
        self._airports = {airport.code: airport for airport in airports}
        if len(self._airports) != len(airports):
            raise ValueError("Duplicate airport codes")
        if not searchable_airports <= self._airports.keys():
            raise ValueError("Unknown searchable airports")
        cities: dict[str, list[Airport]] = {}
        for code in sorted(searchable_airports):
            airport = self._airports[code]
            cities.setdefault(airport.city_code, []).append(airport)
        self._cities = {code: tuple(items) for code, items in cities.items()}

    def airport_for_code(self, code: str) -> Airport | None:
        return self._airports.get(code) or (
            self._fallback.airport_for_code(code) if self._fallback else None
        )

    def airports_for_city(self, city_code: str) -> tuple[Airport, ...]:
        return self._cities.get(city_code) or (
            self._fallback.airports_for_city(city_code) if self._fallback else ()
        )


def default_directory() -> AirportDirectory:
    return AirportDirectory(
        tuple(
            airport
            for code in _AIRPORTS
            if (airport := airport_for_code(code)) is not None
        ),
        frozenset(_AIRPORTS) - _RETIRED_CODES,
    )
