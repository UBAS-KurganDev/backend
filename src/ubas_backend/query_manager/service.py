"""Application service and durable worker for route-search requests."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from ubas_backend.query_manager.ports import ClaimedQuery, QueryRepository
from ubas_backend.query_manager.repository import utc_now
from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryFailure,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)
from ubas_backend.routing.schemas import PlanResult, RouteRequest

logger = logging.getLogger(__name__)


class Planner(Protocol):
    async def plan(self, request: RouteRequest) -> PlanResult: ...


class QueryManager:
    """API-facing service. It persists work but never runs it in an HTTP request."""

    def __init__(self, repository: QueryRepository) -> None:
        self._repository = repository

    async def submit_query(self, request: SubmitQueryRequest) -> SubmitQueryResponse:
        return await self._repository.submit(request)

    async def get_query(self, query_id: UUID) -> GetQueryResponse:
        return await self._repository.get(query_id)

    async def get_query_result(self, query_id: UUID) -> QueryResult:
        return await self._repository.get_result(query_id)


class QueryWorker:
    """Polls the durable queue; multiple instances safely share the same database."""

    def __init__(
        self,
        repository: QueryRepository,
        planner: Planner,
        *,
        concurrency: int,
        poll_interval: float,
        lease_duration: timedelta,
        retry_delay: timedelta,
        max_attempts: int,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._max_attempts = max_attempts
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("Query worker is already running")
        self._tasks = [
            asyncio.create_task(self._run_slot(slot), name=f"query-worker-{slot}")
            for slot in range(self._concurrency)
        ]

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_slot(self, slot: int) -> None:
        while not self._stopping.is_set():
            claimed = await self._repository.claim_next(utc_now())
            if claimed is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), self._poll_interval)
                continue
            await self._execute(claimed, slot)

    async def _execute(self, claimed: ClaimedQuery, slot: int) -> None:
        lease_task = asyncio.create_task(
            self._renew_lease(claimed), name=f"query-lease-{claimed.query_id}"
        )
        try:
            result = await self._planner.plan(claimed.request)
        except asyncio.CancelledError:
            await self._repository.release(claimed.query_id, claimed.lease_token)
            raise
        except Exception:
            logger.exception(
                "Route search failed", extra={"query_id": str(claimed.query_id)}
            )
            retry_at = None
            retryable = claimed.attempt < self._max_attempts
            if retryable:
                retry_at = utc_now() + self._retry_delay * claimed.attempt
            await self._repository.fail(
                claimed.query_id,
                claimed.lease_token,
                QueryFailure(
                    code="planner_error",
                    message="Route search could not be completed.",
                    retryable=retryable,
                ),
                retry_at,
            )
        else:
            saved = await self._repository.complete(
                claimed.query_id,
                claimed.lease_token,
                QueryResult(query_id=claimed.query_id, result=result),
            )
            if not saved:
                logger.warning(
                    "Discarded stale route-search result",
                    extra={"query_id": str(claimed.query_id), "worker_slot": slot},
                )
        finally:
            lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_task

    async def _renew_lease(self, claimed: ClaimedQuery) -> None:
        interval = max(1.0, self._lease_duration.total_seconds() / 3)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), interval)
                return
            except TimeoutError:
                extended = await self._repository.extend_lease(
                    claimed.query_id,
                    claimed.lease_token,
                    utc_now(),
                )
                if not extended:
                    return
