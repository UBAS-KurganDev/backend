"""Single-flight source searches with bounded concurrency and cancellation."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ubas_backend.agent_scraper.cache import ResponseCache
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.scraper_agent import run_agent_scraper

type Scrape = Callable[[SearchRequest, Settings], Awaitable[CapturedOffers]]


@dataclass
class _Flight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    result: CapturedOffers | None = None


class FlightSearchService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cache: ResponseCache | None = None,
        scrape: Scrape = run_agent_scraper,
    ) -> None:
        self.settings = settings or Settings()
        self.cache = cache or ResponseCache(self.settings)
        self._scrape = scrape
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._flights: dict[str, _Flight] = {}

    async def get_offers(self, request: SearchRequest) -> CapturedOffers:
        key = request.cache_key
        # No await between acquiring the shared entry and incrementing its users.
        flight = self._flights.setdefault(key, _Flight())
        flight.users += 1
        try:
            async with flight.lock:
                if flight.result is not None:
                    return flight.result
                cached = await self.cache.get(key)
                if cached is not None:
                    flight.result = cached
                    return cached
                async with self._semaphore:
                    result = await self._scrape(request, self.settings)
                await self.cache.set(key, result)
                flight.result = result
                return result
        finally:
            flight.users -= 1
            if flight.users == 0:
                del self._flights[key]

    async def aclose(self) -> None:
        # Runtime stops workers (and their searches) before closing the provider.
        await self.cache.aclose()
