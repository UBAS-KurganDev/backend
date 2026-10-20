"""Composition root and deterministic cleanup for API and worker resources."""

import importlib
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import cast

from ubas_backend.database import create_query_engine, create_sessions
from ubas_backend.query_manager.repository import PostgresQueryRepository
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.query_manager.worker import QueryWorker
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.policy import RequestPolicy
from ubas_backend.routing.ports import AsyncCloseable, FlightProvider
from ubas_backend.routing.storage import SqliteCatalog
from ubas_backend.settings import Settings


def _load_provider(target: str) -> FlightProvider:
    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError(
            "UBAS_FLIGHT_PROVIDER_FACTORY must use 'package.module:factory' format"
        )
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise RuntimeError("Configured flight provider factory is not callable")
    provider = factory()
    if not callable(getattr(provider, "search", None)):
        raise RuntimeError("Flight provider must implement search(FlightQuery)")
    return cast("FlightProvider", provider)


@dataclass
class Runtime:
    manager: QueryManager
    worker: QueryWorker | None
    resources: AsyncExitStack

    async def close(self) -> None:
        await self.resources.aclose()


async def create_runtime(settings: Settings, *, include_worker: bool) -> Runtime:
    resources = AsyncExitStack()
    try:
        engine = create_query_engine(settings.database_url)
        resources.push_async_callback(engine.dispose)
        async with engine.connect():
            pass
        repository = PostgresQueryRepository(
            create_sessions(engine),
            lease_duration=settings.worker_lease_duration,
            max_attempts=settings.worker_max_attempts,
        )
        manager = QueryManager(repository, RequestPolicy(settings.planner_config))
        worker = None
        if include_worker:
            if settings.catalog_path is None or not settings.catalog_path.is_file():
                raise RuntimeError(
                    f"Catalog database does not exist: {settings.catalog_path}"
                )
            provider = _load_provider(settings.flight_provider_factory)
            if isinstance(provider, AsyncCloseable):
                resources.push_async_callback(provider.aclose)
            catalog = SqliteCatalog(settings.catalog_path)
            resources.callback(catalog.close)
            planner = RoutePlanner(
                provider, catalog, catalog, config=settings.planner_config
            )
            worker = QueryWorker(
                repository,
                planner,
                concurrency=settings.worker_concurrency,
                poll_interval=settings.worker_poll_interval,
                lease_duration=settings.worker_lease_duration,
                retry_delay=settings.worker_retry_delay,
                execution_timeout=settings.worker_execution_timeout,
            )
            resources.push_async_callback(worker.stop)
        return Runtime(manager=manager, worker=worker, resources=resources)
    except BaseException:
        await resources.aclose()
        raise
