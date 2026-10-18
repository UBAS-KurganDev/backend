"""PostgreSQL implementation of the durable, lease-based query queue."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg

from ubas_backend.query_manager.ports import (
    ClaimedQuery,
    IdempotencyConflictError,
    QueryNotFoundError,
    QueryNotReadyError,
)
from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryFailure,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)
from ubas_backend.routing.schemas import PlanResult, RouteRequest

_ACTIVE_STATUSES = ("queued", "running")


def _canonical_request(request: RouteRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _fingerprint(request_json: str) -> str:
    return hashlib.sha256(request_json.encode("utf-8")).hexdigest()


class PostgresQueryRepository:
    """Uses database constraints, not process-local locks, for concurrency safety."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> None:
        self._pool = pool
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts

    async def submit(self, request: SubmitQueryRequest) -> SubmitQueryResponse:
        request_json = _canonical_request(request.request)
        fingerprint = _fingerprint(request_json)
        query_id = uuid4()
        async with self._pool.acquire() as connection, connection.transaction():
            if request.idempotency_key is not None:
                existing = await connection.fetchrow(
                    """SELECT id, request_fingerprint, status
                       FROM route_queries WHERE idempotency_key = $1""",
                    request.idempotency_key,
                )
                if existing is not None:
                    if existing["request_fingerprint"] != fingerprint:
                        raise IdempotencyConflictError
                    return SubmitQueryResponse(
                        query_id=existing["id"], status=existing["status"], reused=True
                    )
            row = await connection.fetchrow(
                """INSERT INTO route_queries (
                       id, request_json, request_fingerprint, idempotency_key,
                       status, available_at, max_attempts
                   ) VALUES ($1, $2::jsonb, $3, $4, 'queued', now(), $5)
                   ON CONFLICT DO NOTHING
                   RETURNING id, status""",
                query_id,
                request_json,
                fingerprint,
                request.idempotency_key,
                self._max_attempts,
            )
            if row is None:
                row = await self._find_conflicting_query(
                    connection, request.idempotency_key, fingerprint
                )
                if row is None:
                    raise RuntimeError("Could not resolve a conflicting query insert")
                if (
                    request.idempotency_key is not None
                    and row["request_fingerprint"] != fingerprint
                ):
                    raise IdempotencyConflictError
                return SubmitQueryResponse(
                    query_id=row["id"], status=row["status"], reused=True
                )
        return SubmitQueryResponse(
            query_id=row["id"], status=row["status"], reused=False
        )

    @staticmethod
    async def _find_conflicting_query(
        connection: asyncpg.Connection,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> asyncpg.Record | None:
        if idempotency_key is not None:
            row = await connection.fetchrow(
                """SELECT id, request_fingerprint, status FROM route_queries
                   WHERE idempotency_key = $1""",
                idempotency_key,
            )
            if row is not None:
                return row
        return await connection.fetchrow(
            """SELECT id, request_fingerprint, status FROM route_queries
               WHERE request_fingerprint = $1 AND status = ANY($2::text[])
               ORDER BY created_at DESC
               LIMIT 1""",
            fingerprint,
            _ACTIVE_STATUSES,
        )

    async def get(self, query_id: UUID) -> GetQueryResponse:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT id, status, created_at, updated_at, attempt,
                          failure_code, failure_message, failure_retryable
                   FROM route_queries WHERE id = $1""",
                query_id,
            )
        if row is None:
            raise QueryNotFoundError
        failure = None
        if row["failure_code"] is not None:
            failure = QueryFailure(
                code=row["failure_code"],
                message=row["failure_message"],
                retryable=row["failure_retryable"],
            )
        return GetQueryResponse(
            query_id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attempt=row["attempt"],
            result_available=row["status"] == "completed",
            failure=failure,
        )

    async def get_result(self, query_id: UUID) -> QueryResult:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, status, result_json FROM route_queries WHERE id = $1",
                query_id,
            )
        if row is None:
            raise QueryNotFoundError
        if row["status"] != "completed" or row["result_json"] is None:
            raise QueryNotReadyError
        return QueryResult(query_id=row["id"], result=_plan_result(row["result_json"]))

    async def claim_next(self, now: datetime) -> ClaimedQuery | None:
        lease_token = uuid4()
        lease_expires_at = now + self._lease_duration
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """UPDATE route_queries
                   SET status = 'failed', updated_at = $1,
                       failure_code = 'lease_expired',
                       failure_message =
                           'Worker did not finish before its lease expired.',
                       failure_retryable = false,
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE status = 'running' AND lease_expires_at <= $1
                     AND attempt >= max_attempts""",
                now,
            )
            row = await connection.fetchrow(
                """WITH next_query AS (
                       SELECT id FROM route_queries
                       WHERE (
                           status = 'queued' AND available_at <= $1
                       ) OR (
                           status = 'running' AND lease_expires_at <= $1
                           AND attempt < max_attempts
                       )
                       ORDER BY available_at, created_at
                       FOR UPDATE SKIP LOCKED
                       LIMIT 1
                   )
                   UPDATE route_queries AS query
                   SET status = 'running', attempt = query.attempt + 1,
                       started_at = COALESCE(query.started_at, $1), updated_at = $1,
                       lease_token = $2, lease_expires_at = $3,
                       failure_code = NULL, failure_message = NULL,
                       failure_retryable = false
                   FROM next_query
                   WHERE query.id = next_query.id
                   RETURNING query.id, query.request_json, query.attempt""",
                now,
                lease_token,
                lease_expires_at,
            )
        if row is None:
            return None
        return ClaimedQuery(
            query_id=row["id"],
            request=_route_request(row["request_json"]),
            lease_token=lease_token,
            attempt=row["attempt"],
            lease_expires_at=lease_expires_at,
        )

    async def complete(
        self, query_id: UUID, lease_token: UUID, result: QueryResult
    ) -> bool:
        result_json = result.result.model_dump_json()
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """UPDATE route_queries
                   SET status = 'completed', result_json = $3::jsonb,
                       completed_at = now(), updated_at = now(),
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE id = $1 AND status = 'running' AND lease_token = $2
                   RETURNING id""",
                query_id,
                lease_token,
                result_json,
            )
        return row is not None

    async def fail(
        self,
        query_id: UUID,
        lease_token: UUID,
        failure: QueryFailure,
        retry_at: datetime | None,
    ) -> bool:
        status = "queued" if retry_at is not None else "failed"
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """UPDATE route_queries
                   SET status = $3, available_at = COALESCE($4, available_at),
                       updated_at = now(), completed_at = CASE
                           WHEN $3 = 'failed' THEN now() ELSE NULL END,
                       failure_code = $5, failure_message = $6,
                       failure_retryable = $7,
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE id = $1 AND status = 'running' AND lease_token = $2
                   RETURNING id""",
                query_id,
                lease_token,
                status,
                retry_at,
                failure.code,
                failure.message,
                failure.retryable,
            )
        return row is not None

    async def release(self, query_id: UUID, lease_token: UUID) -> bool:
        """Return a shutdown-interrupted task to the queue without waiting a lease."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """UPDATE route_queries
                   SET status = 'queued', available_at = now(), updated_at = now(),
                       lease_token = NULL, lease_expires_at = NULL
                   WHERE id = $1 AND status = 'running' AND lease_token = $2
                   RETURNING id""",
                query_id,
                lease_token,
            )
        return row is not None

    async def extend_lease(
        self, query_id: UUID, lease_token: UUID, now: datetime
    ) -> bool:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """UPDATE route_queries
                   SET lease_expires_at = $3, updated_at = $2
                   WHERE id = $1 AND status = 'running' AND lease_token = $4
                   RETURNING id""",
                query_id,
                now,
                now + self._lease_duration,
                lease_token,
            )
        return row is not None


def utc_now() -> datetime:
    return datetime.now(UTC)


def _route_request(value: object) -> RouteRequest:
    if isinstance(value, str):
        return RouteRequest.model_validate_json(value)
    return RouteRequest.model_validate(value)


def _plan_result(value: object) -> PlanResult:
    if isinstance(value, str):
        return PlanResult.model_validate_json(value)
    return PlanResult.model_validate(value)
