"""Implement these protocols to connect services, storage, or new ranking."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.schemas import (
    CityProfile,
    FlightQuery,
    FlightSearchResult,
    RouteCandidate,
    RouteScore,
)


class FlightProviderError(Exception):
    """Expected upstream failure; adapters translate transport errors here."""


class FlightProvider(Protocol):
    async def search(self, query: FlightQuery) -> FlightSearchResult: ...


@runtime_checkable
class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class TopologyRepository(Protocol):
    async def connecting_cities(self, origin: str, destination: str) -> tuple[str, ...]:
        """Intersect outgoing(origin) and incoming(destination), no live prices."""
        ...


class CityRepository(Protocol):
    async def get_many(
        self, city_codes: tuple[str, ...]
    ) -> tuple[CityProfile, ...]: ...


class RouteScorer(Protocol):
    def score(self, route: RouteCandidate, config: PlannerConfig) -> RouteScore: ...


class DaytimeCalculator(Protocol):
    def hours(
        self, start: datetime, end: datetime, timezone: str, config: PlannerConfig
    ) -> float: ...
