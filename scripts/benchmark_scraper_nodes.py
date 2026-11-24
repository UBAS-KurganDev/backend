"""Measure one scraper node versus two nodes on the same provider code path.

Simulated mode (default) starts two local node servers with a fixed per-request
latency, so the numbers show dispatch and concurrency rather than agent.ru jitter.
Live mode targets already running nodes and uses different dates per scenario to
avoid their caches.
"""

import argparse
import asyncio
import socket
import statistics
import sys
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import uvicorn

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.node_api import create_node_app
from ubas_backend.agent_scraper.nodes import NodeSearchService
from ubas_backend.agent_scraper.provider import AgentFlightProvider
from ubas_backend.routing.schemas import FlightQuery


class SimulatedNode:
    """Fixed latency and a browser-slot limit, without Chromium or the real source."""

    def __init__(self, delay: float, concurrency: int) -> None:
        self._delay = delay
        self._slots = asyncio.Semaphore(concurrency)
        self.calls = 0

    async def get_offers(self, request: SearchRequest) -> CapturedOffers:
        del request
        async with self._slots:
            self.calls += 1
            await asyncio.sleep(self._delay)
            return CapturedOffers(
                fetched_at=datetime.now(UTC),
                data={
                    "status": "Ready",
                    "currency": "RUB",
                    "offers": [],
                    "ow_offers": [],
                },
            )

    async def aclose(self) -> None:
        pass


def positive_seconds(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def node_urls(value: str) -> tuple[str, ...]:
    urls = tuple(item.strip().rstrip("/") for item in value.split(",") if item.strip())
    if not urls:
        raise argparse.ArgumentTypeError("нужен хотя бы один URL узла")
    return urls


async def start_node(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            return server, task
        if task.done():
            await task
            raise RuntimeError("локальный узел не запустился")
        await asyncio.sleep(0.01)
    raise RuntimeError("локальный узел не запустился за 5 секунд")


async def stop_node(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    server.should_exit = True
    await task


async def measure(
    urls: tuple[str, ...],
    concurrency: int,
    day: date,
    *,
    timeout: float,
) -> float:
    settings = Settings(
        redis_url=None,
        node_urls=urls,
        node_concurrency=concurrency,
        node_timeout_seconds=timeout,
        node_max_attempts=1,
    )
    provider = AgentFlightProvider(NodeSearchService(settings))
    started = time.perf_counter()
    try:
        await provider.search(
            FlightQuery(
                origin="MOW",
                destination="TYO",
                departure_date=day,
                passengers=1,
                currency="RUB",
                limit=200,
            )
        )
    finally:
        await provider.aclose()
    return time.perf_counter() - started


def summary(values: list[float]) -> str:
    return (
        f"median={statistics.median(values):.3f}s "
        f"min={min(values):.3f}s max={max(values):.3f}s"
    )


def speedup(single: list[float], dual: list[float]) -> str:
    ratio = statistics.median(single) / statistics.median(dual)
    return f"{ratio:.2f}x"


async def simulated(arguments: argparse.Namespace) -> int:
    day = datetime.now(UTC).date() + timedelta(days=14)
    ports = (free_port(), free_port())
    nodes = (
        SimulatedNode(arguments.delay, arguments.concurrency),
        SimulatedNode(arguments.delay, arguments.concurrency),
    )
    servers = [
        await start_node(create_node_app(Settings(), node), port)
        for node, port in zip(nodes, ports, strict=True)
    ]
    urls = tuple(f"http://127.0.0.1:{port}" for port in ports)
    print(
        f"СИМУЛЯЦИЯ: задержка {arguments.delay:.2f}s на запрос, "
        f"{arguments.concurrency} слотов на узел, MOW -> TYO (8 пар аэропортов).",
        file=sys.stderr,
    )
    try:
        single: list[float] = []
        dual: list[float] = []
        for _ in range(arguments.runs):
            single.append(
                await measure(
                    urls[:1],
                    arguments.concurrency,
                    day,
                    timeout=arguments.timeout,
                )
            )
            dual.append(
                await measure(
                    urls,
                    arguments.concurrency,
                    day,
                    timeout=arguments.timeout,
                )
            )
    finally:
        for server, task in servers:
            await stop_node(server, task)
    print(f"1 узел:  {summary(single)}")
    print(f"2 узла:  {summary(dual)}")
    print("Ускорение по медиане:", speedup(single, dual))
    print(f"Вызовов узлов за прогон: {nodes[0].calls + nodes[1].calls}")
    return 0


async def live(arguments: argparse.Namespace) -> int:
    first = datetime.now(UTC).date() + timedelta(days=14)
    start = arguments.date or first
    single: list[float] = []
    dual: list[float] = []
    print(
        "ЖИВОЙ РЕЖИМ: используются запущенные узлы; даты сценариев различаются, "
        "чтобы не сравнивать кэш с сетью.",
        file=sys.stderr,
    )
    for run in range(arguments.runs):
        baseline_day = start + timedelta(days=run * 2)
        distributed_day = baseline_day + timedelta(days=1)
        single.append(
            await measure(
                arguments.nodes[:1],
                arguments.concurrency,
                baseline_day,
                timeout=arguments.timeout,
            )
        )
        dual.append(
            await measure(
                arguments.nodes,
                arguments.concurrency,
                distributed_day,
                timeout=arguments.timeout,
            )
        )
    print(f"1 узел:  {summary(single)}")
    print(f"2 узла:  {summary(dual)}")
    print("Ускорение по медиане:", speedup(single, dual))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nodes",
        type=node_urls,
        help="URLs запущенных узлов через запятую; без флага включается симуляция",
    )
    parser.add_argument("--delay", type=positive_seconds, default=0.5)
    parser.add_argument("--concurrency", type=positive_int, default=2)
    parser.add_argument("--runs", type=positive_int, default=3)
    parser.add_argument("--timeout", type=positive_seconds, default=60)
    parser.add_argument("--date", type=date.fromisoformat)
    arguments = parser.parse_args()
    runner = live if arguments.nodes else simulated
    raise SystemExit(asyncio.run(runner(arguments)))


if __name__ == "__main__":
    main()
