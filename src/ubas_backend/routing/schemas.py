"""Validated, JSON-serializable contracts at the application boundaries."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

type Code = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
type CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
type UnitScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
type PositiveHours = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Money(Model):
    amount: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: Code = "RUB"


class Airport(Model):
    code: Code
    city_code: Code
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown IANA timezone") from error
        return value


class FlightSegment(Model):
    origin: Airport
    destination: Airport
    departure_at: AwareDatetime
    arrival_at: AwareDatetime
    marketing_carrier: str = Field(min_length=2, max_length=3)
    operating_carrier: str = Field(min_length=2, max_length=3)
    flight_number: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_flight(self) -> Self:
        if self.arrival_at.astimezone(UTC) <= self.departure_at.astimezone(UTC):
            raise ValueError("Flight arrival must be after departure")
        if self.origin.city_code == self.destination.city_code:
            raise ValueError("Flight must connect different cities")
        return self


class TicketOffer(Model):
    """An independently purchasable offer; total price covers all passengers."""

    provider: str = Field(min_length=1)
    offer_id: str = Field(min_length=1)
    segments: tuple[FlightSegment, ...] = Field(min_length=1, max_length=2)
    total_price: Money
    passengers: int = Field(ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    checked_baggage_included: bool | None = None
    protected_connection: bool = False
    booking_url: HttpUrl | None = None
    fetched_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def valid_offer(self) -> Self:
        if self.expires_at.astimezone(UTC) <= self.fetched_at.astimezone(UTC):
            raise ValueError("Offer must expire after it was fetched")
        for first, second in zip(self.segments, self.segments[1:], strict=False):
            if first.destination.city_code != second.origin.city_code:
                raise ValueError("Offer segments must connect in the same city")
            if first.arrival_at.astimezone(UTC) >= second.departure_at.astimezone(UTC):
                raise ValueError("Offer segments must be chronological")
        return self


class FlightQuery(Model):
    """None means nonstop only; a stopover city requests one through offer."""

    origin: Code
    destination: Code
    departure_from: date
    departure_to: date
    stopover_city: Code | None = None
    max_stopover_hours: PositiveHours = 72
    passengers: int = Field(ge=1, le=9)
    currency: Code = "RUB"
    limit: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_query(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        if self.departure_from > self.departure_to:
            raise ValueError("Departure date range is reversed")
        if self.stopover_city in (self.origin, self.destination):
            raise ValueError("Stopover must differ from route endpoints")
        return self


class FlightSearchResult(Model):
    offers: tuple[TicketOffer, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()


class VisaPolicy(Model):
    """Entry permission for sightseeing, not merely airside transit."""

    citizenship: CountryCode = "RU"
    status: Literal["allowed", "unavailable", "unknown"]
    kind: Literal["visa_free", "on_arrival", "evisa", "advance", "unknown"]
    fee_per_person: Money | None = None
    ease: UnitScore
    processing_days: int = Field(ge=0)
    max_stay_hours: PositiveHours
    verified_at: date
    valid_until: date
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_policy(self) -> Self:
        if self.valid_until < self.verified_at:
            raise ValueError("Visa validity must follow verification")
        if self.status == "allowed" and (
            self.fee_per_person is None or self.kind == "unknown"
        ):
            raise ValueError("Allowed entry requires known visa terms and fee")
        return self


class CityProfile(Model):
    city_code: Code
    name: str = Field(min_length=1)
    country_code: CountryCode
    timezone: str
    tourism_score: UnitScore
    minimum_visit_hours: PositiveHours = 3
    rewarding_visit_hours: PositiveHours = 16
    hotel_per_night: Money | None = None
    visas: tuple[VisaPolicy, ...]

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown IANA timezone") from error
        return value

    @model_validator(mode="after")
    def valid_profile(self) -> Self:
        if self.minimum_visit_hours > self.rewarding_visit_hours:
            raise ValueError("Minimum visit cannot exceed rewarding visit duration")
        citizenships = [visa.citizenship for visa in self.visas]
        if len(citizenships) != len(set(citizenships)):
            raise ValueError("Only one visa policy per citizenship is permitted")
        return self


class RouteRequest(Model):
    origin: Code
    destination: Code
    date_from: date
    date_to: date
    citizenship: CountryCode = "RU"
    passengers: int = Field(ge=1, le=9, default=1)
    currency: Code = "RUB"
    budget: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    limit: int = Field(ge=1, le=50, default=10)
    excluded_cities: tuple[Code, ...] = ()
    checked_baggage_required: bool = False

    @model_validator(mode="after")
    def valid_request(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        if self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        return self


class VisitMetrics(Model):
    stopover_hours: NonNegative
    available_hours: NonNegative
    daytime_hours: NonNegative
    nighttime_hours: NonNegative
    daytime_fraction: UnitScore
    total_trip_hours: NonNegative
    airport_change: bool
    self_transfer: bool


class RouteCandidate(Model):
    id: str
    tickets: tuple[TicketOffer, ...]
    stopover: CityProfile
    visa: VisaPolicy
    ticket_cost: Money
    visa_cost: Money
    total_cost: Money
    metrics: VisitMetrics
    warnings: tuple[str, ...] = ()


class ScoreComponent(Model):
    name: str
    value: UnitScore
    weight: NonNegative
    contribution: NonNegative


class RouteScore(Model):
    total: float = Field(ge=0, le=100)
    components: tuple[ScoreComponent, ...]


class RankedRoute(Model):
    route: RouteCandidate
    score: RouteScore


class PlanResult(Model):
    routes: tuple[RankedRoute, ...]
    searched_cities: tuple[str, ...]
    evaluated_candidates: int
    rejected: dict[str, int]
    complete: bool
    warnings: tuple[str, ...]
    generated_at: datetime
