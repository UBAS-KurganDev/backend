"""Collect all airports of explicit cities before making any catalog changes."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from ubas_backend.topology.geography import Geography, UnknownAirportError
from ubas_backend.topology.models import Observation, Snapshot


class DestinationSource(Protocol):
    async def destinations(self, origin: str) -> Observation: ...


async def collect(
    geography: Geography,
    source: DestinationSource,
    city_codes: tuple[str, ...],
    *,
    max_airports: int = 100,
    excluded_airports: tuple[str, ...] = (),
    unmapped_airports: tuple[str, ...] = (),
    progress: Callable[[str], None] = lambda _: None,
) -> Snapshot:
    started = datetime.now(UTC)
    codes = tuple(sorted(set(city_codes)))
    if not codes or max_airports < 1:
        raise ValueError("Explicit cities and positive airport budget required")
    selected = {
        airport.code: airport
        for code in codes
        for airport in geography.airports_of(code)
    }
    excluded = set(excluded_airports)
    permitted_unmapped = set(unmapped_airports)
    if len(permitted_unmapped) != len(unmapped_airports):
        raise ValueError("Repeated unmapped airport")
    if len(excluded) != len(excluded_airports):
        raise ValueError("Repeated excluded airport")
    if excluded - selected.keys():
        raise ValueError("Excluded airport does not belong to a selected city")
    origins = {
        code: airport for code, airport in selected.items() if code not in excluded
    }
    active_cities = {airport.city_code for airport in origins.values()}
    if set(codes) - active_cities:
        raise ValueError("Every selected city requires a non-excluded airport")
    if len(origins) > max_airports:
        raise ValueError(f"Airport budget exceeded: {len(origins)} > {max_airports}")
    airports = dict(selected)
    observations = []
    observed_unmapped: set[str] = set()
    link_count = 0
    for number, code in enumerate(sorted(origins), 1):
        observation = await source.destinations(code)
        if observation.origin != code:
            raise ValueError("Source returned another origin")
        link_count += len(observation.destinations)
        if link_count > 100_000:
            raise ValueError("Snapshot exceeds 100000 airport links")
        for destination in observation.destinations:
            try:
                airports[destination.airport] = geography.airport(destination.airport)
            except UnknownAirportError:
                if destination.airport not in permitted_unmapped:
                    raise
                observed_unmapped.add(destination.airport)
        observations.append(observation)
        progress(f"{number}/{len(origins)} {code}: {len(observation.destinations)}")
    # Include all flightable airports for each covered city, including zero-route ones.
    city_ids = {airport.city_code for airport in airports.values()}
    return Snapshot(
        started_at=started,
        finished_at=datetime.now(UTC),
        covered_cities=codes,
        excluded_airports=tuple(sorted(excluded)),
        unmapped_airports=tuple(sorted(observed_unmapped)),
        airports=tuple(airports[code] for code in sorted(airports)),
        cities=tuple(geography.city(code) for code in sorted(city_ids)),
        observations=tuple(observations),
    )
