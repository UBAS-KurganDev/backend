"""Apply packaged Alembic revisions, including adoption of the original schema."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.engine import Connection

from ubas_backend.database import create_query_engine
from ubas_backend.settings import Settings


def migrate_connection(connection: Connection) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
    config.attributes["connection"] = connection
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version") and inspector.has_table(
        "route_queries"
    ):
        if not inspector.has_table("schema_migrations"):
            raise RuntimeError("Unversioned route_queries table; cannot adopt safely")
        legacy = Table("schema_migrations", MetaData(), autoload_with=connection)
        applied = connection.scalar(
            select(legacy.c.version).where(legacy.c.version == "0001_route_queries.sql")
        )
        if applied is None:
            raise RuntimeError("Original queue migration was not recorded")
        command.stamp(config, "0001")
    command.upgrade(config, "head")


async def run() -> None:
    settings = Settings.from_environment(need_planner=False)
    engine = create_query_engine(settings.database_url)
    try:
        async with engine.begin() as connection:
            # PostgreSQL transaction advisory lock, expressed without textual SQL.
            # Serializes concurrent deploys, including initial version-table creation.
            await connection.execute(select(func.pg_advisory_xact_lock(846_271_503)))
            await connection.run_sync(migrate_connection)
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
