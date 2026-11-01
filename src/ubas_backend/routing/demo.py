"""Runnable synthetic example. These are not actual fares or entry regulations."""

import argparse
import asyncio
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.schemas import (
    Airport,
    CityProfile,
    FlightQuery,
    FlightSearchResult,
    FlightSegment,
    Money,
    RouteRequest,
    TicketOffer,
    VisaPolicy,
)
from ubas_backend.routing.storage import CatalogData, FlightLink, SqliteCatalog


class DemoFlightProvider:
    def __init__(self, offers: tuple[TicketOffer, ...] | None = None) -> None:
        self.offers = offers

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        offers = (
            self.offers if self.offers is not None else _demo_offers(datetime.now(UTC))
        )
        matches = [
            offer
            for offer in offers
            if offer.segments[0].origin.city_code == query.origin
            and offer.segments[-1].destination.city_code == query.destination
            and len(offer.segments) == (2 if query.stopover_city else 1)
            and (
                query.stopover_city is None
                or offer.segments[0].destination.city_code == query.stopover_city
            )
            and query.departure_from
            <= offer.segments[0]
            .departure_at.astimezone(ZoneInfo(offer.segments[0].origin.timezone))
            .date()
            <= query.departure_to
            and offer.passengers == query.passengers
            and offer.total_price.currency == query.currency
        ]
        return FlightSearchResult(
            offers=tuple(matches[: query.limit]),
            complete=len(matches) <= query.limit,
            warnings=("synthetic_demo_data",),
        )


def create_flight_provider() -> DemoFlightProvider:
    """Opt-in worker factory; refresh synthetic quotes on every search."""
    return DemoFlightProvider()


def _demo_offers(now: datetime) -> tuple[TicketOffer, ...]:
    departure = (now + timedelta(days=14)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    moscow = Airport(code="SVO", city_code="MOW", timezone="Europe/Moscow")
    almaty = Airport(code="ALA", city_code="ALA", timezone="Asia/Almaty")
    tokyo = Airport(code="NRT", city_code="TYO", timezone="Asia/Tokyo")
    first = FlightSegment(
        origin=moscow,
        destination=almaty,
        departure_at=departure,
        arrival_at=departure + timedelta(hours=4),
        marketing_carrier="XX",
        operating_carrier="XX",
        flight_number="101",
    )
    second = FlightSegment(
        origin=almaty,
        destination=tokyo,
        departure_at=departure + timedelta(hours=38),
        arrival_at=departure + timedelta(hours=45),
        marketing_carrier="XX",
        operating_carrier="XX",
        flight_number="202",
    )

    def offer(
        offer_id: str,
        segments: tuple[FlightSegment, ...],
        amount: str,
        *,
        protected: bool = False,
    ) -> TicketOffer:
        return TicketOffer(
            provider="synthetic-demo",
            offer_id=offer_id,
            segments=segments,
            total_price=Money(amount=Decimal(amount)),
            passengers=1,
            checked_baggage_included=True,
            protected_connection=protected,
            fetched_at=now,
            expires_at=now + timedelta(minutes=20),
        )

    return (
        offer("outbound", (first,), "12000"),
        offer("onward", (second,), "24000"),
        offer("through", (first, second), "42000", protected=True),
    )


def _demo_catalog_data(now: datetime) -> CatalogData:
    city = CityProfile(
        city_code="ALA",
        name="Алматы (демонстрационные данные)",
        country_code="KZ",
        timezone="Asia/Almaty",
        tourism_score=0.85,
        rewarding_visit_hours=20,
        hotel_per_night=Money(amount=Decimal("5000")),
        visas=(
            VisaPolicy(
                citizenship="RU",
                status="allowed",
                kind="visa_free",
                fee_per_person=Money(amount=Decimal(0)),
                ease=1,
                processing_days=0,
                max_stay_hours=72,
                verified_at=now.date(),
                valid_until=(now + timedelta(days=90)).date(),
                source="SYNTHETIC DEMO: not actual immigration advice",
            ),
        ),
    )
    return CatalogData(
        cities=(city,),
        links=(
            FlightLink(origin="MOW", destination="ALA"),
            FlightLink(origin="ALA", destination="TYO"),
        ),
    )


def write_demo_catalog(
    path: Path, *, overwrite: bool = False, now: datetime | None = None
) -> None:
    """Generate synthetic data only through an explicit preparation command."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    data = _demo_catalog_data(current.astimezone(UTC))
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists; use --overwrite for a demo file")
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="ubas-demo-", dir=path.parent) as directory:
        temporary = Path(directory) / "catalog.sqlite3"
        with closing(SqliteCatalog(temporary)) as catalog:
            catalog.initialize()
            catalog.upsert(data)
        if overwrite:
            temporary.replace(path)
        else:
            # Exclusive creation also protects files created while we prepared the DB.
            with temporary.open("rb") as source, path.open("xb") as target:
                shutil.copyfileobj(source, target)


async def main() -> None:
    now = datetime.now(UTC)
    offers = _demo_offers(now)
    provider = DemoFlightProvider(offers)
    departure = offers[0].segments[0].departure_at
    with TemporaryDirectory(prefix="ubas-demo-") as directory:
        path = Path(directory) / "catalog.sqlite3"
        write_demo_catalog(path, now=now)
        with closing(SqliteCatalog(path)) as catalog:
            planner = RoutePlanner(provider, catalog, catalog)
            result = await planner.plan(
                RouteRequest(
                    origin="MOW",
                    destination="TYO",
                    date_from=departure.date(),
                    date_to=(departure + timedelta(days=3)).date(),
                ),
                now=now,
            )
        print(result.model_dump_json(indent=2))


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, help="Write a synthetic SQLite catalog")
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace the selected demo catalog"
    )
    arguments = parser.parse_args()
    if arguments.overwrite and arguments.catalog is None:
        parser.error("--overwrite requires --catalog")
    if arguments.catalog is None:
        asyncio.run(main())
        return
    try:
        write_demo_catalog(arguments.catalog, overwrite=arguments.overwrite)
    except OSError as error:
        parser.exit(1, f"Cannot create demo catalog: {error}\n")
    print(f"Synthetic demo catalog created: {arguments.catalog}")


if __name__ == "__main__":
    cli()
