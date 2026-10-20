"""Request and entry-policy checks shared by the API and planner."""

from datetime import date, timedelta

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.schemas import CityProfile, RouteRequest, VisaPolicy


class InvalidRouteRequest(ValueError):
    """A request cannot be executed under the configured search policy."""


class RequestPolicy:
    def __init__(self, config: PlannerConfig) -> None:
        self.config = config

    def validate(self, request: RouteRequest) -> None:
        if (request.date_to - request.date_from).days + 1 > self.config.max_search_days:
            raise InvalidRouteRequest(
                f"Search date range must not exceed {self.config.max_search_days} days."
            )
        if request.date_from < date.min + timedelta(
            days=2
        ) or request.date_to > date.max - timedelta(days=2):
            raise InvalidRouteRequest(
                "Search dates exceed the supported calendar range."
            )
        if request.currency not in self.config.price_references:
            raise InvalidRouteRequest(
                f"Search currency {request.currency} is not configured."
            )


def visa_for(city: CityProfile, request: RouteRequest) -> VisaPolicy | None:
    return next(
        (visa for visa in city.visas if visa.citizenship == request.citizenship),
        None,
    )
