"""Single-flight source searches with bounded concurrency and cancellation."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ubas_backend.agent_scraper.cache import ResponseCache
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.scraper_agent import (
    TransientScrapeError,
    run_agent_scraper,
)

logger = logging.getLogger(__name__)

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
                result = await self._scrape_with_retry(request)
                await self.cache.set(key, result)
                flight.result = result
                return result
        finally:
            flight.users -= 1
            if flight.users == 0:
                del self._flights[key]

    async def _scrape_with_retry(self, request: SearchRequest) -> CapturedOffers:
        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                async with self._semaphore:
                    return await self._scrape(request, self.settings)
            except TransientScrapeError:
                if attempt == self.settings.max_attempts:
                    raise
                logger.warning(
                    "agent_retry origin=%s destination=%s date=%s attempt=%s",
                    request.origin,
                    request.destination,
                    request.depart_date,
                    attempt + 1,
                )
                await asyncio.sleep(0.5 * attempt)
        raise AssertionError("At least one source attempt is required")

    async def aclose(self) -> None:
        # Runtime stops workers (and their searches) before closing the provider.
        await self.cache.aclose()
