"""Fan airport-pair captures out to N independent scraper nodes in parallel."""

import asyncio
import logging

import httpx
from pydantic import ValidationError

from ubas_backend.agent_scraper.cache import ResponseCache
from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.scraper_agent import TransientScrapeError
from ubas_backend.agent_scraper.service import FlightSearchService
from ubas_backend.routing.ports import FlightProviderError

logger = logging.getLogger(__name__)

SEARCH_PATH = "/v1/search"
TOKEN_HEADER = "X-UBAS-Node-Token"


class _NodeFailure(Exception):
    """Node response unusable; the caller decides whether a retry is honest."""

    def __init__(self, code: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


class NodePool:
    """Least-loaded dispatch with bounded concurrency on every node.

    Each node owns its browsers and its local cache. One airport pair is sent to
    one node; independent pairs are distributed concurrently. Transport failures
    are retried on another node within a bounded number of attempts.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not settings.node_urls:
            raise ValueError("At least one scraper node URL is required")
        token = (
            settings.node_token.get_secret_value()
            if settings.node_token is not None
            else None
        )
        headers = {TOKEN_HEADER: token} if token else {}
        self._clients = tuple(
            httpx.AsyncClient(
                base_url=url,
                timeout=httpx.Timeout(settings.node_timeout_seconds, connect=5.0),
                headers=headers,
                transport=transport,
            )
            for url in settings.node_urls
        )
        self._slots = tuple(
            asyncio.Semaphore(settings.node_concurrency) for _ in settings.node_urls
        )
        self._inflight = [0] * len(settings.node_urls)
        self._cursor = 0
        self._max_attempts = settings.node_max_attempts
        self._closed = False

    def _pick(self) -> int:
        # The event loop is single-threaded; counters change only at await points.
        # Ties rotate so retries do not always return to the node that just failed.
        count = len(self._clients)
        offset = min(
            range(count),
            key=lambda item: self._inflight[(self._cursor + item) % count],
        )
        index = (self._cursor + offset) % count
        self._cursor = (index + 1) % count
        self._inflight[index] += 1
        return index

    async def _post(self, index: int, request: SearchRequest) -> CapturedOffers:
        try:
            async with self._slots[index]:
                response = await self._clients[index].post(
                    SEARCH_PATH, json=request.model_dump(mode="json")
                )
        finally:
            self._inflight[index] -= 1
        if response.status_code == 200:
            try:
                return CapturedOffers.model_validate_json(response.content)
            except ValidationError as error:
                raise _NodeFailure(
                    "agent_node_invalid_response", transient=False
                ) from error
        transient = response.status_code in (408, 429) or response.status_code >= 500
        raise _NodeFailure(
            f"agent_node_http_{response.status_code}", transient=transient
        )

    async def scrape(
        self, request: SearchRequest, settings: Settings
    ) -> CapturedOffers:
        del settings  # The node keeps its own browser settings; dispatch is local.
        failure: BaseException | None = None
        code = "agent_node_unavailable"
        for attempt in range(1, self._max_attempts + 1):
            index = self._pick()
            try:
                return await self._post(index, request)
            except httpx.TransportError as error:
                failure, code = error, "agent_node_unreachable"
            except _NodeFailure as error:
                if not error.transient:
                    raise FlightProviderError(error.code) from error
                failure, code = error, error.code
            if attempt < self._max_attempts:
                logger.warning(
                    "agent_node_retry origin=%s destination=%s date=%s "
                    "node=%s attempt=%s",
                    request.origin,
                    request.destination,
                    request.depart_date,
                    index,
                    attempt + 1,
                )
        assert failure is not None
        raise TransientScrapeError(code) from failure

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self._clients:
            await client.aclose()


class NodeSearchService:
    """Coordinator cache and single-flight over remote nodes."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._pool = NodePool(settings, transport=transport)
        # The pool enforces per-node limits; the service only bounds the total.
        distributed = settings.model_copy(
            update={
                "max_concurrency": len(settings.node_urls) * settings.node_concurrency,
                # NodePool owns cross-node retries, so do not retry twice.
                "max_attempts": 1,
            }
        )
        self._service = FlightSearchService(
            settings=distributed,
            cache=ResponseCache(settings),
            scrape=self._pool.scrape,
        )

    async def get_offers(self, request: SearchRequest) -> CapturedOffers:
        return await self._service.get_offers(request)

    async def aclose(self) -> None:
        try:
            await self._service.aclose()
        finally:
            await self._pool.aclose()
