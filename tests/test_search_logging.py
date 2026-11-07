"""Progress survives cancellation and remains isolated between searches."""

import asyncio
import json
import unittest
from unittest.mock import patch

from planner_fixtures import NOW, REQUEST, Cities, Geography, Provider, Topology

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.diagnostics import SearchDiagnostics, query_context
from ubas_backend.routing.offers import TicketSearch
from ubas_backend.routing.planner import IncompleteSearchError, RoutePlanner
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_query_does_not_block_next_query(self):
        slow = asyncio.Event()
        next_started = asyncio.Event()

        class Source:
            async def search(self, query):
                if query.origin == "AAA":
                    await slow.wait()
                if query.origin == "CCC":
                    next_started.set()
                return FlightSearchResult()

        queries = [
            FlightQuery(
                origin=origin,
                destination="ZZZ",
                departure_date=REQUEST.date_from,
                passengers=1,
                limit=10,
            )
            for origin in ("AAA", "BBB", "CCC")
        ]
        task = asyncio.create_task(
            TicketSearch(Source(), PlannerConfig(max_concurrent_queries=2)).collect(
                queries, REQUEST, SearchDiagnostics(), now=NOW
            )
        )
        try:
            await asyncio.wait_for(next_started.wait(), 1)
            self.assertFalse(task.done())
        finally:
            slow.set()
            await task

    async def test_timeout_logs_final_snapshot_and_stops_reporter(self):
        planner = RoutePlanner(
            Provider(()),
            Topology(),
            Cities(),
            geography=Geography(),
            config=PlannerConfig(search_timeout_seconds=0.01),
        )

        async def stalled(request, current, fixed_now, diagnostics):
            diagnostics.phase = "tickets"
            diagnostics.offers_received = 7
            await asyncio.sleep(60)

        token = query_context.set("test-query")
        try:
            with (
                patch.object(planner, "_plan", side_effect=stalled),
                self.assertLogs(
                    "ubas_backend.routing.diagnostics", level="INFO"
                ) as logs,
                self.assertRaises(IncompleteSearchError),
            ):
                await planner.plan(REQUEST, now=NOW)
            final = json.loads(
                logs.records[-1].getMessage().split("planner_progress ")[1]
            )
            self.assertEqual(final["outcome"], "timeout")
            self.assertEqual(final["query_id"], "test-query")
            self.assertEqual(final["offers_received"], 7)
            self.assertEqual(final["phase"], "tickets")
            self.assertFalse(
                any(
                    "report_progress" in task.get_coro().__qualname__
                    for task in asyncio.all_tasks()
                )
            )
        finally:
            query_context.reset(token)

    async def test_completed_response_counted_before_batch_cancellation(self):
        ready = asyncio.Event()

        class Source:
            async def search(self, query):
                if query.origin == "AAA":
                    ready.set()
                    return FlightSearchResult(complete=False)
                await asyncio.sleep(60)

        diagnostics = SearchDiagnostics()
        search = TicketSearch(Source(), PlannerConfig())
        queries = [
            FlightQuery(
                origin=origin,
                destination="ZZZ",
                departure_date=REQUEST.date_from,
                passengers=1,
                limit=10,
            )
            for origin in ("AAA", "BBB")
        ]
        task = asyncio.create_task(
            search.collect(queries, REQUEST, diagnostics, now=NOW)
        )
        await ready.wait()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(diagnostics.queries_started, 2)
        self.assertEqual(diagnostics.queries_finished, 1)
        self.assertEqual(diagnostics.queries_incomplete, 1)
        self.assertEqual(diagnostics.queries_cancelled, 1)
        self.assertEqual(SearchDiagnostics().queries_started, 0)
