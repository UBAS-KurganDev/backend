"""Builders for valid catalog entities shared across test modules."""

from datetime import date
from decimal import Decimal

from ubas_backend.routing.schemas import CityProfile, Money, VisaPolicy


def city_profile(
    city_code: str = "AAA",
    *,
    name: str | None = None,
    country_code: str = "FR",
    timezone: str = "Europe/Paris",
    tourism_score: float = 0.5,
    hotel_per_night: Money | None = None,
    visas: tuple[VisaPolicy, ...] = (),
) -> CityProfile:
    """A minimal valid profile; every field is overridable per test."""

    return CityProfile(
        city_code=city_code,
        name=name or f"City {city_code}",
        country_code=country_code,
        timezone=timezone,
        tourism_score=tourism_score,
        hotel_per_night=hotel_per_night,
        visas=visas,
    )


def visa_policy(**overrides: object) -> VisaPolicy:
    """A minimal valid visa-free policy for RU citizens."""

    values: dict[str, object] = {
        "citizenship": "RU",
        "status": "allowed",
        "kind": "visa_free",
        "fee_per_person": Money(amount=Decimal(0)),
        "ease": 1.0,
        "processing_days": 0,
        "max_stay_hours": 72.0,
        "verified_at": date(2026, 1, 1),
        "valid_until": date(2027, 1, 1),
        "source": "tests",
    }
    values.update(overrides)
    return VisaPolicy.model_validate(values)
