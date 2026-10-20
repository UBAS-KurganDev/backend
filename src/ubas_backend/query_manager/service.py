"""Application service: validate requests and delegate durable operations."""

from uuid import UUID

from ubas_backend.query_manager.ports import QueryRepository
from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)
from ubas_backend.routing.policy import RequestPolicy


class QueryManager:
    def __init__(self, repository: QueryRepository, policy: RequestPolicy) -> None:
        self._repository = repository
        self._policy = policy

    async def submit_query(self, request: SubmitQueryRequest) -> SubmitQueryResponse:
        self._policy.validate(request.request)
        return await self._repository.submit(request)

    async def get_query(self, query_id: UUID) -> GetQueryResponse:
        return await self._repository.get(query_id)

    async def get_query_result(self, query_id: UUID) -> QueryResult:
        return await self._repository.get_result(query_id)
