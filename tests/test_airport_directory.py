"""Expanded city coverage is shared by request expansion and ticket decoding."""

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_agent_scraper import DAY, NOW, empty, one_ticket, query, raw_ticket, request

from ubas_backend.agent_scraper.airports import AirportDirectory, default_directory
from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.provider import (
    AgentFlightProvider,
    create_flight_provider,
)
from ubas_backend.routing.schemas import Airport
from ubas_backend.topology.models import City, Observation, ReferenceAirport, Snapshot
from ubas_backend.topology.snapshots import combine_snapshots


def city_snapshot(city="LON", codes=("LHR", "LGW")):
    now = datetime.now(UTC)
    return Snapshot(
        started_at=now,
        finished_at=now,
        covered_cities=(city,),
        cities=(
            City(code=city, name=city, country_code="GB", timezone="Europe/London"),
        ),
        airports=tuple(
            ReferenceAirport(
                code=code, city_code=city, timezone="Europe/London", flightable=True
            )
            for code in codes
        ),
        observations=tuple(
            Observation(origin=code, destinations=(), fetched_at=now) for code in codes
        ),
    )


class AirportDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_directory_expands_new_city_and_decodes_transfer(self):
        directory = AirportDirectory(
            (
                Airport(code="LHR", city_code="LON", timezone="Europe/London"),
                Airport(code="LGW", city_code="LON", timezone="Europe/London"),
            ),
            frozenset({"LHR", "LGW"}),
            fallback=default_directory(),
        )
        service = SimpleNamespace(get_offers=AsyncMock(return_value=empty()))
        with patch("ubas_backend.agent_scraper.provider.datetime") as clock:
            clock.now.return_value = NOW
            result = await AgentFlightProvider(service, airports=directory).search(
                query(origin="LON")
            )
        self.assertTrue(result.complete)
        self.assertEqual(
            {call.args[0].origin for call in service.get_offers.call_args_list},
            {"LHR", "LGW"},
        )
        value = one_ticket()
        first, last = raw_ticket(value)["flights_info"]
        first.update(
            arrival_airport="LHR",
            departure_date=DAY.isoformat(),
            arrival_date=DAY.isoformat(),
            departure_local_time="06:00",
            arrival_local_time="10:00",
        )
        last.update(
            departure_airport="LHR",
            departure_date=DAY.isoformat(),
            arrival_date=DAY.isoformat(),
            departure_local_time="12:00",
            arrival_local_time="16:00",
        )
        self.assertFalse(map_response(value, request()).complete)
        result = map_response(
            value, request(), airport_lookup=directory.airport_for_code
        )
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual(result.offers[0].segments[0].destination.city_code, "LON")

    async def test_factory_loads_snapshot_before_constructing_service(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            path.write_text(city_snapshot().model_dump_json(), encoding="utf-8")
            service = SimpleNamespace(
                get_offers=AsyncMock(return_value=empty()), aclose=AsyncMock()
            )
            with (
                patch.dict(
                    os.environ,
                    {"UBAS_AGENT_TOPOLOGY_SNAPSHOT_PATH": str(path)},
                    clear=True,
                ),
                patch(
                    "ubas_backend.agent_scraper.provider.FlightSearchService",
                    return_value=service,
                ) as constructor,
                patch("ubas_backend.agent_scraper.provider.datetime") as clock,
            ):
                clock.now.return_value = NOW
                provider = create_flight_provider()
                result = await provider.search(query(origin="LON"))
                self.assertTrue(result.complete)
                await provider.aclose()
                service.aclose.assert_awaited_once()
                constructor.reset_mock()
                path.write_text("{}", encoding="utf-8")
                with self.assertRaises(ValueError):
                    create_flight_provider()
                constructor.assert_not_called()

    def test_transit_only_airports_do_not_imply_city_search_coverage(self):
        directory = AirportDirectory(
            (Airport(code="LHR", city_code="LON", timezone="Europe/London"),),
            frozenset(),
        )
        self.assertIsNotNone(directory.airport_for_code("LHR"))
        self.assertEqual(directory.airports_for_city("LON"), ())
        with self.assertRaises(ValueError):
            AirportDirectory((), frozenset({"LHR"}))


class SnapshotCombinationTests(unittest.TestCase):
    def test_disjoint_coverage_and_original_timestamps(self):
        first, second = city_snapshot(), city_snapshot("SIN", ("SIN",))
        result = combine_snapshots((first, second))
        self.assertEqual(result.covered_cities, ("LON", "SIN"))
        self.assertEqual(result.started_at, first.started_at)
        self.assertEqual(result.finished_at, second.finished_at)
        self.assertEqual(len(result.observations), 3)
        self.assertEqual(result.observations[0].fetched_at, first.started_at)

    def test_overlap_conflicting_geography_and_provenance_are_rejected(self):
        first = city_snapshot()
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            combine_snapshots((first, first))
        second = city_snapshot("SIN", ("SIN",))
        raw = second.model_dump(mode="json")
        raw["cities"].append({**first.cities[0].model_dump(), "name": "Changed"})
        with self.assertRaisesRegex(ValueError, "Conflicting city"):
            combine_snapshots((first, Snapshot.model_validate(raw)))
        with self.assertRaisesRegex(ValueError, "provenance"):
            combine_snapshots(
                (first, second.model_copy(update={"attribution": "Other"}))
            )

    def test_city_manifest_matches_collected_scope(self):
        root = Path(__file__).resolve().parents[1]
        ready = json.loads((root / "config/topology-cities.json").read_text("utf-8"))
        pending = json.loads(
            (root / "config/topology-pending-cities.json").read_text("utf-8")
        )
        self.assertEqual(len(ready), len(set(ready)))
        self.assertFalse(set(ready).intersection(pending))
