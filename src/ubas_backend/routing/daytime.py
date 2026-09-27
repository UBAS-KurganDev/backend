"""Local sightseeing windows; elapsed time is always calculated in UTC."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ubas_backend.routing.config import PlannerConfig


class LocalDaytimeCalculator:
    def hours(
        self, start: datetime, end: datetime, timezone: str, config: PlannerConfig
    ) -> float:
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        if start_utc >= end_utc:
            return 0
        zone = ZoneInfo(timezone)
        day = start.astimezone(zone).date()
        last_day = end.astimezone(zone).date()
        seconds = 0.0
        while day <= last_day:
            local_start = datetime.combine(
                day, time(config.daytime_start_hour), tzinfo=zone
            )
            local_end = datetime.combine(day, time(), tzinfo=zone) + timedelta(
                hours=config.daytime_end_hour
            )
            overlap_start = max(start_utc, local_start.astimezone(UTC))
            overlap_end = min(end_utc, local_end.astimezone(UTC))
            seconds += max(0, (overlap_end - overlap_start).total_seconds())
            day += timedelta(days=1)
        return seconds / 3600
