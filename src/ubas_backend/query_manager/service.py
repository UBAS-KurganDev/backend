from datetime import UTC, datetime
from uuid import UUID, uuid4

from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)


# TODO написать реальные методы вместо заглушек
class QueryManager:
    async def submit_query(
        self,
        request: SubmitQueryRequest,
    ) -> SubmitQueryResponse:
        return SubmitQueryResponse(
            query_id=uuid4(),
            status="queued",
        )

    async def get_query(
        self,
        query_id: UUID,
    ) -> GetQueryResponse:
        now = datetime.now(UTC)

        return GetQueryResponse(
            query_id=query_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )

    async def get_query_result(
        self,
        query_id: UUID,
    ) -> QueryResult:
        return QueryResult(
            query_id=query_id,
            offers=(),
            complete=True,
            warnings=(),
        )
