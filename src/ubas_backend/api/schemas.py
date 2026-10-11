from typing import Literal
from uuid import UUID

from pydantic import BaseModel

QueryStatus = Literal["queued", "running", "completed", "failed"]


class CreateQueryResponse(BaseModel):
    query_id: UUID


class QueryStatusResponse(BaseModel):
    query_id: UUID
    status: QueryStatus
