"""Tourism imports preserve catalog data and work with existing readers."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.factories import city_profile, visa_policy
from ubas_backend.routing.catalog_models import CityRow
from ubas_backend.routing.schemas import Money
from ubas_backend.routing.storage import (
    CatalogData,
    FlightLink,
    SqliteCatalog,
    SqliteCityRepository,
    SqliteGeographyRepository,
    SqliteTopologyRepository,
)


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_imported_score_selects_hub_and_allows_long_stop(self):
        from planner_fixtures import NOW, REQUEST, NoVisas, Provider, offer, segment

        from ubas_backend.import_cities import SightCounts, tourism_scores
        from ubas_backend.routing.config import PlannerConfig
        from ubas_backend.routing.planner import RoutePlanner

        profiles = tuple(
            city_profile(c, timezone="UTC", tourism_score=0)
            for c in ("AAA", "BBB", "CCC", "ZZZ")
        )
        self.catalog.upsert(
            CatalogData(
                cities=profiles,
                links=tuple(
                    FlightLink(origin=a, destination=b)
                    for a, b in (
                        ("AAA", "BBB"),
                        ("BBB", "ZZZ"),
                        ("AAA", "CCC"),
                        ("CCC", "ZZZ"),
                    )
                ),
            )
        )
        topology = SqliteTopologyRepository(self.path)
        geography = SqliteGeographyRepository(self.path)
        self.addCleanup(topology.close)
        self.addCleanup(geography.close)
        provider = Provider(
            (
                offer("ab", [segment("AAA", "BBB", 0)], 10),
                offer("bz", [segment("BBB", "ZZZ", 18)], 20),
            )
        )
        planner = RoutePlanner(
            provider,
            topology,
            self.cities,
            config=PlannerConfig(max_hubs=1),
            geography=geography,
            visa_costs=NoVisas(),
        )
        before = await planner.plan(REQUEST, now=NOW)
        self.assertFalse(before.routes)
        score = tourism_scores({"BBB": SightCounts(200, 0)})["BBB"]
        self.catalog.update_tourism((city_profile("BBB", tourism_score=score),))
        after = await planner.plan(REQUEST, now=NOW)
        self.assertEqual(after.search_scope.selected_hubs, ("BBB",))
        self.assertTrue(after.routes)
        self.assertEqual(after.routes[0].route.total_cost.amount, 30)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "catalog.sqlite3"
        self.catalog = SqliteCatalog(self.path)
        self.addCleanup(self.catalog.close)
        self.catalog.initialize()
        self.cities = SqliteCityRepository(self.path)
        self.addCleanup(self.cities.close)

    async def test_import_preserves_visas_geography_and_topology(self):
        original = city_profile(
            "PAR", visas=(visa_policy(),), hotel_per_night=Money(amount=Decimal("5000"))
        )
        self.catalog.upsert(
            CatalogData(
                cities=(original,), links=(FlightLink(origin="PAR", destination="ROM"),)
            )
        )
        incoming = city_profile(
            "PAR",
            name="Replacement",
            timezone="UTC",
            country_code="GB",
            tourism_score=80,
        )
        self.catalog.update_tourism((incoming, city_profile("ROM", tourism_score=95)))
        result = await self.cities.get_many(("PAR",))
        self.assertEqual(result[0], original.model_copy(update={"tourism_score": 80}))
        geography = SqliteGeographyRepository(self.path)
        topology = SqliteTopologyRepository(self.path)
        self.addCleanup(geography.close)
        self.addCleanup(topology.close)
        self.assertEqual(await geography.timezone("PAR"), "Europe/Paris")
        self.assertEqual(await topology.destinations(("PAR",)), ("ROM",))
        listed = await self.cities.list_by_tourism(limit=1, min_tourism_score=60)
        self.assertEqual(listed[0].city_code, "ROM")

    async def test_legacy_profiles_normalize_in_listing_without_rewrite(self):
        legacy = city_profile("PAR").model_dump(mode="json")
        del legacy["profile_version"]
        legacy["tourism_score"] = 0.9
        raw = json.dumps(legacy)
        engine = create_engine(f"sqlite:///{self.path}")
        self.addCleanup(engine.dispose)
        with Session(engine) as session, session.begin():
            session.add(CityRow(city_code="PAR", profile_json=raw))
        self.assertEqual(
            (await self.cities.list_by_tourism(min_tourism_score=60))[0].tourism_score,
            90,
        )
        with Session(engine) as session:
            self.assertEqual(session.scalar(select(CityRow.profile_json)), raw)
        self.catalog.update_tourism((city_profile("PAR", tourism_score=75),))
        self.assertEqual((await self.cities.get_many(("PAR",)))[0].tourism_score, 75)

    async def test_batch_rolls_back_on_failure(self):
        self.catalog.upsert(
            CatalogData(cities=(city_profile("PAR", tourism_score=40),), links=())
        )
        broken = city_profile("ROM").model_copy(update={"city_code": None})
        with self.assertRaises(IntegrityError):
            self.catalog.update_tourism((city_profile("PAR", tourism_score=80), broken))
        self.assertEqual((await self.cities.get_many(("PAR",)))[0].tourism_score, 40)
