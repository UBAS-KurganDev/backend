from collections.abc import Callable, Coroutine
from typing import Any

from loguru import logger

from ubas_backend.agent_scraper.cache import ResponseCache
from ubas_backend.agent_scraper.models import SearchRequest
from ubas_backend.agent_scraper.scraper_agent import run_agent_scraper

ResponseHandler = Callable[
    [SearchRequest, str, dict[str, Any]], Coroutine[Any, Any, None]
]


class FlightSearchService:
    """Сервис поиска билетов.

    Логика:
      1. Проверяем Redis-кэш по ключу запроса (направление + дата + класс + пассажиры).
      2. Если есть закешированный ответ — возвращаем его, скрапер не запускаем.
      3. Если кэш пуст — запускаем скрапер, перехваченные ответы идут сразу в кэш.
    """

    def __init__(self, cache: ResponseCache | None = None) -> None:
        self.cache = cache or ResponseCache()

    async def get_offers(
        self, request: SearchRequest, refresh: bool = False
    ) -> dict[str, Any]:
        key = request.cache_key

        if not refresh:
            cached = self.cache.get(key)
            if cached is not None:
                logger.info("Найден кэш: {key}", key=key)
                return dict(cached)
            logger.info("Кэш пуст: {key} — запускаем скрапер...", key=key)

        return await self._scrape_and_cache(request)

    async def _scrape_and_cache(self, request: SearchRequest) -> dict[str, Any]:
        ready: dict[str, Any] | None = None

        async def on_response(
            req: SearchRequest, url: str, data: dict[str, Any] | list[Any]
        ) -> None:
            nonlocal ready
            if isinstance(data, dict):
                ready = data

        await run_agent_scraper(request, on_response=on_response)

        if not ready:
            logger.info(
                "Скрапер не перехватил ни одного завершённого ответа —"
                " кэшировать нечего."
            )
            return {}

        self.cache.set(request.cache_key, ready)
        logger.info(
            "Завершённый offers сохранён по ключу: {key}", key=request.cache_key
        )
        return ready


async def get_flights(request: SearchRequest, refresh: bool = False) -> dict[str, Any]:
    """Точка входа для вызова поиска (можно использовать из main / API)."""
    service = FlightSearchService()
    return await service.get_offers(request, refresh=refresh)
