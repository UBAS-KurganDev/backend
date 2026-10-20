"""SQLAlchemy mappings compatible with existing SQLite catalog files."""

from sqlalchemy import CheckConstraint, Index, Text, column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class CatalogBase(DeclarativeBase):
    pass


class CityRow(CatalogBase):
    __tablename__ = "city_profiles"

    city_code: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_json: Mapped[str] = mapped_column(Text)


class FlightLinkRow(CatalogBase):
    __tablename__ = "flight_links"
    __table_args__ = (
        CheckConstraint(column("origin") != column("destination")),
        Index("flight_links_destination", "destination", "origin"),
    )

    origin: Mapped[str] = mapped_column(Text, primary_key=True)
    destination: Mapped[str] = mapped_column(Text, primary_key=True)
