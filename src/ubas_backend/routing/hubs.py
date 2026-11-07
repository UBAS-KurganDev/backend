"""Discover viable hubs, then explicitly restrict the ticket-search coverage."""

import asyncio

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.ports import PathTopologyRepository, TopologyRepository
from ubas_backend.routing.schemas import CityProfile, RouteRequest
from ubas_backend.routing.topology import cities_on_paths


async def outgoing(
    topology: TopologyRepository,
    codes: tuple[str, ...],
    config: PlannerConfig,
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for offset in range(0, len(codes), config.max_concurrent_queries):
        batch = codes[offset : offset + config.max_concurrent_queries]
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(topology.destinations((code,))) for code in batch
            ]
        result.update(
            (code, set(task.result())) for code, task in zip(batch, tasks, strict=True)
        )
    return result


async def discover_cities(
    topology: TopologyRepository,
    request: RouteRequest,
    config: PlannerConfig,
    diagnostics: SearchDiagnostics,
) -> tuple[str, ...]:
    endpoints = (request.origin, request.destination)
    if isinstance(topology, PathTopologyRepository):
        candidates = await topology.cities_on_paths(*endpoints, max_segments=2)
        eligible = set(candidates) - set(endpoints)
        if len(eligible) + 2 > config.max_topology_cities:
            diagnostics.partial("topology_discovery_limit")
            return endpoints
    else:
        # Include the second edge so candidates can be checked against the target.
        seen = {request.origin}
        frontier: tuple[str, ...] = (request.origin,)
        links: set[tuple[str, str]] = set()
        for _ in range(2):
            adjacency = await outgoing(topology, frontier, config)
            links.update((a, b) for a, targets in adjacency.items() for b in targets)
            found = {b for targets in adjacency.values() for b in targets} - seen
            seen.update(found)
            if (
                len(seen | set(endpoints)) > config.max_topology_cities
                or len(links) > config.max_topology_edges
            ):
                diagnostics.partial("topology_discovery_limit")
                return endpoints
            frontier = tuple(sorted(found))
            if not frontier:
                break
        eligible = set(cities_on_paths(links, *endpoints, max_segments=2))
    return (*endpoints, *sorted(eligible))


async def select_cities(
    topology: TopologyRepository,
    candidates: tuple[str, ...],
    profiles: dict[str, CityProfile],
    config: PlannerConfig,
    diagnostics: SearchDiagnostics,
) -> tuple[str, ...]:
    origin, destination, *hubs = candidates
    if len(hubs) <= config.max_hubs:
        return candidates
    adjacency = await outgoing(topology, candidates, config)
    if sum(map(len, adjacency.values())) > config.max_topology_edges:
        diagnostics.partial("topology_discovery_limit")
        return (origin, destination)
    allowed = set(candidates)
    incoming: dict[str, set[str]] = {city: set() for city in candidates}
    for start, targets in adjacency.items():
        for end in targets & allowed:
            if start != end:
                incoming[end].add(start)

    def priority(city: str) -> tuple[float, int, int, str]:
        profile = profiles.get(city)
        tourism = profile.tourism_score if profile else 0
        direct = int(city in adjacency[origin]) + int(destination in adjacency[city])
        degree = len(incoming[city]) + len((adjacency[city] & allowed) - {city})
        return (-tourism, -direct, -degree, city)

    selected = sorted(hubs, key=priority)[: config.max_hubs]
    diagnostics.warnings.append("search_scope_limited")
    return (origin, destination, *selected)
