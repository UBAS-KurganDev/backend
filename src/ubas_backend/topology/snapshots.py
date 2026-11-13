"""Combine disjoint city collections without hiding geography conflicts."""

from ubas_backend.topology.models import City, Observation, ReferenceAirport, Snapshot


def combine_snapshots(snapshots: tuple[Snapshot, ...]) -> Snapshot:
    if not snapshots:
        raise ValueError("At least one snapshot is required")
    cities: dict[str, City] = {}
    airports: dict[str, ReferenceAirport] = {}
    covered: set[str] = set()
    excluded: set[str] = set()
    unmapped: set[str] = set()
    observations: list[Observation] = []
    for snapshot in snapshots:
        if covered.intersection(snapshot.covered_cities):
            raise ValueError("Overlapping city coverage; choose one snapshot per city")
        if (
            snapshot.geography_sources != snapshots[0].geography_sources
            or snapshot.attribution != snapshots[0].attribution
        ):
            raise ValueError("Incompatible snapshot provenance")
        covered.update(snapshot.covered_cities)
        excluded.update(snapshot.excluded_airports)
        unmapped.update(snapshot.unmapped_airports)
        for city in snapshot.cities:
            if city.code in cities and cities[city.code] != city:
                raise ValueError(f"Conflicting city geography: {city.code}")
            cities[city.code] = city
        for airport in snapshot.airports:
            if airport.code in airports and airports[airport.code] != airport:
                raise ValueError(f"Conflicting airport geography: {airport.code}")
            airports[airport.code] = airport
        observations.extend(snapshot.observations)
    return Snapshot(
        geography_sources=snapshots[0].geography_sources,
        attribution=snapshots[0].attribution,
        started_at=min(item.started_at for item in snapshots),
        finished_at=max(item.finished_at for item in snapshots),
        covered_cities=tuple(sorted(covered)),
        excluded_airports=tuple(sorted(excluded)),
        unmapped_airports=tuple(sorted(unmapped - airports.keys())),
        cities=tuple(cities[code] for code in sorted(cities)),
        airports=tuple(airports[code] for code in sorted(airports)),
        observations=tuple(sorted(observations, key=lambda item: item.origin)),
    )
