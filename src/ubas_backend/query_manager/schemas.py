from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from ubas_backend.routing.schemas import FlightSearchResult, PlanResult, RouteRequest

QueryStatus = Literal["queued", "running", "completed", "failed"]


class SubmitQueryRequest(BaseModel):
    request: RouteRequest


class SubmitQueryResponse(BaseModel):
    query_id: UUID
    status: QueryStatus


class GetQueryResponse(BaseModel):
    query_id: UUID
    status: QueryStatus
    created_at: datetime
    updated_at: datetime
    result: PlanResult | None = None
    error: str | None = None


class QueryResult(FlightSearchResult):
    query_id: UUID
