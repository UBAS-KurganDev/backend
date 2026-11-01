import json
from typing import Any

import redis
from loguru import logger

from ubas_backend.agent_scraper.config import settings


class ResponseCache:
    """Кэш ответов скрапера на основе Redis.

    Ключи вида flights:SVX:AYT:2026-10-16.

    Если Redis недоступен — кэш отключается и скрапер работает
    без кэширования.
    """

    def __init__(
        self,
        host: str = settings.redis_host,
        port: int = settings.redis_port,
        db: int = settings.redis_db,
        password: str | None = settings.redis_password,
        ttl_seconds: int = settings.cache_ttl_seconds,
    ) -> None:
        self._client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            socket_connect_timeout=2,
            socket_timeout=3,
            decode_responses=True,
        )
        self._ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False

    def get(self, key: str) -> Any | None:
        """Возвращает значение по ключу, либо None при промахе/ошибке."""
        if not self.enabled:
            return None
        try:
            raw = self._client.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except (redis.RedisError, json.JSONDecodeError) as e:
            logger.warning("Ошибка чтения кэша {key}: {error}", key=key, error=e)
            return None

    def set(self, key: str, value: Any) -> None:
        """Сохраняет значение по ключу с TTL по умолчанию (2 дня)."""
        if not self.enabled:
            return
        try:
            self._client.set(key, json.dumps(value, ensure_ascii=False), ex=self._ttl)
        except (redis.RedisError, TypeError) as e:
            logger.warning("Ошибка записи кэша {key}: {error}", key=key, error=e)

    def delete(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            self._client.delete(key)
        except redis.RedisError as e:
            logger.warning("Ошибка удаления кэша {key}: {error}", key=key, error=e)
