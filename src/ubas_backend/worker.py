"""Standalone process for durable route-search jobs."""

import asyncio

from ubas_backend.runtime import create_runtime
from ubas_backend.settings import Settings


async def run() -> None:
    runtime = await create_runtime(Settings.from_environment(), include_worker=True)
    if runtime.worker is None:
        raise RuntimeError("Worker runtime was not configured")
    runtime.worker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
