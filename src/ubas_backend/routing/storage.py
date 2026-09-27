"""Small SQLite catalog; replace with another implementation of the same ports."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from ubas_backend.routing.models import CityProfile, Code, Model


class FlightLink(Model):
    origin: Code
    destination: Code


class CatalogData(Model):
    cities: tuple[CityProfile, ...]
    links: tuple[FlightLink, ...]


class SqliteCatalog:
    """One connection per operation; async reads do not block the event loop."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS city_profiles (
                    city_code TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flight_links (
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    PRIMARY KEY (origin, destination),
                    CHECK (origin <> destination)
                );
                CREATE INDEX IF NOT EXISTS flight_links_destination
                    ON flight_links (destination, origin);
                PRAGMA user_version = 1;
                """
            )

    def upsert(self, data: CatalogData) -> None:
        """Import verified catalog records at startup or from an offline importer."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany(
                """INSERT INTO city_profiles (city_code, profile_json) VALUES (?, ?)
                   ON CONFLICT(city_code) DO UPDATE
                   SET profile_json = excluded.profile_json""",
                [(city.city_code, city.model_dump_json()) for city in data.cities],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO flight_links (origin, destination) "
                "VALUES (?, ?)",
                [(link.origin, link.destination) for link in data.links],
            )

    async def connecting_cities(self, origin: str, destination: str) -> tuple[str, ...]:
        def read() -> tuple[str, ...]:
            with closing(sqlite3.connect(self.path)) as connection:
                rows = connection.execute(
                    """SELECT outgoing.destination
                       FROM flight_links AS outgoing
                       JOIN flight_links AS incoming
                         ON incoming.origin = outgoing.destination
                       WHERE outgoing.origin = ? AND incoming.destination = ?
                       ORDER BY outgoing.destination""",
                    (origin, destination),
                ).fetchall()
                return tuple(str(row[0]) for row in rows)

        return await asyncio.to_thread(read)

    async def get_many(self, city_codes: tuple[str, ...]) -> tuple[CityProfile, ...]:
        def read() -> tuple[CityProfile, ...]:
            result: list[CityProfile] = []
            with closing(sqlite3.connect(self.path)) as connection:
                for offset in range(0, len(city_codes), 500):
                    batch = city_codes[offset : offset + 500]
                    placeholders = ",".join("?" for _ in batch)
                    rows = connection.execute(
                        "SELECT profile_json FROM city_profiles "
                        f"WHERE city_code IN ({placeholders}) ORDER BY city_code",
                        batch,
                    ).fetchall()
                    result.extend(
                        CityProfile.model_validate_json(row[0]) for row in rows
                    )
            return tuple(result)

        return await asyncio.to_thread(read)
