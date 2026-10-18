"""Composition root shared by the HTTP process and dedicated workers."""

import importlib
from dataclasses import dataclass
from typing import cast

import asyncpg

from ubas_backend.query_manager.repository import PostgresQueryRepository
from ubas_backend.query_manager.service import Planner, QueryManager, QueryWorker
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.ports import FlightProvider
from ubas_backend.routing.storage import SqliteCatalog
from ubas_backend.settings import Settings


def _load_provider(target: str) -> FlightProvider:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError(
            "UBAS_FLIGHT_PROVIDER_FACTORY must use 'package.module:factory' format"
        )
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise RuntimeError("Configured flight provider factory is not callable")
    return cast("FlightProvider", factory())


@dataclass
class Runtime:
    pool: asyncpg.Pool
    manager: QueryManager
    worker: QueryWorker | None

    async def close(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
        await self.pool.close()


async def create_runtime(settings: Settings, *, include_worker: bool) -> Runtime:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    try:
        repository = PostgresQueryRepository(
            pool,
            lease_duration=settings.worker_lease_duration,
            max_attempts=settings.worker_max_attempts,
        )
        worker = None
        if include_worker:
            if settings.catalog_path is None or not settings.catalog_path.is_file():
                raise RuntimeError(
                    f"Catalog database does not exist: {settings.catalog_path}"
                )
            provider = _load_provider(settings.flight_provider_factory)
            catalog = SqliteCatalog(settings.catalog_path)
            planner: Planner = RoutePlanner(provider, catalog, catalog)
            worker = QueryWorker(
                repository,
                planner,
                concurrency=settings.worker_concurrency,
                poll_interval=settings.worker_poll_interval,
                lease_duration=settings.worker_lease_duration,
                retry_delay=settings.worker_retry_delay,
                max_attempts=settings.worker_max_attempts,
            )
    except Exception:
        await pool.close()
        raise
    return Runtime(pool=pool, manager=QueryManager(repository), worker=worker)
