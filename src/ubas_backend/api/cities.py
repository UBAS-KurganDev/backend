"""Read-only endpoints over the city catalog."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from ubas_backend.routing.ports import CityDirectory
from ubas_backend.routing.schemas import CityProfile

router = APIRouter(prefix="/cities", tags=["cities"])


def get_catalog(request: Request) -> CityDirectory:
    catalog = getattr(request.app.state, "cities", None)
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="City catalog is not configured.",
        )
    return cast("CityDirectory", catalog)


CatalogDep = Annotated[CityDirectory, Depends(get_catalog)]


@router.get("", response_model=tuple[CityProfile, ...])
async def list_cities(
    catalog: CatalogDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    min_tourism_score: Annotated[float, Query(ge=0.0, le=100.0)] = 0,
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
    profiles = await catalog.get_many((city_code,))
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="City was not found."
        )
    return profiles[0]
