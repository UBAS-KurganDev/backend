"""FastAPI application and its lifecycle-managed runtime dependencies."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ubas_backend.api.router import router
from ubas_backend.runtime import Runtime, create_runtime
from ubas_backend.settings import Settings, load_cors_origins


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


def create_app(*, cors_origins: tuple[str, ...] | None = None) -> FastAPI:
    app = FastAPI(title="UBAS Backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=load_cors_origins() if cors_origins is None else cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["Location", "Retry-After"],
    )
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()


def main() -> None:
    """Run the ASGI server on http://127.0.0.1:8000."""
    uvicorn.run("ubas_backend.main:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
