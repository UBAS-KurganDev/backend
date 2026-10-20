"""Regression scenarios for lifecycle failures; no database or network required."""

import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from ubas_backend.query_manager.ports import ClaimedQuery, QueryRepository
from ubas_backend.query_manager.worker import QueryWorker
from ubas_backend.routing.policy import InvalidRouteRequest
from ubas_backend.routing.schemas import PlanResult, RouteRequest


def claimed_query() -> ClaimedQuery:
    return ClaimedQuery(
        query_id=uuid4(),
        request=RouteRequest(
            origin="MOW",
            destination="TYO",
            date_from=date(2027, 6, 1),
            date_to=date(2027, 6, 4),
        ),
        lease_token=uuid4(),
        attempt=1,
        max_attempts=3,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
    )


def empty_result() -> PlanResult:
    return PlanResult(
        routes=(),
        searched_cities=(),
        evaluated_candidates=0,
        rejected={},
        complete=True,
        warnings=(),
        generated_at=datetime.now(UTC),
    )


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repo = AsyncMock(spec=QueryRepository)
        self.repo.extend_lease.return_value = True
        self.repo.complete.return_value = True
        self.planner = AsyncMock()
        self.planner.plan.return_value = empty_result()
        self.worker = QueryWorker(
            self.repo,
            self.planner,
            concurrency=1,
            poll_interval=0.01,
            lease_duration=timedelta(seconds=0.09),
            retry_delay=timedelta(seconds=1),
        )

    async def asyncTearDown(self) -> None:
        await self.worker.stop()

    async def test_temporary_database_error_does_not_kill_slot(self) -> None:
        recovered = asyncio.Event()
        attempts = 0

        async def claim() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary outage")
            recovered.set()

        self.repo.claim_next.side_effect = claim
        with self.assertLogs("ubas_backend.query_manager.worker", level="ERROR"):
            self.worker.start()
            await asyncio.wait_for(recovered.wait(), 1)
        self.assertGreaterEqual(attempts, 2)
        self.assertFalse(self.worker._tasks[0].done())

    async def test_lost_lease_cancels_planning_and_does_not_write(self) -> None:
        cancelled = asyncio.Event()

        async def plan(_request: RouteRequest) -> PlanResult:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.planner.plan.side_effect = plan
        self.repo.extend_lease.return_value = False
        with self.assertLogs("ubas_backend.query_manager.worker", level="WARNING"):
            await asyncio.wait_for(self.worker._execute(claimed_query()), 1)
        self.assertTrue(cancelled.is_set())
        self.repo.complete.assert_not_called()
        self.repo.fail.assert_not_called()

    async def test_failed_heartbeat_cancels_planning(self) -> None:
        cancelled = asyncio.Event()

        async def plan(_request: RouteRequest) -> PlanResult:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.planner.plan.side_effect = plan
        self.repo.extend_lease.side_effect = ConnectionError("heartbeat unavailable")
        with self.assertRaises(ConnectionError):
            await self.worker._execute(claimed_query())
        self.assertTrue(cancelled.is_set())
        self.repo.complete.assert_not_called()

    async def test_invalid_request_is_not_retried(self) -> None:
        self.planner.plan.side_effect = InvalidRouteRequest("Invalid date range.")
        await self.worker._execute(claimed_query())
        self.assertIsNone(self.repo.fail.call_args.args[-1])
        self.assertEqual(self.repo.fail.call_args.args[2].code, "invalid_request")

    async def test_shutdown_cancels_work_before_releasing(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def plan(_request: RouteRequest) -> PlanResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def release(*_args: object) -> bool:
            self.assertTrue(cancelled.is_set())
            return True

        self.repo.release.side_effect = release
        self.planner.plan.side_effect = plan
        task = asyncio.create_task(self.worker._execute(claimed_query()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.repo.release.assert_awaited_once()

    async def test_failed_result_write_does_not_kill_slot(self) -> None:
        recovered = asyncio.Event()
        count = 0

        async def claim() -> ClaimedQuery | None:
            nonlocal count
            count += 1
            if count == 1:
                return claimed_query()
            recovered.set()
            return None

        self.repo.claim_next.side_effect = claim
        self.repo.complete.side_effect = ConnectionError("write unavailable")
        with self.assertLogs("ubas_backend.query_manager.worker", level="ERROR"):
            self.worker.start()
            await asyncio.wait_for(recovered.wait(), 1)
        self.assertFalse(self.worker._tasks[0].done())
