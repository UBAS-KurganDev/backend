from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from ubas_backend.query_manager.ports import (
    IdempotencyConflictError,
    QueryNotFoundError,
)
from ubas_backend.query_manager.schemas import (
    GetQueryResponse,
    QueryResult,
    SubmitQueryRequest,
    SubmitQueryResponse,
)
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.routing.policy import InvalidRouteRequest
from ubas_backend.routing.schemas import RouteRequest

router = APIRouter(prefix="/queries", tags=["queries"])


def get_query_manager(request: Request) -> QueryManager:
    return cast("QueryManager", request.app.state.query_manager)


QueryManagerDep = Annotated[QueryManager, Depends(get_query_manager)]


@router.post(
    "", response_model=SubmitQueryResponse, status_code=status.HTTP_202_ACCEPTED
)
async def create_query(
    query: RouteRequest,
    query_manager: QueryManagerDep,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ] = None,
) -> SubmitQueryResponse:
    try:
        result = await query_manager.submit_query(
            SubmitQueryRequest(request=query, idempotency_key=idempotency_key)
        )
    except IdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used with a different request.",
        ) from error
    except InvalidRouteRequest as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    response.headers["Location"] = f"/api/v1/queries/{result.query_id}"
    return result


@router.get("/{query_id}", response_model=GetQueryResponse)
async def get_query_status(
    query_id: UUID,
    query_manager: QueryManagerDep,
) -> GetQueryResponse:
    try:
        return await query_manager.get_query(query_id)
    except QueryNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Query was not found."
        ) from error


@router.get("/{query_id}/result", response_model=QueryResult)
async def get_query_result(
    query_id: UUID,
    query_manager: QueryManagerDep,
) -> QueryResult:
    try:
        metadata = await query_manager.get_query(query_id)
    except QueryNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Query was not found."
        ) from error
    if metadata.status in {"queued", "running"}:
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail="Query is still being processed.",
            headers={"Retry-After": "2"},
        )
    if metadata.status == "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=metadata.failure.model_dump()
            if metadata.failure
            else "Query failed.",
        )
    return await query_manager.get_query_result(query_id)
