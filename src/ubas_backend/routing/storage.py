"""SQLite catalog backed by SQLAlchemy; synchronous I/O runs off the event loop."""

import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import URL, Engine, create_engine, delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ubas_backend.routing.catalog_models import CatalogBase, CityRow, FlightLinkRow
from ubas_backend.routing.schemas import CityProfile, Code, Model
from ubas_backend.routing.topology import cities_on_paths


class FlightLink(Model):
    origin: Code
    destination: Code


class CatalogData(Model):
    cities: tuple[CityProfile, ...]
    links: tuple[FlightLink, ...]


class SqliteCatalog:
    """Catalog administration only; planner reads use independent repositories."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self.path)),
            poolclass=NullPool,
        )

    def initialize(self) -> None:
        CatalogBase.metadata.create_all(self._engine)

    def close(self) -> None:
        self._engine.dispose()

    def upsert(self, data: CatalogData) -> None:
        with Session(self._engine) as session, session.begin():
            for city in data.cities:
                statement = insert(CityRow).values(
                    city_code=city.city_code, profile_json=city.model_dump_json()
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[CityRow.city_code],
                        set_={"profile_json": statement.excluded.profile_json},
                    )
                )
            for link in data.links:
                session.execute(
                    insert(FlightLinkRow)
                    .values(origin=link.origin, destination=link.destination)
                    .on_conflict_do_nothing()
                )

    def replace_topology(self, data: CatalogData, origins: tuple[str, ...]) -> None:
        """Replace only covered origins atomically; preserve existing city profiles."""
        covered = set(origins)
        if not covered or any(link.origin not in covered for link in data.links):
            raise ValueError("Every imported link must belong to the covered origins")
        if any(link.origin == link.destination for link in data.links):
            raise ValueError("City self-loops are not supported")
        with Session(self._engine) as session, session.begin():
            for city in data.cities:
                session.execute(
                    insert(CityRow)
                    .values(
                        city_code=city.city_code, profile_json=city.model_dump_json()
                    )
                    .on_conflict_do_nothing()
                )
            for offset in range(0, len(origins), 500):
                session.execute(
                    delete(FlightLinkRow).where(
                        FlightLinkRow.origin.in_(origins[offset : offset + 500])
                    )
                )
            for link in data.links:
                session.execute(
                    insert(FlightLinkRow)
                    .values(origin=link.origin, destination=link.destination)
                    .on_conflict_do_nothing()
                )


def _read_engine(path: Path) -> Engine:
    path = path.resolve()
    if not path.is_file():
        raise RuntimeError(f"Catalog database does not exist: {path}")
    return create_engine(
        URL.create(
            "sqlite+pysqlite",
            database=path.as_uri(),
            query={"mode": "ro", "uri": "true"},
        ),
        poolclass=NullPool,
    )


class SqliteTopologyRepository:
    def __init__(self, path: Path) -> None:
        self._engine = _read_engine(path)

    def close(self) -> None:
        self._engine.dispose()

    async def cities_on_paths(
        self, origin: str, destination: str, max_segments: int
    ) -> tuple[str, ...]:
        def read() -> tuple[str, ...]:
            with Session(self._engine) as session:
                rows = session.execute(
                    select(FlightLinkRow.origin, FlightLinkRow.destination)
                )
                return cities_on_paths(
                    ((first, last) for first, last in rows),
                    origin,
                    destination,
                    max_segments,
                )

        return await asyncio.to_thread(read)

    async def destinations(self, origins: tuple[str, ...]) -> tuple[str, ...]:
        def read() -> tuple[str, ...]:
            statement = (
                select(FlightLinkRow.destination)
                .distinct()
                .where(FlightLinkRow.origin.in_(origins))
                .order_by(FlightLinkRow.destination)
            )
            with Session(self._engine) as session:
                return tuple(session.scalars(statement))

        return await asyncio.to_thread(read)


class SqliteCityRepository:
    def __init__(self, path: Path) -> None:
        self._engine = _read_engine(path)

    def close(self) -> None:
        self._engine.dispose()

    async def get_many(self, city_codes: tuple[str, ...]) -> tuple[CityProfile, ...]:
        def read() -> tuple[CityProfile, ...]:
            result: list[CityProfile] = []
            with Session(self._engine) as session:
                for offset in range(0, len(city_codes), 500):
                    batch = city_codes[offset : offset + 500]
                    profiles = session.scalars(
                        select(CityRow.profile_json)
                        .where(CityRow.city_code.in_(batch))
                        .order_by(CityRow.city_code)
                    )
                    for item in profiles:
                        data = json.loads(item)
                        # Old catalog JSON used a 0..1 scale; normalize at the boundary.
                        if "profile_version" not in data:
                            data["tourism_score"] *= 100
                        result.append(CityProfile.model_validate(data))
            return tuple(result)

        return await asyncio.to_thread(read)


class SqliteGeographyRepository:
    """Read stable geography even if tourism fields are absent or unavailable."""

    def __init__(self, path: Path) -> None:
        self._engine = _read_engine(path)

    def close(self) -> None:
        self._engine.dispose()

    async def timezone(self, city_code: str) -> str | None:
        def read() -> str | None:
            with Session(self._engine) as session:
                raw = session.scalar(
                    select(CityRow.profile_json).where(CityRow.city_code == city_code)
                )
                if raw is None:
                    return None
                value = json.loads(raw).get("timezone")
                if not isinstance(value, str):
                    return None
                ZoneInfo(value)
                return value

        return await asyncio.to_thread(read)
