"""SQLite catalog backed by SQLAlchemy; synchronous I/O runs off the event loop."""

import asyncio
from pathlib import Path

from sqlalchemy import URL, create_engine, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, aliased
from sqlalchemy.pool import NullPool

from ubas_backend.routing.catalog_models import CatalogBase, CityRow, FlightLinkRow
from ubas_backend.routing.schemas import CityProfile, Code, Model


class FlightLink(Model):
    origin: Code
    destination: Code


class CatalogData(Model):
    cities: tuple[CityProfile, ...]
    links: tuple[FlightLink, ...]


class SqliteCatalog:
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

    async def connecting_cities(self, origin: str, destination: str) -> tuple[str, ...]:
        def read() -> tuple[str, ...]:
            outgoing = aliased(FlightLinkRow)
            incoming = aliased(FlightLinkRow)
            statement = (
                select(outgoing.destination)
                .join(incoming, incoming.origin == outgoing.destination)
                .where(outgoing.origin == origin, incoming.destination == destination)
                .order_by(outgoing.destination)
            )
            with Session(self._engine) as session:
                return tuple(session.scalars(statement))

        return await asyncio.to_thread(read)

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
                    result.extend(
                        CityProfile.model_validate_json(item) for item in profiles
                    )
            return tuple(result)

        return await asyncio.to_thread(read)
