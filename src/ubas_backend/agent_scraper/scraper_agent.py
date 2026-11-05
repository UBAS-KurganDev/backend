"""Capture a complete, correlated chain of source responses in an isolated browser."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response, async_playwright

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.routing.ports import FlightProviderError


class ResponseAccumulator:
    """Source next_token pages are deltas, including the final Ready page."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {"offers": [], "ow_offers": []}
        self._request_id: str | None = None
        self._next_token: str | None = None
        self._seen: dict[str | None, str] = {}

    def add(self, url: str, data: dict[str, Any]) -> bool:
        query = parse_qs(urlsplit(url).query)
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or query.get("request_id") != [request_id]:
            raise FlightProviderError("agent_request_mismatch")
        if self._request_id is not None and request_id != self._request_id:
            raise FlightProviderError("agent_search_changed")
        self._request_id = request_id
        token = query.get("next_token", [None])[0]
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        if token in self._seen:
            if self._seen[token] != digest:
                raise FlightProviderError("agent_conflicting_page")
            return False  # An identical transport replay is not another fare.
        if token != self._next_token:
            raise FlightProviderError("agent_missing_page")
        if data.get("status") not in ("Ready", "InProcess"):
            raise FlightProviderError("agent_unexpected_status")
        currency = data.get("currency")
        if not isinstance(currency, str) or (
            "currency" in self.data and currency != self.data["currency"]
        ):
            raise FlightProviderError("agent_currency_changed")
        for key in ("offers", "ow_offers"):
            batch = data.get(key)
            if not isinstance(batch, list):
                raise FlightProviderError("agent_invalid_page")
            self.data[key].extend(batch)
        self._seen[token] = digest
        next_token = data.get("next_token")
        if data["status"] == "InProcess" and (
            not isinstance(next_token, str) or next_token in self._seen
        ):
            raise FlightProviderError("agent_invalid_next_token")
        self._next_token = next_token
        self.data.update(status=data["status"], currency=currency)
        return bool(data["status"] == "Ready")


async def run_agent_scraper(
    request: SearchRequest, settings: Settings
) -> CapturedOffers:
    responses: asyncio.Queue[tuple[Response, datetime]] = asyncio.Queue(
        settings.max_response_batches
    )
    overflow = False

    def on_response(response: Response) -> None:
        nonlocal overflow
        url = urlsplit(response.url)
        if url.hostname not in ("agent.ru", "www.agent.ru"):
            return
        if url.path.rstrip("/") != "/api/air/offers":
            return
        try:
            responses.put_nowait((response, datetime.now(UTC)))
        except asyncio.QueueFull:
            overflow = True

    try:
        async with (
            async_playwright() as playwright,
            asyncio.timeout(settings.timeout_seconds),
        ):
            # Launch is inside the timeout so a hung start cannot outlive it.
            browser = await playwright.chromium.launch(headless=settings.headless)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                page.on("response", on_response)
                await page.goto(request.search_url, wait_until="domcontentloaded")
                accumulator = ResponseAccumulator()
                size = 0
                fetched_at: datetime | None = None
                for _ in range(settings.max_response_batches):
                    response, received_at = await responses.get()
                    fetched_at = fetched_at or received_at
                    if overflow or response.status != 200:
                        raise FlightProviderError("agent_response_unavailable")
                    body = await response.body()
                    size += len(body)
                    if size > 20_000_000:
                        raise FlightProviderError("agent_response_size_limit")
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise FlightProviderError("agent_invalid_response")
                    if accumulator.add(response.url, data):
                        return CapturedOffers(
                            fetched_at=fetched_at, data=accumulator.data
                        )
                raise FlightProviderError("agent_response_batch_limit")
            finally:
                # Closing the browser closes all contexts even on cancellation.
                await browser.close()
    except (PlaywrightError, TimeoutError, ValueError) as error:
        raise FlightProviderError("agent_search_failed") from error
