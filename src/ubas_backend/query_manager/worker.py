"""Supervised queue slots with bounded retries and cancellable lease ownership."""

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Protocol

from ubas_backend.query_manager.ports import ClaimedQuery, QueryRepository
from ubas_backend.query_manager.schemas import QueryFailure, QueryResult
from ubas_backend.routing.policy import InvalidRouteRequest
from ubas_backend.routing.schemas import PlanResult, RouteRequest

logger = logging.getLogger(__name__)


class Planner(Protocol):
    async def plan(self, request: RouteRequest) -> PlanResult: ...


class LeaseLostError(Exception):
    """The worker may no longer compute or write this task's result."""


class QueryWorker:
    def __init__(
        self,
        repository: QueryRepository,
        planner: Planner,
        *,
        concurrency: int,
        poll_interval: float,
        lease_duration: timedelta,
        retry_delay: timedelta,
        execution_timeout: float = 300,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._lease_duration = lease_duration
        self._retry_delay = retry_delay
        self._execution_timeout = execution_timeout
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if self._tasks:
            raise RuntimeError("Query worker is already running")
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._run_slot(slot), name=f"query-worker-{slot}")
            for slot in range(self._concurrency)
        ]

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), seconds)

    async def _run_slot(self, slot: int) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                claimed = await self._repository.claim_next()
                if claimed is None:
                    failures = 0
                    await self._pause(self._poll_interval)
                    continue
                await self._execute(claimed)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # Includes failed claims, result writes and heartbeat operations.
                # The lease will recover unfinished work after a database outage.
                failures += 1
                logger.exception("Worker slot failed", extra={"worker_slot": slot})
                await self._pause(min(30, self._poll_interval * 2 ** min(failures, 6)))

    async def _execute(self, claimed: ClaimedQuery) -> None:
        try:
            await self._with_lease(claimed)
        except LeaseLostError:
            logger.warning(
                "Cancelled search after losing lease",
                extra={"query_id": str(claimed.query_id)},
            )
        except asyncio.CancelledError:
            # Release is best effort; expiry still recovers the task if DB is down.
            try:
                async with asyncio.timeout(5):
                    await self._repository.release(
                        claimed.query_id, claimed.lease_token
                    )
            except Exception:
                logger.exception("Could not release interrupted query")
            raise

    async def _with_lease(self, claimed: ClaimedQuery) -> None:
        work = asyncio.create_task(self._process(claimed))
        heartbeat = asyncio.create_task(self._renew_lease(claimed))
        try:
            done, _ = await asyncio.wait(
                (work, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            # If both finish together, a successful result write takes precedence.
            if work in done:
                if work.cancelled():
                    raise RuntimeError("Planner cancelled its own task")
                await work
            else:
                if heartbeat.cancelled():
                    raise LeaseLostError
                await heartbeat
                raise LeaseLostError
        finally:
            work.cancel()
            heartbeat.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def _process(self, claimed: ClaimedQuery) -> None:
        try:
            async with asyncio.timeout(self._execution_timeout):
                result = await self._planner.plan(claimed.request)
        except InvalidRouteRequest as error:
            await self._repository.fail(
                claimed.query_id,
                claimed.lease_token,
                QueryFailure(code="invalid_request", message=str(error)),
                None,
            )
        except Exception:
            logger.exception(
                "Route search failed", extra={"query_id": str(claimed.query_id)}
            )
            retryable = claimed.attempt < claimed.max_attempts
            delay = self._retry_delay * 2 ** min(claimed.attempt - 1, 6)
            await self._repository.fail(
                claimed.query_id,
                claimed.lease_token,
                QueryFailure(
                    code="planner_error",
                    message="Route search could not be completed.",
                    retryable=retryable,
                ),
                delay if retryable else None,
            )
        else:
            saved = await self._repository.complete(
                claimed.query_id,
                claimed.lease_token,
                QueryResult(query_id=claimed.query_id, result=result),
            )
            if not saved:
                raise LeaseLostError

    async def _renew_lease(self, claimed: ClaimedQuery) -> None:
        interval = self._lease_duration.total_seconds() / 3
        while True:
            await asyncio.sleep(interval)
            # A hanging DB call must not let computation outlive its ownership.
            async with asyncio.timeout(interval):
                extended = await self._repository.extend_lease(
                    claimed.query_id, claimed.lease_token
                )
            if not extended:
                raise LeaseLostError
