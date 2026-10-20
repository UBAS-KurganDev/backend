"""Select connected stopovers with usable tourism and entry-policy data."""

from datetime import datetime

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics
from ubas_backend.routing.policy import visa_for
from ubas_backend.routing.ports import CityRepository, TopologyRepository
from ubas_backend.routing.schemas import CityProfile, RouteRequest


class HubSelector:
    def __init__(
        self,
        topology: TopologyRepository,
        cities: CityRepository,
        config: PlannerConfig,
    ) -> None:
        self.topology = topology
        self.cities = cities
        self.config = config

    async def select(
        self, request: RouteRequest, now: datetime, diagnostics: SearchDiagnostics
    ) -> list[CityProfile]:
        connected = set(
            await self.topology.connecting_cities(request.origin, request.destination)
        )
        connected.difference_update(
            (request.origin, request.destination, *request.excluded_cities)
        )
        profiles = await self.cities.get_many(tuple(sorted(connected)))
        missing = connected - {profile.city_code for profile in profiles}
        if missing:
            diagnostics.partial("missing_city_profiles:" + ",".join(sorted(missing)))
        hubs = []
        for profile in profiles:
            if profile.city_code not in connected:
                continue
            if profile.tourism_score < self.config.min_tourism_score:
                diagnostics.rejected["city_tourism"] += 1
                continue
            if not self._entry_available(profile, request, now):
                diagnostics.rejected["city_visa"] += 1
                continue
            hubs.append(profile)
        hubs.sort(key=lambda profile: (-profile.tourism_score, profile.city_code))
        if len(hubs) > self.config.max_hubs:
            diagnostics.partial("hub_limit")
        return hubs[: self.config.max_hubs]

    @staticmethod
    def _entry_available(
        profile: CityProfile, request: RouteRequest, now: datetime
    ) -> bool:
        visa = visa_for(profile, request)
        if visa is None or visa.status != "allowed" or visa.fee_per_person is None:
            return False
        return (
            visa.fee_per_person.currency == request.currency
            and visa.verified_at <= now.date()
            and visa.valid_until >= max(now.date(), request.date_from)
        )
