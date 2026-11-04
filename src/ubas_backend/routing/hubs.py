"""Bounded topology discovery; never filter cities by tourism or visa."""

import asyncio

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.ports import PathTopologyRepository, TopologyRepository
from ubas_backend.routing.schemas import RouteRequest


async def discover_cities(
    topology: TopologyRepository,
    request: RouteRequest,
    config: PlannerConfig,
    diagnostics: SearchDiagnostics,
) -> tuple[str, ...]:
    if isinstance(topology, PathTopologyRepository):
        candidates = await topology.cities_on_paths(
            request.origin, request.destination, max_segments=4
        )
        eligible = sorted(set(candidates) - {request.origin, request.destination})
        if len(eligible) > config.max_hubs:
            diagnostics.partial("topology_city_limit")
        return (request.origin, request.destination, *eligible[: config.max_hubs])
    # Up to three intermediate cities. Destination is also expanded: cycles are
    # permitted by the specification, and the segment limit guarantees termination.
    seen = {request.origin}
    frontier: tuple[str, ...] = (request.origin,)
    for _ in range(3):
        found = set(await topology.destinations(frontier)) - seen
        hubs = found - {request.destination}
        available = max(
            0, config.max_hubs - len(seen - {request.origin, request.destination})
        )
        if len(hubs) > available:
            diagnostics.partial("topology_city_limit")
        frontier = tuple(sorted(hubs)[:available])
        if request.destination in found:
            frontier += (request.destination,)
        seen.update(frontier)
        if not frontier:
            break
        await asyncio.sleep(0)
    return (
        request.origin,
        request.destination,
        *sorted(seen - {request.origin, request.destination}),
    )
