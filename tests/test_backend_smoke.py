"""Smoke runner lifecycle with a real HTTP server and controlled API responses.

These checks do not claim PostgreSQL or live ticket-source integration.
"""

import importlib
import io
import json
import socket
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse


class BackendSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        sys.path.insert(0, scripts)
        try:
            cls.runner = importlib.import_module("run_backend_query")
        finally:
            sys.path.remove(scripts)

    def exercise(self, status):
        app = FastAPI()
        query_id = str(uuid4())
        submitted = []

        @app.post("/api/v1/queries", status_code=202)
        async def submit(body: dict):
            submitted.append(body)
            return {"query_id": query_id}

        @app.get("/api/v1/queries/{identifier}")
        async def metadata(identifier: str):
            self.assertEqual(identifier, query_id)
            return {"status": status}

        @app.get("/api/v1/queries/{identifier}/result")
        async def result(identifier: str):
            if status == "failed":
                return JSONResponse({"detail": {"code": "search_incomplete"}}, 409)
            return {"query_id": identifier, "result": {"routes": []}}

        real_config = uvicorn.Config
        with patch.object(sys, "argv", ["smoke", "--timeout", "0.3"]):
            arguments = self.runner.parse_arguments()
        arguments.poll_interval = 0.01
        output = io.StringIO()
        with (
            socket.socket() as listener,
            redirect_stdout(output),
            patch.object(
                self.runner.uvicorn,
                "Config",
                side_effect=lambda *a, **kw: real_config(app, **kw),
            ),
        ):
            listener.bind(("127.0.0.1", 0))
            try:
                if status == "completed":
                    self.assertEqual(
                        self.runner.serve_and_query(arguments, listener), 0
                    )
                elif status == "failed":
                    with self.assertRaises(RuntimeError):
                        self.runner.serve_and_query(arguments, listener)
                else:
                    with self.assertRaises(TimeoutError):
                        self.runner.serve_and_query(arguments, listener)
            finally:
                self.assertFalse(
                    any(t.name == "ubas-smoke-api" for t in threading.enumerate())
                )
        self.assertEqual(submitted[0]["origin"], "MOW")
        if status != "running":
            return json.loads(output.getvalue())
        return None

    def test_success_prints_json_and_stops_server(self):
        self.assertEqual(self.exercise("completed")["result"], {"routes": []})

    def test_failure_prints_reason_and_stops_server(self):
        self.assertEqual(self.exercise("failed")["detail"]["code"], "search_incomplete")

    def test_timeout_stops_server(self):
        self.exercise("running")

    def test_missing_database_configuration_fails_before_start(self):
        with patch.object(sys, "argv", ["smoke", "--demo"]):
            arguments = self.runner.parse_arguments()
        with (
            patch.dict(self.runner.os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "UBAS_DATABASE_URL"),
        ):
            self.runner.run(arguments)
