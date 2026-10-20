"""Transactional queue operations built from typed SQLAlchemy expressions."""

import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql.elements import ColumnElement

from ubas_backend.database import SessionFactory
from ubas_backend.query_manager.models import (
    ACTIVE_STATUSES,
    IdempotencyKeyRow,
    RouteQueryRow,
)
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


def request_fingerprint(request: RouteRequest) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


class PostgresQueryRepository:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> None:
        self._sessions = sessions
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts

    async def submit(self, request: SubmitQueryRequest) -> SubmitQueryResponse:
        fingerprint = request_fingerprint(request.request)
        async with self._sessions.begin() as session:
            reservation = await self._reserve_key(session, request, fingerprint)
            if reservation is not None and reservation.query_id is not None:
                existing = await session.get(
                    RouteQueryRow,
                    reservation.query_id,
                    options=[load_only(RouteQueryRow.id, RouteQueryRow.status)],
                )
                if existing is None:
                    raise RuntimeError("Idempotency key references a missing query")
                return self._submitted(existing, reused=True)

            row, reused = await self._find_or_create(session, request, fingerprint)
            if reservation is not None:
                reservation.query_id = row.id
            return self._submitted(row, reused=reused)

    @staticmethod
    async def _reserve_key(
        session: AsyncSession, request: SubmitQueryRequest, fingerprint: str
    ) -> IdempotencyKeyRow | None:
        if request.idempotency_key is None:
            return None
        await session.execute(
            insert(IdempotencyKeyRow)
            .values(key=request.idempotency_key, request_fingerprint=fingerprint)
            .on_conflict_do_nothing(index_elements=[IdempotencyKeyRow.key])
        )
        reservation = (
            await session.scalars(
                select(IdempotencyKeyRow)
                .where(IdempotencyKeyRow.key == request.idempotency_key)
                .with_for_update()
            )
        ).one()
        if reservation.request_fingerprint != fingerprint:
            raise IdempotencyConflictError
        return reservation

    async def _find_or_create(
        self, session: AsyncSession, request: SubmitQueryRequest, fingerprint: str
    ) -> tuple[RouteQueryRow, bool]:
        # A conflicting row may finish before the subsequent SELECT.
        # Each retry obtains a fresh READ COMMITTED statement snapshot.
        while True:
            active = await session.scalar(
                select(RouteQueryRow)
                .where(
                    RouteQueryRow.request_fingerprint == fingerprint,
                    RouteQueryRow.status.in_(ACTIVE_STATUSES),
                )
                .with_for_update()
            )
            if active is not None:
                return active, True
            inserted = await session.scalar(
                insert(RouteQueryRow)
                .values(
                    id=uuid4(),
                    request_json=request.request.model_dump(mode="json"),
                    request_fingerprint=fingerprint,
                    status="queued",
                    available_at=func.now(),
                    max_attempts=self._max_attempts,
                )
                # Let PostgreSQL use its unique indexes directly. A parameterized
                # partial-index predicate cannot be inferred by a generic plan.
                .on_conflict_do_nothing()
                .returning(RouteQueryRow)
            )
            if inserted is not None:
                return inserted, False

    @staticmethod
    def _submitted(row: RouteQueryRow, *, reused: bool) -> SubmitQueryResponse:
        return SubmitQueryResponse(query_id=row.id, status=row.status, reused=reused)

    async def get(self, query_id: UUID) -> GetQueryResponse:
        async with self._sessions() as session:
            row = await session.get(
                RouteQueryRow,
                query_id,
                options=[
                    load_only(
                        RouteQueryRow.id,
                        RouteQueryRow.status,
                        RouteQueryRow.created_at,
                        RouteQueryRow.updated_at,
                        RouteQueryRow.attempt,
                        RouteQueryRow.failure_code,
                        RouteQueryRow.failure_message,
                        RouteQueryRow.failure_retryable,
                    )
                ],
            )
            if row is None:
                raise QueryNotFoundError
            failure = (
                QueryFailure(
                    code=row.failure_code,
                    message=row.failure_message or "Route search failed.",
                    retryable=row.failure_retryable,
                )
                if row.failure_code is not None
                else None
            )
            return GetQueryResponse(
                query_id=row.id,
                status=row.status,
                created_at=row.created_at,
                updated_at=row.updated_at,
                attempt=row.attempt,
                result_available=row.status == "completed",
                failure=failure,
            )

    async def get_result(self, query_id: UUID) -> QueryResult:
        async with self._sessions() as session:
            row = await session.get(
                RouteQueryRow,
                query_id,
                options=[
                    load_only(
                        RouteQueryRow.id,
                        RouteQueryRow.status,
                        RouteQueryRow.result_json,
                    )
                ],
            )
            if row is None:
                raise QueryNotFoundError
            if row.status != "completed" or row.result_json is None:
                raise QueryNotReadyError
            return QueryResult(
                query_id=row.id, result=PlanResult.model_validate(row.result_json)
            )

    async def claim_next(self) -> ClaimedQuery | None:
        async with self._sessions.begin() as session:
            now = (await session.scalars(select(func.clock_timestamp()))).one()
            await self._expire_exhausted(session, now)
            row = await session.scalar(
                select(RouteQueryRow)
                .where(
                    RouteQueryRow.attempt < RouteQueryRow.max_attempts,
                    or_(
                        and_(
                            RouteQueryRow.status == "queued",
                            RouteQueryRow.available_at <= now,
                        ),
                        and_(
                            RouteQueryRow.status == "running",
                            RouteQueryRow.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(RouteQueryRow.available_at, RouteQueryRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "running"
            row.attempt += 1
            row.started_at = row.started_at or now
            row.updated_at = now
            row.lease_token = uuid4()
            row.lease_expires_at = now + self._lease_duration
            row.failure_code = row.failure_message = None
            row.failure_retryable = False
            return ClaimedQuery(
                query_id=row.id,
                request=RouteRequest.model_validate(row.request_json),
                lease_token=row.lease_token,
                attempt=row.attempt,
                max_attempts=row.max_attempts,
                lease_expires_at=row.lease_expires_at,
            )

    @staticmethod
    async def _expire_exhausted(session: AsyncSession, now: datetime) -> None:
        expired = (
            select(RouteQueryRow.id)
            .where(
                RouteQueryRow.attempt >= RouteQueryRow.max_attempts,
                or_(
                    RouteQueryRow.status == "queued",
                    and_(
                        RouteQueryRow.status == "running",
                        RouteQueryRow.lease_expires_at <= now,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
            .limit(100)
            .cte("exhausted")
        )
        await session.execute(
            update(RouteQueryRow)
            .where(RouteQueryRow.id.in_(select(expired.c.id)))
            .values(
                status="failed",
                updated_at=now,
                completed_at=now,
                failure_code="attempts_exhausted",
                failure_message="Route search exhausted its processing attempts.",
                failure_retryable=False,
                lease_token=None,
                lease_expires_at=None,
            ),
            execution_options={"synchronize_session": False},
        )

    @staticmethod
    def _owned(query_id: UUID, token: UUID) -> ColumnElement[bool]:
        return and_(
            RouteQueryRow.id == query_id,
            RouteQueryRow.status == "running",
            RouteQueryRow.lease_token == token,
            RouteQueryRow.lease_expires_at > func.clock_timestamp(),
        )

    async def complete(
        self, query_id: UUID, lease_token: UUID, result: QueryResult
    ) -> bool:
        async with self._sessions.begin() as session:
            saved = await session.scalar(
                update(RouteQueryRow)
                .where(self._owned(query_id, lease_token))
                .values(
                    status="completed",
                    result_json=result.result.model_dump(mode="json"),
                    updated_at=func.clock_timestamp(),
                    completed_at=func.clock_timestamp(),
                    lease_token=None,
                    lease_expires_at=None,
                )
                .returning(RouteQueryRow.id)
            )
            return saved is not None

    async def fail(
        self,
        query_id: UUID,
        lease_token: UUID,
        failure: QueryFailure,
        retry_delay: timedelta | None,
    ) -> bool:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(RouteQueryRow)
                .where(self._owned(query_id, lease_token))
                .with_for_update()
            )
            if row is None:
                return False
            now = (await session.scalars(select(func.clock_timestamp()))).one()
            retryable = retry_delay is not None and row.attempt < row.max_attempts
            row.status = "queued" if retryable else "failed"
            row.available_at = now + (retry_delay or timedelta())
            row.updated_at = now
            row.completed_at = None if retryable else now
            row.failure_code = failure.code
            row.failure_message = failure.message
            row.failure_retryable = retryable
            row.lease_token = row.lease_expires_at = None
            return True

    async def release(self, query_id: UUID, lease_token: UUID) -> bool:
        return await self.fail(
            query_id,
            lease_token,
            QueryFailure(
                code="worker_stopped",
                message="Worker stopped before finishing the search.",
                retryable=True,
            ),
            timedelta(),
        )

    async def extend_lease(self, query_id: UUID, lease_token: UUID) -> bool:
        async with self._sessions.begin() as session:
            extended = await session.scalar(
                update(RouteQueryRow)
                .where(self._owned(query_id, lease_token))
                .values(
                    lease_expires_at=func.clock_timestamp() + self._lease_duration,
                    updated_at=func.clock_timestamp(),
                )
                .returning(RouteQueryRow.id)
            )
            return extended is not None
