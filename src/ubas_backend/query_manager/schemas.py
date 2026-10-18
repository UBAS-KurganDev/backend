"""Contracts for durable route-search requests and their HTTP representation."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ubas_backend.routing.schemas import PlanResult, RouteRequest

type QueryStatus = Literal["queued", "running", "completed", "failed"]


class QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryFailure(QueryModel):
    """Safe error details suitable for API clients; stack traces stay in logs."""

    code: str = Field(pattern=r"^[a-z0-9_]+$")
    message: str
    retryable: bool = False


class SubmitQueryRequest(QueryModel):
    request: RouteRequest
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class SubmitQueryResponse(QueryModel):
    query_id: UUID
    status: QueryStatus
    reused: bool


class GetQueryResponse(QueryModel):
    query_id: UUID
    status: QueryStatus
    created_at: datetime
    updated_at: datetime
    attempt: int = Field(ge=0)
    result_available: bool
    failure: QueryFailure | None = None


class QueryResult(QueryModel):
    query_id: UUID
    result: PlanResult
