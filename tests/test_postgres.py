"""Optional real-PostgreSQL regressions. Use a disposable ubas_test_* database."""

import asyncio
import os
import unittest
from datetime import timedelta

from sqlalchemy import delete, func, make_url, select, update
from test_worker import claimed_query, empty_result

from ubas_backend.database import create_query_engine, create_sessions
from ubas_backend.migrate import migrate_connection
from ubas_backend.query_manager.models import IdempotencyKeyRow, RouteQueryRow
from ubas_backend.query_manager.ports import IdempotencyConflictError
from ubas_backend.query_manager.repository import PostgresQueryRepository
from ubas_backend.query_manager.schemas import (
    QueryFailure,
    QueryResult,
    SubmitQueryRequest,
)

TEST_DATABASE_URL = os.getenv("UBAS_TEST_DATABASE_URL")


@unittest.skipUnless(
    TEST_DATABASE_URL, "Set UBAS_TEST_DATABASE_URL for PostgreSQL checks"
)
class PostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(TEST_DATABASE_URL)
        if not (url.database or "").startswith("ubas_test_"):
            raise RuntimeError("Tests require a disposable database named ubas_test_*")
        self.engine = create_query_engine(TEST_DATABASE_URL)
        async with self.engine.begin() as connection:
            await connection.run_sync(migrate_connection)
        self.sessions = create_sessions(self.engine)
        async with self.sessions.begin() as session:
            await session.execute(delete(IdempotencyKeyRow))
            await session.execute(delete(RouteQueryRow))
        self.repo = PostgresQueryRepository(
            self.sessions, lease_duration=timedelta(seconds=10), max_attempts=3
        )
        self.request = claimed_query().request

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_concurrent_submissions_keep_every_key(self):
        requests = [
            SubmitQueryRequest(request=self.request, idempotency_key=f"key-{i}")
            for i in range(24)
        ]
        responses = await asyncio.gather(*(self.repo.submit(item) for item in requests))
        self.assertEqual(len({response.query_id for response in responses}), 1)
        self.assertEqual(sum(not response.reused for response in responses), 1)
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(IdempotencyKeyRow)
            )
        self.assertEqual(count, len(requests))
        claim = await self.repo.claim_next()
        self.assertIsNotNone(claim)
        self.assertTrue(
            await self.repo.complete(
                claim.query_id,
                claim.lease_token,
                QueryResult(query_id=claim.query_id, result=empty_result()),
            )
        )
        repeated = await asyncio.gather(*(self.repo.submit(item) for item in requests))
        self.assertEqual({item.query_id for item in repeated}, {claim.query_id})
        self.assertTrue(all(item.status == "completed" for item in repeated))
        fresh = await self.repo.submit(SubmitQueryRequest(request=self.request))
        self.assertNotEqual(fresh.query_id, claim.query_id)

    async def test_same_key_different_payload_has_one_winner(self):
        requests = [
            SubmitQueryRequest(request=self.request, idempotency_key="shared"),
            SubmitQueryRequest(
                request=self.request.model_copy(update={"passengers": 2}),
                idempotency_key="shared",
            ),
        ]
        results = await asyncio.gather(
            *(self.repo.submit(item) for item in requests), return_exceptions=True
        )
        self.assertEqual(
            sum(isinstance(item, IdempotencyConflictError) for item in results), 1
        )
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(RouteQueryRow)
            )
        self.assertEqual(count, 1)

    async def test_parallel_claims_are_exclusive(self):
        for passengers in range(1, 9):
            await self.repo.submit(
                SubmitQueryRequest(
                    request=self.request.model_copy(update={"passengers": passengers})
                )
            )
        claims = await asyncio.gather(*(self.repo.claim_next() for _ in range(8)))
        self.assertTrue(all(claim is not None for claim in claims))
        self.assertEqual(len({claim.query_id for claim in claims}), 8)

    async def test_reclaimed_lease_rejects_old_owner(self):
        await self.repo.submit(SubmitQueryRequest(request=self.request))
        old = await self.repo.claim_next()
        async with self.sessions.begin() as session:
            await session.execute(
                update(RouteQueryRow)
                .where(RouteQueryRow.id == old.query_id)
                .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        self.assertFalse(await self.repo.extend_lease(old.query_id, old.lease_token))
        new = await self.repo.claim_next()
        result = QueryResult(query_id=old.query_id, result=empty_result())
        self.assertNotEqual(old.lease_token, new.lease_token)
        self.assertFalse(
            await self.repo.complete(old.query_id, old.lease_token, result)
        )
        self.assertTrue(await self.repo.extend_lease(new.query_id, new.lease_token))
        self.assertTrue(await self.repo.complete(new.query_id, new.lease_token, result))

    async def test_retry_limit_is_stored_with_the_task(self):
        repo = PostgresQueryRepository(
            self.sessions, lease_duration=timedelta(seconds=10), max_attempts=1
        )
        response = await repo.submit(SubmitQueryRequest(request=self.request))
        claim = await self.repo.claim_next()
        self.assertEqual(claim.max_attempts, 1)
        await self.repo.fail(
            claim.query_id,
            claim.lease_token,
            QueryFailure(code="provider_error", message="Unavailable", retryable=True),
            timedelta(),
        )
        self.assertEqual((await repo.get(response.query_id)).status, "failed")
        self.assertIsNone(await self.repo.claim_next())

    async def test_finishing_and_resubmitting_does_not_fail(self):
        async def submit_many():
            return await asyncio.gather(
                *(
                    self.repo.submit(SubmitQueryRequest(request=self.request))
                    for _ in range(60)
                )
            )

        async def finish_many():
            for _ in range(60):
                claim = await self.repo.claim_next()
                if claim is not None:
                    await self.repo.complete(
                        claim.query_id,
                        claim.lease_token,
                        QueryResult(query_id=claim.query_id, result=empty_result()),
                    )
                await asyncio.sleep(0)

        responses, _ = await asyncio.wait_for(
            asyncio.gather(submit_many(), finish_many()), 15
        )
        self.assertEqual(len(responses), 60)
