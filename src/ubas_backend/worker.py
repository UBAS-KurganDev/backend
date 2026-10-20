"""Standalone process for durable route-search jobs."""

import asyncio
import contextlib
import signal

from ubas_backend.runtime import create_runtime
from ubas_backend.settings import Settings


async def run() -> None:
    runtime = await create_runtime(Settings.from_environment(), include_worker=True)
    if runtime.worker is None:
        raise RuntimeError("Worker runtime was not configured")
    runtime.worker.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            # Windows asyncio.run handles Ctrl+C by cancelling the main task.
            continue
        installed.append(signum)
    try:
        await stop.wait()
    finally:
        await runtime.close()
        for signum in installed:
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(signum)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
