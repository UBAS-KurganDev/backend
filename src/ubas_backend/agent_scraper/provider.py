"""Адаптер скрапера agent.ru под протокол FlightProvider backend.

Реализует `routing.ports.FlightProvider` поверх `FlightSearchService`:
запрос `FlightQuery` превращается в поисковую ссылку agent.ru, перехваченный
ответ маппится в `FlightSearchResult`. Ожидаемые сбои транспорта переводятся
в `FlightProviderError` (контракт docs/routing.md); синхронный Redis-кэш
скрапера вызывается в рабочем потоке, чтобы не блокировать event loop.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.models import SearchRequest
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.ports import FlightProviderError
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult
from ubas_backend.routing.validation import matches_query

# Свежесть кэша сырых ответов в памяти процесса; Redis живёт до 2 дней.
_RAW_TTL_SECONDS = 30 * 60
_MAX_RAW_ENTRIES = 128


class _RawResponseCache:
    """TTL-кэш сырых ответов по направлению, общий для запросов планировщика.

    Скрапер отдаёт выдачу на фиксированную дату вылета; планировщик же
    запрашивает диапазоны. Ответ хранится по направлению и переиспользуется
    параллельными дочерними запросами, пока не устареет.
    """

    def __init__(self, ttl_seconds: int = _RAW_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self, origin: str, destination: str, fetch: asyncio.Future[dict[str, Any]] | Any
    ) -> dict[str, Any]:
        key = f"{origin}:{destination}"
        now = datetime.now(UTC).timestamp()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
        data = await fetch
        async with self._lock:
            self._entries[key] = (now + self._ttl, data)
            if len(self._entries) > _MAX_RAW_ENTRIES:
                stale = min(self._entries, key=lambda item: self._entries[item][0])
                del self._entries[stale]
        return data


class AgentFlightProvider:
    """FlightProvider поверх скрапера agent.ru (Playwright + Redis-кэш)."""

    def __init__(self, service: FlightSearchService | None = None) -> None:
        self._service = service or FlightSearchService()
        self._cache = _RawResponseCache()

    async def _raw_response(
        self, origin: str, destination: str, depart_date: str
    ) -> dict[str, Any]:
        async def fetch() -> dict[str, Any]:
            request = SearchRequest(
                origin=origin,
                destination=destination,
                depart_date=depart_date,
            )
            return await asyncio.to_thread(self._run_service, request)

        return await self._cache.get_or_fetch(origin, destination, fetch())

    def _run_service(self, request: SearchRequest) -> dict[str, Any]:
        """Синхронный мост: service.get_offers корутинный, Redis — синхронный."""
        return asyncio.run(self._service.get_offers(request))

    async def search(self, query: FlightQuery) -> FlightSearchResult:
        try:
            data = await self._raw_response(
                query.origin, query.destination, self._depart_date(query)
            )
        except FlightProviderError:
            raise
        except Exception as error:
            logger.warning("Скрапер agent.ru завершился ошибкой: {error}", error=error)
            raise FlightProviderError(str(error)) from error
        if not data:
            return FlightSearchResult(
                complete=False, warnings=("agent_empty_response",)
            )
        offers = map_response(data, now=datetime.now(UTC), passengers=query.passengers)
        matched = tuple(offer for offer in offers if matches_query(offer, query))
        return FlightSearchResult(
            offers=matched[: query.limit],
            complete=True,
            warnings=() if matched else ("agent_no_matching_offers",),
        )

    @staticmethod
    def _depart_date(query: FlightQuery) -> str:
        """agent.ru ищет на конкретную дату; берём середину диапазона запроса."""
        span = (query.departure_to - query.departure_from).days
        middle = query.departure_from + timedelta(days=span // 2)
        return middle.isoformat()


def create_flight_provider() -> AgentFlightProvider:
    """Фабрика для UBAS_FLIGHT_PROVIDER_FACTORY (см. .env.example)."""
    return AgentFlightProvider()
