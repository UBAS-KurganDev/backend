from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from ubas_backend.api.schemas import (
    CreateQueryResponse,
    QueryStatusResponse,
)
from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryResult,
    SubmitQueryRequest,
)
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.routing.schemas import RouteRequest

router = APIRouter(prefix="/queries", tags=["queries"])


def get_query_manager(request: Request) -> QueryManager:
    return cast("QueryManager", request.app.state.query_manager)


QueryManagerDep = Annotated[QueryManager, Depends(get_query_manager)]


@router.post("", response_model=CreateQueryResponse, status_code=202)
async def create_query(
    query: RouteRequest,
    query_manager: QueryManagerDep,
) -> CreateQueryResponse:
    result = await query_manager.submit_query(SubmitQueryRequest(request=query))

    return CreateQueryResponse(query_id=result.query_id)


@router.get("/{query_id}", response_model=QueryStatusResponse)
async def get_query_status(
    query_id: UUID,
    query_manager: QueryManagerDep,
) -> QueryStatusResponse:
    result: GetQueryResponse = await query_manager.get_query(query_id)

    return QueryStatusResponse(
        query_id=result.query_id,
        status=result.status,
    )


@router.get("/{query_id}/result", response_model=QueryResult)
async def get_query_result(
    query_id: UUID,
    query_manager: QueryManagerDep,
) -> QueryResult:
    return await query_manager.get_query_result(query_id)
