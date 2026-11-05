"""Bounded memory cache with optional nonblocking Redis persistence."""

import logging
from collections import OrderedDict
from datetime import UTC, datetime
from math import ceil

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers

logger = logging.getLogger(__name__)


class ResponseCache:
    def __init__(self, settings: Settings) -> None:
        self._ttl = settings.cache_ttl_seconds
        self._max_entries = settings.cache_max_entries
        self._entries: OrderedDict[str, CapturedOffers] = OrderedDict()
        url = settings.redis_url
        self._client: Redis | None = (
            Redis.from_url(
                url.get_secret_value(),
                socket_connect_timeout=1,
                socket_timeout=1,
                decode_responses=True,
            )
            if url is not None and url.get_secret_value()
            else None
        )

    def _remaining(self, value: CapturedOffers) -> float:
        age = (datetime.now(UTC) - value.fetched_at).total_seconds()
        return self._ttl - age if age >= 0 else 0

    def _remember(self, key: str, value: CapturedOffers) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def get(self, key: str) -> CapturedOffers | None:
        value = self._entries.get(key)
        if value is not None:
            if self._remaining(value) > 0:
                self._entries.move_to_end(key)
                return value
            del self._entries[key]
        if self._client is None:
            return None
        try:
            raw = await self._client.get(key)
            if raw is not None:
                value = CapturedOffers.model_validate_json(raw)
                if self._remaining(value) > 0 and value.data.get("status") == "Ready":
                    self._remember(key, value)
                    return value
        except RedisError, ValidationError, ValueError:
            # Never log Redis URLs/credentials or raw source responses.
            logger.warning("Agent cache read unavailable or invalid")
        return None

    async def set(self, key: str, value: CapturedOffers) -> None:
        remaining = self._remaining(value)
        if remaining <= 0 or value.data.get("status") != "Ready":
            return
        self._remember(key, value)
        if self._client is not None:
            try:
                await self._client.set(
                    key, value.model_dump_json(), ex=max(1, ceil(remaining))
                )
            except RedisError:
                logger.warning("Agent cache write unavailable")

    async def aclose(self) -> None:
        self._entries.clear()
        if self._client is not None:
            await self._client.aclose()
