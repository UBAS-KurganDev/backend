"""Safe hop bounds over the agreed directed topology, independent of ticket prices."""

from collections import defaultdict, deque
from collections.abc import Iterable


def _distances(
    adjacency: dict[str, set[str]], start: str, limit: int
) -> dict[str, int]:
    distances = {start: 0}
    queue = deque([start])
    while queue:
        city = queue.popleft()
        depth = distances[city]
        if depth == limit:
            continue
        for destination in adjacency.get(city, ()):
            if destination not in distances:
                distances[destination] = depth + 1
                queue.append(destination)
    return distances


def cities_on_paths(
    links: Iterable[tuple[str, str]], origin: str, destination: str, max_segments: int
) -> tuple[str, ...]:
    """Necessary hop condition; cycles and equal-length alternatives are retained."""
    if not 1 <= max_segments <= 4:
        raise ValueError("MVP supports one to four flight segments")
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for first, last in links:
        forward[first].add(last)
        reverse[last].add(first)
    from_origin = _distances(forward, origin, max_segments)
    to_destination = _distances(reverse, destination, max_segments)
    return tuple(
        sorted(
            city
            for city, depth in from_origin.items()
            if city not in (origin, destination)
            and depth + to_destination.get(city, max_segments + 1) <= max_segments
        )
    )
