"""MVP request policy shared by API and worker."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.schemas import RouteRequest


class InvalidRouteRequest(ValueError):
    """A request cannot be executed under the MVP policy."""


class RequestPolicy:
    def __init__(self, config: PlannerConfig) -> None:
        self.config = config

    def validate(
        self,
        request: RouteRequest,
        *,
        now: datetime | None = None,
        origin_timezone: str | None = None,
    ) -> None:
        if (request.date_to - request.date_from).days + 1 > 30:
            raise InvalidRouteRequest("Search date range must not exceed 30 days.")
        if request.date_from < date.min + timedelta(days=2) or (
            request.date_to > date.max - timedelta(days=2)
        ):
            raise InvalidRouteRequest(
                "Search dates exceed the supported calendar range."
            )
        if (
            request.passengers != 1
            or request.citizenship != "RU"
            or request.currency != "RUB"
            or request.limit != 10
            or request.budget is not None
            or request.excluded_cities
            or request.checked_baggage_required
        ):
            raise InvalidRouteRequest(
                "MVP uses one adult, economy, RU, RUB, top 10 and no extra filters."
            )
        current = now or datetime.now(UTC)
        # Before airport geography is available reject only universally past dates.
        # Exact origin-local validation also runs against actual first-flight airports.
        today = (
            current.astimezone(ZoneInfo(origin_timezone)).date()
            if origin_timezone
            else (current.astimezone(UTC) - timedelta(hours=12)).date()
        )
        if request.date_from < today:
            raise InvalidRouteRequest("date_from is in the past in the origin city.")
