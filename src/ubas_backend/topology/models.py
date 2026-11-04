"""Validated, reproducible source snapshots. No invented tourism or visa values."""

from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ubas_backend.routing.schemas import Airport, CityProfile, Code, CountryCode, Model
from ubas_backend.routing.storage import CatalogData, FlightLink

SOURCE = "https://flywhereto.com/mcp"
AIRPORTS_URL = "https://api.travelpayouts.com/data/en/airports.json"
CITIES_URL = "https://api.travelpayouts.com/data/en/cities.json"
type ServiceKind = Literal[
    "scheduled",
    "seasonal",
    "charter",
    "suspended",
    "begins",
    "ends",
    "resumes",
    "terminates",
]


class City(Model):
    code: Code
    name: str = Field(min_length=1)
    country_code: CountryCode
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    def empty_profile(self) -> CityProfile:
        return CityProfile(
            city_code=self.code,
            name=self.name,
            country_code=self.country_code,
            timezone=self.timezone,
            tourism_score=0,
        )


class ReferenceAirport(Airport):
    flightable: bool


class Destination(Model):
    airport: Code
    kinds: tuple[ServiceKind, ...] = Field(min_length=1)


class Observation(Model):
    origin: Code
    destinations: tuple[Destination, ...]
    fetched_at: AwareDatetime

    @model_validator(mode="after")
    def distinct_destinations(self) -> Self:
        codes = [item.airport for item in self.destinations]
        if len(codes) != len(set(codes)) or self.origin in codes:
            raise ValueError("Repeated destination or airport self-loop")
        return self


class Snapshot(Model):
    version: Literal[1] = 1
    source: Literal["https://flywhereto.com/mcp"] = "https://flywhereto.com/mcp"
    geography_sources: tuple[str, str] = (AIRPORTS_URL, CITIES_URL)
    attribution: str = (
        "Fly Where To? / Intuitive Bricks AB; Wikipedia airport route tables "
        "(CC BY-SA). Geography: Travelpayouts."
    )
    started_at: AwareDatetime
    finished_at: AwareDatetime
    covered_cities: tuple[Code, ...] = Field(min_length=1)
    cities: tuple[City, ...] = Field(min_length=1)
    airports: tuple[ReferenceAirport, ...] = Field(min_length=1)
    observations: tuple[Observation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        cities = {city.code: city for city in self.cities}
        airports = {airport.code: airport for airport in self.airports}
        origins = {observation.origin for observation in self.observations}
        if (
            len(cities) != len(self.cities)
            or len(airports) != len(self.airports)
            or len(origins) != len(self.observations)
            or len(set(self.covered_cities)) != len(self.covered_cities)
        ):
            raise ValueError("Duplicate snapshot identities")
        if self.finished_at < self.started_at:
            raise ValueError("Reversed collection interval")
        if not set(self.covered_cities) <= cities.keys():
            raise ValueError("Missing city geography")
        if any(airport.city_code not in cities for airport in self.airports):
            raise ValueError("Missing airport city geography")
        expected = {
            airport.code
            for airport in self.airports
            if airport.city_code in self.covered_cities and airport.flightable
        }
        if origins != expected or any(
            not any(airports[code].city_code == city for code in origins)
            for city in self.covered_cities
        ):
            raise ValueError(
                "Not all flightable airports of covered cities were fetched"
            )
        for observation in self.observations:
            if not self.started_at <= observation.fetched_at <= self.finished_at:
                raise ValueError("Observation outside collection interval")
            if any(item.airport not in airports for item in observation.destinations):
                raise ValueError("Missing destination airport geography")
        return self

    def catalog_data(self) -> CatalogData:
        cities = {airport.code: airport.city_code for airport in self.airports}
        links = {
            (cities[observation.origin], cities[item.airport])
            for observation in self.observations
            for item in observation.destinations
            if cities[observation.origin] != cities[item.airport]
        }
        return CatalogData(
            cities=tuple(city.empty_profile() for city in self.cities),
            links=tuple(FlightLink(origin=a, destination=b) for a, b in sorted(links)),
        )
