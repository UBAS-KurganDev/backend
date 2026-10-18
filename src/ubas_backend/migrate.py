"""Apply ordered SQL migrations from the repository's migrations directory."""

import asyncio
from pathlib import Path

import asyncpg

from ubas_backend.settings import Settings


async def run() -> None:
    settings = Settings.from_environment(need_planner=False)
    migrations_path = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(migrations_path.glob("*.sql"))
    if not files:
        raise RuntimeError("No SQL migrations were found")
    connection = await asyncpg.connect(settings.database_url)
    try:
        await connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
        for migration in files:
            version = migration.name
            already_applied = await connection.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version = $1", version
            )
            if already_applied:
                continue
            async with connection.transaction():
                await connection.execute(migration.read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", version
                )
    finally:
        await connection.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
