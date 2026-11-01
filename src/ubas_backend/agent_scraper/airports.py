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
    "MOW": ("MOW", "Europe/Moscow"),
    "LED": ("LED", "Europe/Saint_Petersburg"),
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
    "AYT": ("AYT", "Europe/Istanbul"),
    "IST": ("IST", "Europe/Istanbul"),
    "SAW": ("IST", "Europe/Istanbul"),
    "ALA": ("ALA", "Asia/Almaty"),
    "NQZ": ("NQZ", "Asia/Almaty"),
    "TSE": ("NQZ", "Asia/Almaty"),
    "TAS": ("TAS", "Asia/Tashkent"),
    "EVN": ("EVN", "Asia/Yerevan"),
    "TBS": ("TBS", "Asia/Tbilisi"),
    "GYD": ("GYD", "Asia/Baku"),
    "DXB": ("DXB", "Asia/Dubai"),
    "HRG": ("HRG", "Africa/Cairo"),
    "SSH": ("SSH", "Africa/Cairo"),
    "CAI": ("CAI", "Africa/Cairo"),
    "BKK": ("BKK", "Asia/Bangkok"),
    "HKT": ("HKT", "Asia/Bangkok"),
    "PEK": ("BJS", "Asia/Shanghai"),
    "CAN": ("CAN", "Asia/Shanghai"),
    "DEL": ("DEL", "Asia/Kolkata"),
    "GOI": ("GOI", "Asia/Kolkata"),
    "MSQ": ("MSQ", "Europe/Minsk"),
}

_cache: dict[str, Airport] = {}


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
