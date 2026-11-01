"""Demo preparation stays reproducible as time passes without changing real data."""

import subprocess
import sys
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ubas_backend.routing.demo import create_flight_provider, write_demo_catalog
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.schemas import RouteRequest
from ubas_backend.routing.storage import SqliteCatalog


class DemoCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def plan(self, path, now):
        start = now.date() + timedelta(days=14)
        request = RouteRequest(
            origin="MOW",
            destination="TYO",
            date_from=start,
            date_to=start + timedelta(days=3),
        )
        with (
            closing(SqliteCatalog(path)) as catalog,
            patch("ubas_backend.routing.demo.datetime") as clock,
        ):
            clock.now.return_value = now
            return await RoutePlanner(create_flight_provider(), catalog, catalog).plan(
                request, now=now
            )

    async def test_generated_catalog_works_after_original_snapshot_expiry(self):
        for year in (2027, 2030):
            with self.subTest(year=year), TemporaryDirectory() as directory:
                now = datetime(year, 2, 1, 12, tzinfo=UTC)
                path = Path(directory) / "nested" / "demo.sqlite3"
                write_demo_catalog(path, now=now)
                result = await self.plan(path, now)
                self.assertTrue(result.complete)
                self.assertEqual(len(result.routes), 2)
                self.assertEqual(result.routes[0].route.visa.verified_at, now.date())
                self.assertEqual(
                    result.routes[0].route.visa.valid_until,
                    (now + timedelta(days=90)).date(),
                )
                self.assertIn("synthetic_demo_data", result.warnings)

    async def test_expired_catalog_requires_explicit_regeneration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            created = datetime(2026, 10, 23, 12, tzinfo=UTC)
            later = datetime(2027, 1, 8, 12, tzinfo=UTC)
            write_demo_catalog(path, now=created)
            original = path.read_bytes()
            expired = await self.plan(path, later)
            self.assertEqual(expired.routes, ())
            self.assertEqual(expired.rejected, {"city_visa": 1})
            self.assertEqual(path.read_bytes(), original)
            write_demo_catalog(path, now=later, overwrite=True)
            refreshed = await self.plan(path, later)
            self.assertEqual(len(refreshed.routes), 2)
            self.assertEqual(refreshed.routes[0].route.visa.verified_at, later.date())

    def test_existing_file_is_preserved_without_overwrite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            original = b"An existing catalog must never be silently replaced"
            path.write_bytes(original)
            with self.assertRaises(FileExistsError):
                write_demo_catalog(path)
            self.assertEqual(path.read_bytes(), original)

    def test_cli_creates_catalog_and_requires_overwrite_on_second_run(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            command = [
                sys.executable,
                "-m",
                "ubas_backend.routing.demo",
                "--catalog",
                str(path),
            ]
            for arguments, expected in (([], 0), ([], 1), (["--overwrite"], 0)):
                result = subprocess.run(
                    command + arguments, capture_output=True, text=True, timeout=15
                )
                self.assertEqual(result.returncode, expected, result.stderr)
            self.assertTrue(path.is_file())
