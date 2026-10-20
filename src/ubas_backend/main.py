"""FastAPI application and its lifecycle-managed runtime dependencies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from ubas_backend.api.router import router
from ubas_backend.runtime import Runtime, create_runtime
from ubas_backend.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_environment(need_planner=None)
    runtime: Runtime = await create_runtime(
        settings, include_worker=settings.embedded_worker
    )
    app.state.query_manager = runtime.manager
    if runtime.worker is not None:
        runtime.worker.start()
    try:
        yield
    finally:
        await runtime.close()


def create_app() -> FastAPI:
    app = FastAPI(title="UBAS Backend", lifespan=lifespan)
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()


def main() -> None:
    """Run the ASGI server on http://127.0.0.1:8000."""
    uvicorn.run("ubas_backend.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
