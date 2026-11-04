"""Stored payload compatibility and geographic validation before enqueue."""

import json
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from sqlalchemy import update
from sqlalchemy.orm import Session

from ubas_backend.main import create_app
from ubas_backend.query_manager.ports import QueryRepository
from ubas_backend.query_manager.schemas import SubmitQueryRequest
from ubas_backend.query_manager.service import QueryManager
from ubas_backend.routing.catalog_models import CityRow
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.demo import write_demo_catalog
from ubas_backend.routing.policy import InvalidRouteRequest, RequestPolicy
from ubas_backend.routing.schemas import PlanResult, RouteRequest
from ubas_backend.routing.storage import (
    SqliteCatalog,
    SqliteCityRepository,
    SqliteGeographyRepository,
)


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_original_committed_result_is_still_readable(self):
        payload = json.loads(
            (Path(__file__).parent / "fixtures" / "legacy-result.json").read_text(
                encoding="utf-8"
            )
        )
        result = PlanResult.model_validate({"schema_version": 1, **payload["result"]})
        self.assertEqual(result.schema_version, 1)
        self.assertEqual(len(result.routes), len(payload["result"]["routes"]))
        for actual, original in zip(
            result.routes, payload["result"]["routes"], strict=True
        ):
            self.assertEqual(actual.route.id, original["route"]["id"])
            self.assertEqual(
                str(actual.route.total_cost.amount),
                original["route"]["total_cost"]["amount"],
            )
            self.assertEqual(actual.score.total, original["score"]["total"])
            self.assertEqual(
                actual.route.stopover.tourism_score,
                original["route"]["stopover"]["tourism_score"],
            )

    def test_openapi_request_is_exactly_four_fields_and_booking_links_are_hidden(self):
        spec = create_app().openapi()
        schema = spec["components"]["schemas"]["RouteInput"]
        self.assertEqual(
            set(schema["properties"]), {"origin", "destination", "date_from", "date_to"}
        )
        self.assertFalse(schema["additionalProperties"])
        for name, definition in spec["components"]["schemas"].items():
            if name.startswith("TicketOffer") and not name.endswith("Input"):
                self.assertNotIn("booking_url", definition["properties"])

    async def test_old_catalog_scores_are_normalized_without_overwriting(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite3"
            write_demo_catalog(path)
            with (
                closing(SqliteCatalog(path)) as catalog,
                Session(catalog._engine) as session,
            ):
                row = session.get(CityRow, "ALA")
                data = json.loads(row.profile_json)
                data.pop("profile_version")
                data["tourism_score"] = 0.85
                session.execute(
                    update(CityRow)
                    .where(CityRow.city_code == "ALA")
                    .values(profile_json=json.dumps(data))
                )
                session.commit()
            original = path.read_bytes()
            with closing(SqliteCityRepository(path)) as cities:
                profiles = await cities.get_many(("ALA",))
                self.assertEqual(profiles[0].tourism_score, 85)
                self.assertEqual(profiles[0].profile_version, 2)
            with closing(SqliteGeographyRepository(path)) as geography:
                self.assertEqual(await geography.timezone("MOW"), "Europe/Moscow")
            self.assertEqual(path.read_bytes(), original)

    async def test_origin_local_yesterday_rejected_before_enqueue(self):
        repo = AsyncMock(spec=QueryRepository)
        geography = AsyncMock()
        geography.timezone.return_value = "Asia/Tokyo"
        at = datetime(2027, 6, 1, 20, tzinfo=UTC)
        request = RouteRequest(
            origin="TYO",
            destination="MOW",
            date_from=at.date(),
            date_to=at.date() + timedelta(days=1),
        )
        with patch("ubas_backend.routing.policy.datetime") as clock:
            clock.now.return_value = at
            manager = QueryManager(repo, RequestPolicy(PlannerConfig()), geography)
            with self.assertRaises(InvalidRouteRequest):
                await manager.submit_query(SubmitQueryRequest(request=request))
        repo.submit.assert_not_called()

    async def test_unknown_origin_rejected_before_enqueue(self):
        repo = AsyncMock(spec=QueryRepository)
        geography = AsyncMock()
        geography.timezone.return_value = None
        request = RouteRequest(
            origin="AAA",
            destination="ZZZ",
            date_from=datetime.now(UTC).date() + timedelta(days=3),
            date_to=datetime.now(UTC).date() + timedelta(days=4),
        )
        manager = QueryManager(repo, RequestPolicy(PlannerConfig()), geography)
        with self.assertRaises(InvalidRouteRequest):
            await manager.submit_query(SubmitQueryRequest(request=request))
        repo.submit.assert_not_called()
