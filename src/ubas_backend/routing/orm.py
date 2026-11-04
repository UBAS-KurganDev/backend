"""SQLAlchemy ORM mapping between catalog rows and pydantic contracts.

Rows store every ``CityProfile`` field as a typed column; visa policies live
in their own table linked to the city. Rows are converted back through
``model_validate``, so values read from SQLite pass the same validation as
anything arriving over HTTP.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ubas_backend.routing.schemas import CityProfile, VisaPolicy

# Money keeps two decimal digits; SQLite stores NUMERIC as REAL regardless.
MONEY = Numeric(12, 2)
CITY_CODE = String(3)
COUNTRY_CODE = String(2)

# Columns of city_profiles that ON CONFLICT DO UPDATE refreshes (PK excluded).
CITY_UPDATE_COLUMNS = (
    "name",
    "country_code",
    "timezone",
    "tourism_score",
    "minimum_visit_hours",
    "rewarding_visit_hours",
    "hotel_amount",
    "hotel_currency",
)


class Base(DeclarativeBase):
    pass


class CityProfileRow(Base):
    __tablename__ = "city_profiles"

    city_code: Mapped[str] = mapped_column(CITY_CODE, primary_key=True)
    name: Mapped[str]
    country_code: Mapped[str] = mapped_column(COUNTRY_CODE)
    timezone: Mapped[str]
    tourism_score: Mapped[float]
    minimum_visit_hours: Mapped[float]
    rewarding_visit_hours: Mapped[float]
    hotel_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    hotel_currency: Mapped[str | None] = mapped_column(CITY_CODE)

    # Quoted on purpose: VisaPolicyRow is defined below; SQLAlchemy resolves
    # the forward reference at mapper configuration time.
    visas: Mapped[list["VisaPolicyRow"]] = relationship(  # noqa: UP037
        back_populates="city",
        cascade="all, delete-orphan",
        lazy="selectin",  # async sessions cannot lazy-load on attribute access
        order_by="VisaPolicyRow.citizenship",
    )


class VisaPolicyRow(Base):
    __tablename__ = "visa_policies"
    __table_args__ = (UniqueConstraint("city_code", "citizenship"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    city_code: Mapped[str] = mapped_column(
        ForeignKey("city_profiles.city_code", ondelete="CASCADE"), index=True
    )
    citizenship: Mapped[str] = mapped_column(COUNTRY_CODE)
    status: Mapped[str]
    kind: Mapped[str]
    fee_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    fee_currency: Mapped[str | None] = mapped_column(CITY_CODE)
    ease: Mapped[float]
    processing_days: Mapped[int]
    max_stay_hours: Mapped[float]
    verified_at: Mapped[date]
    valid_until: Mapped[date]
    source: Mapped[str]

    city: Mapped[CityProfileRow] = relationship(back_populates="visas")


class FlightLinkRow(Base):
    __tablename__ = "flight_links"
    __table_args__ = (
        CheckConstraint("origin <> destination"),
        Index("flight_links_destination", "destination", "origin"),
    )

    origin: Mapped[str] = mapped_column(CITY_CODE, primary_key=True)
    destination: Mapped[str] = mapped_column(CITY_CODE, primary_key=True)


def city_values(profile: CityProfile) -> dict[str, Any]:
    """Flatten a profile into city_profiles column values for INSERT."""

    hotel = profile.hotel_per_night
    return {
        "city_code": profile.city_code,
        "name": profile.name,
        "country_code": profile.country_code,
        "timezone": profile.timezone,
        "tourism_score": profile.tourism_score,
        "minimum_visit_hours": profile.minimum_visit_hours,
        "rewarding_visit_hours": profile.rewarding_visit_hours,
        "hotel_amount": hotel.amount if hotel is not None else None,
        "hotel_currency": hotel.currency if hotel is not None else None,
    }


def visa_values(visa: VisaPolicy) -> dict[str, Any]:
    """Flatten a visa policy into visa_policies column values for INSERT."""

    fee = visa.fee_per_person
    return {
        "citizenship": visa.citizenship,
        "status": visa.status,
        "kind": visa.kind,
        "fee_amount": fee.amount if fee is not None else None,
        "fee_currency": fee.currency if fee is not None else None,
        "ease": visa.ease,
        "processing_days": visa.processing_days,
        "max_stay_hours": visa.max_stay_hours,
        "verified_at": visa.verified_at,
        "valid_until": visa.valid_until,
        "source": visa.source,
    }


def row_to_city(row: CityProfileRow) -> CityProfile:
    hotel = (
        None
        if row.hotel_amount is None
        else {"amount": row.hotel_amount, "currency": row.hotel_currency}
    )
    return CityProfile.model_validate(
        {
            "city_code": row.city_code,
            "name": row.name,
            "country_code": row.country_code,
            "timezone": row.timezone,
            "tourism_score": row.tourism_score,
            "minimum_visit_hours": row.minimum_visit_hours,
            "rewarding_visit_hours": row.rewarding_visit_hours,
            "hotel_per_night": hotel,
            "visas": [_row_to_visa(visa) for visa in row.visas],
        }
    )


def _row_to_visa(row: VisaPolicyRow) -> dict[str, Any]:
    fee = (
        None
        if row.fee_amount is None
        else {"amount": row.fee_amount, "currency": row.fee_currency}
    )
    return {
        "citizenship": row.citizenship,
        "status": row.status,
        "kind": row.kind,
        "fee_per_person": fee,
        "ease": row.ease,
        "processing_days": row.processing_days,
        "max_stay_hours": row.max_stay_hours,
        "verified_at": row.verified_at,
        "valid_until": row.valid_until,
        "source": row.source,
    }
