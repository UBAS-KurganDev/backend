"""Storage boundary for the API service and the asynchronous worker."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryFailure,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)
from ubas_backend.routing.schemas import RouteRequest


class QueryNotFoundError(Exception):
    """The requested query identifier does not exist."""


class QueryNotReadyError(Exception):
    """The query has not produced a successful result yet."""


class IdempotencyConflictError(Exception):
    """One idempotency key was reused with a different request body."""


class ClaimedQuery:
    """One lease owner may run the query until its expiry."""

    def __init__(
        self,
        *,
        query_id: UUID,
        request: RouteRequest,
        lease_token: UUID,
        attempt: int,
        lease_expires_at: datetime,
    ) -> None:
        self.query_id = query_id
        self.request = request
        self.lease_token = lease_token
        self.attempt = attempt
        self.lease_expires_at = lease_expires_at


class QueryRepository(Protocol):
    async def submit(self, request: SubmitQueryRequest) -> SubmitQueryResponse: ...

    async def get(self, query_id: UUID) -> GetQueryResponse: ...

    async def get_result(self, query_id: UUID) -> QueryResult: ...

    async def claim_next(self, now: datetime) -> ClaimedQuery | None: ...

    async def complete(
        self, query_id: UUID, lease_token: UUID, result: QueryResult
    ) -> bool: ...

    async def fail(
        self,
        query_id: UUID,
        lease_token: UUID,
        failure: QueryFailure,
        retry_at: datetime | None,
    ) -> bool: ...

    async def extend_lease(
        self, query_id: UUID, lease_token: UUID, now: datetime
    ) -> bool: ...

    async def release(self, query_id: UUID, lease_token: UUID) -> bool: ...
