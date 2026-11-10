"""Optional file paths passed explicitly by Docker Compose."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ubas_backend.agent_scraper.config import Settings as AgentSettings
from ubas_backend.routing.config import PlannerConfig
from ubas_backend.settings import load_planner_config


class OptionalPathTests(unittest.TestCase):
    def test_missing_and_empty_paths_use_defaults(self):
        for env in (
            {},
            {"UBAS_PLANNER_CONFIG_PATH": "", "UBAS_AGENT_TOPOLOGY_SNAPSHOT_PATH": ""},
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(load_planner_config(), PlannerConfig())
                self.assertIsNone(AgentSettings().topology_snapshot_path)

    def test_configured_planner_file_is_read(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "planner.json"
            path.write_text('{"query_timeout_seconds": 150}', encoding="utf-8")
            with patch.dict(
                os.environ, {"UBAS_PLANNER_CONFIG_PATH": str(path)}, clear=True
            ):
                self.assertEqual(load_planner_config().query_timeout_seconds, 150)

    def test_missing_configured_planner_file_is_an_error(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"UBAS_PLANNER_CONFIG_PATH": str(Path(directory) / "missing.json")},
                clear=True,
            ),
            self.assertRaises(FileNotFoundError),
        ):
            load_planner_config()

    def test_configured_snapshot_path_is_preserved(self):
        with patch.dict(
            os.environ,
            {"UBAS_AGENT_TOPOLOGY_SNAPSHOT_PATH": "./var/topology/airports.json"},
            clear=True,
        ):
            self.assertEqual(
                AgentSettings().topology_snapshot_path,
                Path("./var/topology/airports.json"),
            )
