"""Small deterministic flight fixtures; no real fares or immigration rules."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.schemas import (
    Airport,
    CityProfile,
    FlightSearchResult,
    FlightSegment,
    Money,
    RouteRequest,
    TicketOffer,
    VisaPolicy,
)
from ubas_backend.routing.validation import matches_query

NOW = datetime(2027, 5, 1, 12, tzinfo=UTC)
START = datetime(2027, 5, 4, tzinfo=UTC)
REQUEST = RouteRequest(
    origin="AAA",
    destination="ZZZ",
    date_from=START.date(),
    date_to=(START + timedelta(days=29)).date(),
)


def airport(city, code=None, timezone="UTC"):
    return Airport(code=code or city, city_code=city, timezone=timezone)


def segment(origin, destination, departure=0, duration=2, *, identity="1"):
    return FlightSegment(
        origin=airport(origin) if isinstance(origin, str) else origin,
        destination=airport(destination)
        if isinstance(destination, str)
        else destination,
        departure_at=START + timedelta(hours=departure),
        arrival_at=START + timedelta(hours=departure + duration),
        marketing_carrier="XX",
        operating_carrier="XX",
        flight_number=identity,
    )


def offer(identity, segments, price=100, currency="RUB", **kwargs):
    return TicketOffer(
        provider="test",
        offer_id=str(identity),
        segments=tuple(segments),
        total_price=Money(amount=Decimal(str(price)), currency=currency)
        if price is not None
        else None,
        passengers=1,
        fetched_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        baggage_description="Synthetic economy: hand luggage only",
        **kwargs,
    )


def profile(code, score=80, fee=0, currency="RUB", country="KZ"):
    return CityProfile(
        city_code=code,
        name=code,
        country_code=country,
        timezone="UTC",
        tourism_score=score,
        visas=(
            VisaPolicy(
                status="unavailable",
                kind="advance",
                fee_per_person=Money(amount=Decimal(str(fee)), currency=currency)
                if fee is not None
                else None,
                ease=0,
                processing_days=999,
                max_stay_hours=1,
                verified_at=NOW.date(),
                valid_until=NOW.date(),
                source="synthetic",
            ),
        ),
    )


class Provider:
    def __init__(self, offers):
        self.offers = tuple(offers)
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        matched = tuple(item for item in self.offers if matches_query(item, query))
        return FlightSearchResult(
            offers=matched[: query.limit], complete=len(matched) <= query.limit
        )


class Topology:
    def __init__(self, offers=()):
        self.links = {
            (part.origin.city_code, part.destination.city_code)
            for item in offers
            for part in item.segments
        }

    async def destinations(self, origins):
        return tuple(sorted({end for start, end in self.links if start in origins}))


class Cities:
    def __init__(self, profiles=()):
        self.profiles = {item.city_code: item for item in profiles}

    async def get_many(self, codes):
        return tuple(self.profiles[code] for code in codes if code in self.profiles)


class Geography:
    async def timezone(self, code):
        return "UTC"


class NoVisas:
    def expenses(self, segments, profiles):
        return ()


async def plan(offers, *, profiles=(), config=None, request=REQUEST, visas=None):
    return await RoutePlanner(
        Provider(offers),
        Topology(offers),
        Cities(profiles),
        config=config or PlannerConfig(),
        visa_costs=visas,
        geography=Geography(),
    ).plan(request, now=NOW)
