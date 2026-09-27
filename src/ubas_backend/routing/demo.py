"""Runnable synthetic example. These are not actual fares or entry regulations."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from ubas_backend.routing.models import (
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
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.storage import CatalogData, FlightLink, SqliteCatalog


class DemoFlightProvider:
    def __init__(self, offers: tuple[TicketOffer, ...]) -> None:
        self.offers = offers

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        matches = [
            offer
            for offer in self.offers
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
        )


async def main() -> None:
    now = datetime.now(UTC)
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

    provider = DemoFlightProvider(
        (
            offer("outbound", (first,), "12000"),
            offer("onward", (second,), "24000"),
            offer("through", (first, second), "42000", protected=True),
        )
    )
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
    with TemporaryDirectory(prefix="ubas-demo-") as directory:
        catalog = SqliteCatalog(Path(directory) / "catalog.sqlite3")
        catalog.initialize()
        catalog.upsert(
            CatalogData(
                cities=(city,),
                links=(
                    FlightLink(origin="MOW", destination="ALA"),
                    FlightLink(origin="ALA", destination="TYO"),
                ),
            )
        )
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


if __name__ == "__main__":
    asyncio.run(main())
