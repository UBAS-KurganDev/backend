"""City API uses read-only repository and the MVP 0..100 scale."""

import tempfile
import unittest
from pathlib import Path

import httpx

from tests.conftest import SEEDED_SCORES
from tests.factories import city_profile
from ubas_backend.main import create_app
from ubas_backend.routing.storage import (
    CatalogData,
    SqliteCatalog,
    SqliteCityRepository,
)


class CitiesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "catalog.sqlite3"
        catalog = SqliteCatalog(path)
        catalog.initialize()
        catalog.upsert(
            CatalogData(
                cities=tuple(
                    city_profile(code, tourism_score=score)
                    for code, score in SEEDED_SCORES.items()
                ),
                links=(),
            )
        )
        catalog.close()
        cities = SqliteCityRepository(path)
        self.addCleanup(cities.close)
        self.app = create_app()
        self.app.state.cities = cities
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_listing_filter_order_limit_and_scale(self):
        response = await self.client.get("/api/v1/cities?min_tourism_score=60")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c["city_code"] for c in response.json()], ["PAR", "ROM"])
        limited = await self.client.get("/api/v1/cities?limit=1")
        self.assertEqual(len(limited.json()), 1)
        for query in (
            "limit=0",
            "limit=501",
            "min_tourism_score=101",
            "min_tourism_score=-1",
        ):
            self.assertEqual(
                (await self.client.get(f"/api/v1/cities?{query}")).status_code, 422
            )

    async def test_detail_and_missing_source(self):
        response = await self.client.get("/api/v1/cities/PAR")
        self.assertEqual(response.json()["tourism_score"], 90)
        self.assertEqual(response.json()["profile_version"], 2)
        self.assertEqual((await self.client.get("/api/v1/cities/XXX")).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/cities/par")).status_code, 422)
        self.app.state.cities = None
        self.assertEqual((await self.client.get("/api/v1/cities")).status_code, 503)
