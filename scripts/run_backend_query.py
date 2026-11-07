"""Start the backend, submit a route query, print JSON and stop the server.

Run from the checkout with uv run --env-file .env python scripts/run_backend_query.py.
PostgreSQL must already be available. Use --demo for synthetic tickets/catalog.
"""

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from query_route import positive_seconds, submit_query, wait_for_result

from ubas_backend import migrate
from ubas_backend.routing.demo import write_demo_catalog
from ubas_backend.settings import Settings


def parse_arguments() -> argparse.Namespace:
    from datetime import date

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo", action="store_true", help="Вымышленные билеты и каталог"
    )
    parser.add_argument("--origin", default="MOW")
    parser.add_argument("--destination", default="TYO")
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument(
        "--port", type=int, default=0, help="0 — свободный локальный порт"
    )
    parser.add_argument(
        "--timeout",
        type=positive_seconds,
        default=1320,
        help="Секунды ожидания результата поиска",
    )
    parser.add_argument("--startup-timeout", type=positive_seconds, default=60)
    parser.add_argument("--poll-interval", type=positive_seconds, default=2)
    arguments = parser.parse_args()
    if not 0 <= arguments.port <= 65535:
        parser.error("--port должен быть в диапазоне 0–65535")
    if (
        arguments.date_from
        and arguments.date_to
        and arguments.date_from > arguments.date_to
    ):
        parser.error("--date-from должен быть не позже --date-to")
    arguments.idempotency_key = (
        None  # Each run creates a new query, not historical data.
    )
    return arguments


def configure(arguments: argparse.Namespace, directory: Path) -> None:
    if not os.environ.get("UBAS_DATABASE_URL"):
        raise RuntimeError(
            "Задайте UBAS_DATABASE_URL и запустите через uv run --env-file .env."
        )
    os.environ["UBAS_EMBEDDED_WORKER"] = "true"
    if arguments.demo:
        catalog = directory / "demo.sqlite3"
        write_demo_catalog(catalog)
        os.environ["UBAS_CATALOG_PATH"] = str(catalog)
        os.environ["UBAS_FLIGHT_PROVIDER_FACTORY"] = (
            "ubas_backend.routing.demo:create_flight_provider"
        )
        os.environ.pop("UBAS_PLANNER_CONFIG_PATH", None)
        print(
            "ДЕМО: вымышленные билеты, цены и визы. PostgreSQL настоящий.",
            file=sys.stderr,
        )
    else:
        os.environ.setdefault(
            "UBAS_FLIGHT_PROVIDER_FACTORY",
            "ubas_backend.agent_scraper.provider:create_flight_provider",
        )
        print("Поиск через билетный провайдер из окружения.", file=sys.stderr)
    settings = Settings.from_environment()
    catalog = settings.catalog_path or Path("var/catalog.sqlite3")
    if not catalog.is_file():
        raise RuntimeError(
            f"Каталог не найден: {catalog}. Создайте его или используйте --demo."
        )


async def apply_migrations(timeout: float) -> None:
    try:
        await asyncio.wait_for(migrate.run(), timeout)
    except TimeoutError:
        raise RuntimeError(
            "Таймаут миграций: проверьте доступность PostgreSQL."
        ) from None


def serve_and_query(arguments: argparse.Namespace, listener: socket.socket) -> int:
    server = uvicorn.Server(
        uvicorn.Config(
            "ubas_backend.main:app",
            access_log=False,
            timeout_graceful_shutdown=10,
        )
    )
    # A pre-bound socket prevents accidentally querying a different running backend.
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="ubas-smoke-api",
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + arguments.startup_timeout
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("API не запустился; подробности в журнале выше.")
            if time.monotonic() >= deadline:
                raise RuntimeError("Истекло время запуска API.")
            time.sleep(0.1)
        arguments.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        print(f"API готов: {arguments.base_url}/docs", file=sys.stderr)
        query_id = submit_query(arguments)
        print(f"Номер запроса: {query_id}", file=sys.stderr)
        return wait_for_result(arguments, query_id)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        if thread.is_alive():
            server.force_exit = True
            raise RuntimeError("Сервер не остановился за 30 секунд; проверьте журнал.")


def run(arguments: argparse.Namespace) -> int:
    with (
        TemporaryDirectory(prefix="ubas-smoke-") as directory,
        socket.socket() as listener,
    ):
        configure(arguments, Path(directory))
        # Bind before migrations: occupied ports fail without changing the database.
        listener.bind(("127.0.0.1", arguments.port))
        print("Применяю миграции очереди…", file=sys.stderr)
        asyncio.run(apply_migrations(arguments.startup_timeout))
        return serve_and_query(arguments, listener)


def main() -> int:
    arguments = parse_arguments()
    try:
        return run(arguments)
    except KeyboardInterrupt:
        print(
            "Остановлено. Запрос остаётся в PostgreSQL; его worker остановлен.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
