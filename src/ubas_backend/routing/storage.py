"""Small SQLite catalog on the SQLAlchemy ORM; swap via other ports implementations."""

from pathlib import Path
from typing import Any

from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased
from sqlalchemy.pool import NullPool

from ubas_backend.routing.orm import (
    CITY_UPDATE_COLUMNS,
    Base,
    CityProfileRow,
    FlightLinkRow,
    VisaPolicyRow,
    city_values,
    row_to_city,
    visa_values,
)
from ubas_backend.routing.schemas import CityProfile, Code, Model

SCHEMA_VERSION = 2
RETIRED_SCHEMA_VERSION = 1  # JSON-blob layout that predated the ORM mapping


def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """SQLite leaves foreign keys off unless a connection opts in."""

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class FlightLink(Model):
    origin: Code
    destination: Code


class CatalogData(Model):
    cities: tuple[CityProfile, ...]
    links: tuple[FlightLink, ...]


class SqliteCatalog:
    """Async ORM sessions over aiosqlite; NullPool: one connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}", poolclass=NullPool
        )
        event.listen(self._engine.sync_engine, "connect", _enable_foreign_keys)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Create the schema; refuse catalogs written by the retired layout."""

        async with self._engine.connect() as connection:
            version = int(
                (await connection.execute(text("PRAGMA user_version"))).scalar_one()
            )
        if version == RETIRED_SCHEMA_VERSION:
            raise RuntimeError(
                f"Catalog {self.path} uses the retired JSON-blob schema; "
                "regenerate it with: ubas-import-cities --from-snapshot "
                f"data/city_profiles.json --catalog {self.path}"
            )
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Catalog {self.path} has schema version {version}, newer than "
                f"the supported {SCHEMA_VERSION}"
            )
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def upsert(self, data: CatalogData) -> None:
        """Import verified catalog records at startup or from an offline importer."""

        async with self._sessions.begin() as session:
            for profile in data.cities:
                # Visa policies are replaced wholesale together with the profile.
                await session.execute(
                    delete(VisaPolicyRow).where(
                        VisaPolicyRow.city_code == profile.city_code
                    )
                )
                values = city_values(profile)
                statement = sqlite_insert(CityProfileRow).values(**values)
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[CityProfileRow.city_code],
                        set_={
                            column: statement.excluded[column]
                            for column in CITY_UPDATE_COLUMNS
                        },
                    )
                )
                for visa in profile.visas:
                    session.add(
                        VisaPolicyRow(city_code=profile.city_code, **visa_values(visa))
                    )
            if data.links:
                statement = sqlite_insert(FlightLinkRow).on_conflict_do_nothing(
                    index_elements=[
                        FlightLinkRow.origin,
                        FlightLinkRow.destination,
                    ]
                )
                await session.execute(
                    statement,
                    [
                        {"origin": link.origin, "destination": link.destination}
                        for link in data.links
                    ],
                )

    async def connecting_cities(self, origin: str, destination: str) -> tuple[str, ...]:
        outgoing = aliased(FlightLinkRow, name="outgoing")
        incoming = aliased(FlightLinkRow, name="incoming")
        query = (
            select(outgoing.destination)
            .join(incoming, incoming.origin == outgoing.destination)
            .where(outgoing.origin == origin, incoming.destination == destination)
            .order_by(outgoing.destination)
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        return tuple(str(row[0]) for row in rows)

    async def get_many(self, city_codes: tuple[str, ...]) -> tuple[CityProfile, ...]:
        profiles: list[CityProfile] = []
        async with self._sessions() as session:
            for offset in range(0, len(city_codes), 500):
                batch = city_codes[offset : offset + 500]
                query = (
                    select(CityProfileRow)
                    .where(CityProfileRow.city_code.in_(batch))
                    .order_by(CityProfileRow.city_code)
                )
                rows = (await session.scalars(query)).all()
                profiles.extend(row_to_city(row) for row in rows)
        return tuple(profiles)

    async def get(self, city_code: str) -> CityProfile | None:
        async with self._sessions() as session:
            row = await session.get(CityProfileRow, city_code)
        return row_to_city(row) if row is not None else None

    async def list_by_tourism(
        self, *, limit: int | None = None, min_tourism_score: float | None = None
    ) -> tuple[CityProfile, ...]:
        """Profiles ordered by tourism score descending, then by city code."""

        query = select(CityProfileRow).order_by(
            CityProfileRow.tourism_score.desc(), CityProfileRow.city_code
        )
        if min_tourism_score is not None:
            query = query.where(CityProfileRow.tourism_score >= min_tourism_score)
        if limit is not None:
            query = query.limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(query)).all()
        return tuple(row_to_city(row) for row in rows)
