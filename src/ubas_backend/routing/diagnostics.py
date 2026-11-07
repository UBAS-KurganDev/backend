"""Per-search diagnostics owned by one planner invocation."""

import asyncio
import json
import logging
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

query_context: ContextVar[str | None] = ContextVar("query_id", default=None)
logger = logging.getLogger(__name__)


@dataclass
class SearchDiagnostics:
    rejected: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True
    evaluated: int = 0
    search_id: str = field(default_factory=lambda: uuid4().hex)
    started_at: float = field(default_factory=monotonic)
    phase: str = "preparation"
    queries_planned: int = 0
    queries_started: int = 0
    queries_finished: int = 0
    queries_failed: int = 0
    queries_cancelled: int = 0
    queries_incomplete: int = 0
    offers_received: int = 0
    offers_accepted: int = 0
    search_nodes: int = 0
    routes_valid: int = 0

    def log(self, outcome: str) -> None:
        logger.info(
            "planner_progress %s",
            json.dumps(
                {
                    "search_id": self.search_id,
                    "query_id": query_context.get(),
                    "outcome": outcome,
                    "phase": self.phase,
                    "elapsed_seconds": round(monotonic() - self.started_at, 3),
                    "queries_planned": self.queries_planned,
                    "queries_started": self.queries_started,
                    "queries_finished": self.queries_finished,
                    "queries_failed": self.queries_failed,
                    "queries_cancelled": self.queries_cancelled,
                    "queries_incomplete": self.queries_incomplete,
                    "queries_active": self.queries_started
                    - self.queries_finished
                    - self.queries_cancelled,
                    "offers_received": self.offers_received,
                    "offers_accepted": self.offers_accepted,
                    "search_nodes": self.search_nodes,
                    "evaluated_candidates": self.evaluated,
                    "routes_valid": self.routes_valid,
                    "rejected": dict(self.rejected),
                    "warnings": dict(Counter(self.warnings)),
                }
            ),
        )

    async def report_progress(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.log("running")

    def partial(self, message: str) -> None:
        self.complete = False
        self.warnings.append(message)
