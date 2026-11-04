"""Repositories are replaceable by ordinary constructor injection."""

import os
import unittest
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ubas_backend.routing.demo import (
    DemoFlightProvider,
    _demo_catalog_data,
    _demo_offers,
    write_demo_catalog,
)
from ubas_backend.routing.planner import RoutePlanner
from ubas_backend.routing.schemas import RouteRequest
from ubas_backend.routing.storage import SqliteCityRepository, SqliteTopologyRepository
from ubas_backend.runtime import create_planner
from ubas_backend.settings import Settings

NOW = datetime(2027, 5, 1, 12, tzinfo=UTC)
HUB = _demo_catalog_data(NOW).cities[0]
START = NOW.date() + timedelta(days=14)
REQUEST = RouteRequest(
    origin="MOW", destination="TYO", date_from=START, date_to=START + timedelta(days=3)
)


class TemporaryTopology:
    async def destinations(self, origins):
        return ("ALA",) if "MOW" in origins else ("TYO",) if "ALA" in origins else ()


class TemporaryCities:
    async def get_many(self, codes):
        return (HUB,) if "ALA" in codes else ()


class RepositoryWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_substitution_and_mixed_sources(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            write_demo_catalog(path, now=NOW)
            original = path.read_bytes()
            with (
                closing(SqliteTopologyRepository(path)) as topology,
                closing(SqliteCityRepository(path)) as cities,
            ):
                for selected_topology, selected_cities in (
                    (topology, cities),
                    (topology, TemporaryCities()),
                    (TemporaryTopology(), cities),
                    (TemporaryTopology(), TemporaryCities()),
                ):
                    result = await RoutePlanner(
                        DemoFlightProvider(_demo_offers(NOW)),
                        selected_topology,
                        selected_cities,
                    ).plan(REQUEST, now=NOW)
                    self.assertEqual(len(result.routes), 3)
                    self.assertTrue(result.complete)
            self.assertEqual(path.read_bytes(), original)

    async def test_startup_failure_closes_provider_without_creating_catalog(self):
        closed = []

        class Provider(DemoFlightProvider):
            async def aclose(self):
                closed.append(True)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            with patch.dict(
                os.environ,
                {
                    "UBAS_DATABASE_URL": "postgresql://unused",
                    "UBAS_FLIGHT_PROVIDER_FACTORY": "unused:factory",
                    "UBAS_CATALOG_PATH": str(path),
                },
                clear=True,
            ):
                settings = Settings.from_environment()
            with (
                patch("ubas_backend.runtime._load_provider", return_value=Provider()),
                self.assertRaisesRegex(RuntimeError, "does not exist"),
            ):
                async with AsyncExitStack() as resources:
                    create_planner(settings, resources)
            self.assertFalse(path.exists())
            self.assertEqual(closed, [True])
