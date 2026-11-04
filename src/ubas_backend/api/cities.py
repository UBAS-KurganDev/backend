"""Read-only endpoints over the city catalog."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ubas_backend.routing.schemas import CityProfile
from ubas_backend.routing.storage import SqliteCatalog

router = APIRouter(prefix="/cities", tags=["cities"])


def get_catalog(request: Request) -> SqliteCatalog:
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="City catalog is not configured.",
        )
    return cast("SqliteCatalog", catalog)


CatalogDep = Annotated[SqliteCatalog, Depends(get_catalog)]


@router.get("", response_model=tuple[CityProfile, ...])
async def list_cities(
    catalog: CatalogDep,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    min_tourism_score: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
) -> tuple[CityProfile, ...]:
    """List city profiles ordered by tourism score, best first."""

    return await catalog.list_by_tourism(
        limit=limit, min_tourism_score=min_tourism_score
    )


@router.get("/{city_code}", response_model=CityProfile)
async def get_city(
    city_code: Annotated[str, Path(pattern=r"^[A-Z]{3}$")],
    catalog: CatalogDep,
) -> CityProfile:
    profile = await catalog.get(city_code)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="City was not found."
        )
    return profile
