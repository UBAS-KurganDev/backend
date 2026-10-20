"""Stable ranking, duplicate removal and city diversity."""

import json
from collections import Counter
from datetime import UTC

from ubas_backend.routing.schemas import RankedRoute, RouteCandidate


def route_signature(route: RouteCandidate) -> str:
    return json.dumps(
        [
            [
                segment.origin.code,
                segment.destination.code,
                segment.operating_carrier,
                segment.flight_number,
                segment.departure_at.astimezone(UTC).isoformat(),
                segment.arrival_at.astimezone(UTC).isoformat(),
            ]
            for ticket in route.tickets
            for segment in ticket.segments
        ]
        + [
            [
                route.metrics.self_transfer,
                [ticket.cabin for ticket in route.tickets],
                [ticket.checked_baggage_included for ticket in route.tickets],
            ]
        ]
    )


def select_routes(
    ranked: list[RankedRoute], *, limit: int, max_per_city: int
) -> tuple[RankedRoute, ...]:
    ordered = sorted(
        ranked,
        key=lambda item: (
            -item.score.total,
            item.route.total_cost.amount,
            item.route.metrics.total_trip_hours,
            item.route.id,
        ),
    )
    selected: list[RankedRoute] = []
    seen: set[str] = set()
    per_city: Counter[str] = Counter()
    for item in ordered:
        city = item.route.stopover.city_code
        signature = route_signature(item.route)
        if signature in seen or per_city[city] >= max_per_city:
            continue
        seen.add(signature)
        per_city[city] += 1
        selected.append(item)
        if len(selected) == limit:
            break
    return tuple(selected)
