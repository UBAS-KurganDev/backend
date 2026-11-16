"""Internal HTTP server that runs one scraper node per process."""

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, cast

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.nodes import SEARCH_PATH, TOKEN_HEADER
from ubas_backend.agent_scraper.ports import OfferService
from ubas_backend.agent_scraper.scraper_agent import TransientScrapeError
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.ports import FlightProviderError

logger = logging.getLogger(__name__)


def create_node_app(
    settings: Settings | None = None,
    service: OfferService | None = None,
) -> FastAPI:
    """Build a node app; tests and benchmarks may inject an OfferService."""
    node_settings = settings or Settings()
    owns_service = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO)
        app.state.service = (
            service
            if service is not None
            else FlightSearchService(settings=node_settings)
        )
        try:
            yield
        finally:
            if owns_service:
                await cast("OfferService", app.state.service).aclose()

    app = FastAPI(title="UBAS Scraper Node", lifespan=lifespan)

    def authorize(
        token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
    ) -> None:
        expected = node_settings.node_token
        if expected is not None and token != expected.get_secret_value():
            raise HTTPException(status_code=401, detail="agent_node_unauthorized")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(SEARCH_PATH, response_model=CapturedOffers)
    async def search(
        body: SearchRequest,
        http_request: Request,
        _: Annotated[None, Depends(authorize)] = None,
    ) -> CapturedOffers:
        active = cast("OfferService", http_request.app.state.service)
        try:
            return await active.get_offers(body)
        except TransientScrapeError as error:
            # The coordinator may retry the same pair on another node.
            raise HTTPException(status_code=503, detail=str(error)) from error
        except FlightProviderError as error:
            # Source rejected this pair/date; retrying cannot change that.
            raise HTTPException(status_code=422, detail=str(error)) from error

    return app


def main() -> None:
    settings = Settings()
    parser = argparse.ArgumentParser(description="Run one agent.ru scraper node.")
    parser.add_argument("--host", default=settings.node_host)
    parser.add_argument("--port", type=int, default=settings.node_port)
    arguments = parser.parse_args()
    uvicorn.run(
        create_node_app(settings),
        host=arguments.host,
        port=arguments.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
