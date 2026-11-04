"""Price-only top ten, retaining duplicate offers and without city quotas."""

from decimal import Decimal

from ubas_backend.routing.schemas import RankedRoute, RouteCandidate


class CheapestResults:
    def __init__(self) -> None:
        self._routes: list[RankedRoute] = []

    @property
    def threshold(self) -> Decimal:
        return (
            self._routes[-1].route.total_cost.amount
            if len(self._routes) == 10
            else Decimal("Infinity")
        )

    def add(self, route: RouteCandidate) -> None:
        self._routes.append(RankedRoute(route=route))
        self._routes.sort(key=lambda item: item.route.total_cost.amount)
        del self._routes[10:]

    @property
    def routes(self) -> tuple[RankedRoute, ...]:
        return tuple(self._routes)
