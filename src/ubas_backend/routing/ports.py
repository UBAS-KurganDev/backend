"""Replaceable data sources, independent of storage and HTTP."""

from typing import Protocol, runtime_checkable

from ubas_backend.routing.schemas import (
    CityProfile,
    FlightQuery,
    FlightSearchResult,
    FlightSegment,
    VisaExpense,
)


class FlightProviderError(Exception):
    """Expected upstream failure; never equivalent to a successful empty search."""


class CitySourceError(Exception):
    """Unavailable tourism source; missing scores fall back to zero."""


class FlightProvider(Protocol):
    async def search(self, query: FlightQuery) -> FlightSearchResult: ...


@runtime_checkable
class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class TopologyRepository(Protocol):
    async def destinations(self, origins: tuple[str, ...]) -> tuple[str, ...]:
        """All outgoing cities for these origins in the agreed coverage."""
        ...


@runtime_checkable
class PathTopologyRepository(Protocol):
    async def cities_on_paths(
        self, origin: str, destination: str, max_segments: int
    ) -> tuple[str, ...]:
        """All possible intermediate cities within the hop budget and coverage."""
        ...


class CityRepository(Protocol):
    async def get_many(self, city_codes: tuple[str, ...]) -> tuple[CityProfile, ...]:
        """Omit unavailable profiles; missing tourism scores become zero."""
        ...


class CityGeography(Protocol):
    async def timezone(self, city_code: str) -> str | None:
        """Origin timezone for local date validation, independent of tourism."""
        ...


class VisaCostResolver(Protocol):
    def expenses(
        self,
        segments: tuple[FlightSegment, ...],
        profiles: dict[str, CityProfile],
    ) -> tuple[VisaExpense, ...]:
        """Required costs for the WHOLE route, including repeated countries.

        No eligibility checks. None means unknown, not free.
        Each required charge has a unique key and is counted once.
        """
        ...
