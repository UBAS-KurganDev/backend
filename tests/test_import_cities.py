"""Offline regressions for tourism collection, refreshes and secrets."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from tests.factories import city_profile
from ubas_backend import import_cities as importer


class ImporterTests(unittest.TestCase):
    def test_fixed_scores_do_not_depend_on_other_cities(self):
        counts = {"AAA": importer.SightCounts(200, 10)}
        before = importer.tourism_scores(counts)["AAA"]
        counts["BBB"] = importer.SightCounts(10000, 0)
        self.assertEqual(importer.tourism_scores(counts)["AAA"], before)
        self.assertGreater(before, 60)
        self.assertEqual(
            importer.tourism_scores({"AAA": importer.SightCounts(0, 0)}), {"AAA": 0}
        )
        self.assertEqual(importer.tourism_scores({}), {})
        with self.assertRaises(importer.OpenTripMapError):
            importer.tourism_scores({"AAA": importer.SightCounts(-1, 0)})

    def test_refresh_overrides_old_counts_preserves_other_timestamp(self):
        old_time = datetime(2026, 1, 1, tzinfo=UTC)
        previous = {
            code: importer.CityRecord(
                city_profile(code), importer.SightCounts(1, 0), 100, old_time
            )
            for code in ("PAR", "ROM")
        }
        records = importer.assemble_records(
            (
                importer.CitySpec("PAR", "Paris", "FR"),
                importer.CitySpec("ROM", "Rome", "IT"),
            ),
            {
                "PAR": (
                    importer.CityLocation(0, 0, "Europe/Paris", 200),
                    importer.SightCounts(999, 0),
                )
            },
            previous,
        )
        self.assertEqual(records[0].counts.notable, 999)
        self.assertGreater(records[0].profile.tourism_score, 60)
        self.assertEqual(records[1].fetched_at, old_time)

    def test_snapshots_legacy_recalibration_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            record = importer.CityRecord(
                city_profile("PAR", tourism_score=0), importer.SightCounts(300, 0), None
            )
            importer.write_snapshot(path, (record,))
            restored = importer.read_snapshot(path)[0]
            self.assertGreater(restored.profile.tourism_score, 60)
            self.assertIsNone(restored.fetched_at)
            original = path.read_bytes()
            with (
                patch.object(importer.os, "replace", side_effect=OSError("disk")),
                self.assertRaises(OSError),
            ):
                importer.write_snapshot(path, (record,))
            self.assertEqual(path.read_bytes(), original)
            legacy = json.loads(original)
            legacy.pop("snapshot_version")
            legacy["cities"][0]["profile"].pop("profile_version")
            legacy["cities"][0]["profile"]["tourism_score"] = 0.01
            path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(
                importer.read_snapshot(path)[0].profile.tourism_score,
                restored.profile.tourism_score,
            )

    def test_retry_failure_does_not_expose_key(self):
        with httpx.Client(
            base_url=importer.API_BASE_URL,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, request=request)
            ),
        ) as http:
            client = importer.OpenTripMapClient(http, "DUMMY_TEST_SECRET")
            with (
                patch.object(importer, "REQUEST_ATTEMPTS", 1),
                self.assertRaises(importer.OpenTripMapError) as caught,
            ):
                client.city_location("Paris", "FR")
            self.assertNotIn("DUMMY_TEST_SECRET", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_pagination_limit_is_not_a_successful_count(self):
        with httpx.Client(
            base_url=importer.API_BASE_URL,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=[{"rate": 3}])
            ),
        ) as http:
            client = importer.OpenTripMapClient(http, "dummy")
            with (
                patch.object(importer, "MAX_PAGES_PER_CITY", 1),
                patch.object(importer, "PAGE_SIZE", 1),
                self.assertRaises(importer.OpenTripMapError),
            ):
                client.sight_counts(0, 0)

    def test_total_source_failure_leaves_snapshot_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            record = importer.CityRecord(
                city_profile("PAR"), importer.SightCounts(300, 0), None
            )
            importer.write_snapshot(path, (record,))
            previous = path.read_bytes()
            args = importer.parse_args(
                ["--api-key", "dummy", "--city", "PAR", "--snapshot", str(path)]
            )
            with patch.object(importer, "fetch_cities", return_value=({}, ("PAR",))):
                self.assertEqual(importer.run(args), 1)
            self.assertEqual(path.read_bytes(), previous)
