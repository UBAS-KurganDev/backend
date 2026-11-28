"""Pure-HTTP agent.ru transport: no browser, reuse of the page-merge contract.

Replicates the request chain the site's own frontend performs:
login (session cookie + etm_auth_key) -> POST /api/air/search -> poll
GET /api/air/offers with next_token pagination. Pages are merged with the
same ResponseAccumulator contract as the browser transport, so mapping and
cache stay unchanged. The HTTP session is shared and refreshed on demand
(login is the slowest step, and the site allows 6-hour sessions). On
repeated transport failure it falls back to the browser agent so a
site-side change cannot take searches fully offline.
"""

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from ubas_backend.agent_scraper.config import Settings
from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest
from ubas_backend.agent_scraper.scraper_agent import (
    ResponseAccumulator,
    TransientScrapeError,
    run_agent_scraper,
)

logger = logging.getLogger(__name__)

BASE = "https://www.agent.ru"
SITE_HASH = "6cb08be74c"
_HTTP_ATTEMPTS = 2
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
# The site allows up to 6h; refresh earlier to stay away from expiry edges.
_SESSION_TTL_SECONDS = 3600.0


def _human_delay() -> float:
    return random.uniform(0.5, 1.5)


def _referer(request: SearchRequest) -> str:
    code = (
        f"{request.depart_date:%d%m}"
        f"{request.origin.lower()}{request.destination.lower()}"
    )
    return f"{BASE}/air/avia-results/{code}%7Ca1E/?lang=ru&curr=RUB"


class HttpTransport:
    """Shared HTTP session: one login, many searches, re-login on 403."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            headers={"accept": "application/json", "user-agent": _UA},
            timeout=30,
        )
        self._auth_key: str | None = None
        self._logged_in_at: float = 0.0
        self._login_lock = asyncio.Lock()

    async def _ensure_session(self, referer: str) -> None:
        async with self._login_lock:
            if (
                self._auth_key is not None
                and time.monotonic() - self._logged_in_at < _SESSION_TTL_SECONDS
            ):
                return
            login = await self._client.get(
                f"{BASE}/api/login/b2c/{SITE_HASH}",
                params={"tz": "Asia/Yekaterinburg", "locale": "ru", "currency": "RUB"},
                headers={"referer": referer},
            )
            if login.status_code != 200:
                raise TransientScrapeError("agent_search_failed") from None
            self._auth_key = login.json().get("etm_auth_key", "")
            self._logged_in_at = time.monotonic()

    async def _refresh_session(self, referer: str) -> None:
        async with self._login_lock:
            self._auth_key = None
        await self._ensure_session(referer)

    async def scrape(
        self, request: SearchRequest, settings: Settings
    ) -> CapturedOffers:
        started = time.monotonic()
        referer = _referer(request)
        await self._ensure_session(referer)
        accumulator = ResponseAccumulator()
        api_headers = {
            "referer": referer,
            "origin": BASE,
            "x-requested-with": "XMLHttpRequest",
            "accept-language": "ru-RU,ru;q=0.9",
            "etm-auth-key": self._auth_key or "",
        }
        search = await self._client.post(
            f"{BASE}/api/air/search",
            params={"use_search_cache": "1"},
            json={
                "directions": [
                    {
                        "departure_code": request.origin,
                        "arrival_code": request.destination,
                        "departure_city": request.origin,
                        "arrival_city": request.destination,
                        "date": request.depart_date.isoformat(),
                    }
                ],
                "class": "E",
                "direct": 0,
                "fare_types": ["PUB", "NEG"],
                "adult_qnt": 1,
                "child_qnt": 0,
                "infant_qnt": 0,
            },
            headers={**api_headers, "content-type": "application/json"},
        )
        if search.status_code == 403:
            # Session died mid-flight; refresh once and let the retry loop
            # resubmit with the fresh key.
            await self._refresh_session(referer)
        if search.status_code != 200:
            raise TransientScrapeError("agent_search_failed") from None
        request_id = search.json()["request_id"]

        base_query = {
            "request_id": request_id,
            "ui_currency": "RUB",
            "locale": "RU",
            "sort": "price",
            "get_luggage_info": "1",
        }
        next_token: str | None = None
        while True:
            if time.monotonic() - started > settings.timeout_seconds:
                raise TransientScrapeError("agent_search_failed") from None
            query = dict(base_query)
            if next_token:
                query["next_token"] = next_token
            offers = await self._client.get(
                f"{BASE}/api/air/offers",
                params=query,
                headers=api_headers,
            )
            if offers.status_code != 200:
                raise TransientScrapeError("agent_search_failed") from None
            data = offers.json()
            page_url = f"{BASE}/api/air/offers?{urlencode(query)}"
            # Same page-merge contract as the browser transport; True on Ready.
            if accumulator.add(page_url, data):
                break
            next_token = data.get("next_token") or None
            await asyncio.sleep(_human_delay())
        return CapturedOffers(fetched_at=datetime.now(UTC), data=accumulator.data)

    async def aclose(self) -> None:
        await self._client.aclose()


_transport: HttpTransport | None = None


def _get_transport() -> HttpTransport:
    global _transport
    if _transport is None:
        _transport = HttpTransport()
    return _transport


async def run_http_scraper(
    request: SearchRequest, settings: Settings
) -> CapturedOffers:
    transport = _get_transport()
    last_error: TransientScrapeError | None = None
    for _ in range(_HTTP_ATTEMPTS):
        try:
            return await transport.scrape(request, settings)
        except TransientScrapeError as error:
            last_error = error
            await transport._refresh_session(_referer(request))
    logger.warning(
        "agent_http_fallback origin=%s destination=%s date=%s error=%s",
        request.origin,
        request.destination,
        request.depart_date,
        last_error and last_error.args[0],
    )
    # A site-side change (captcha, schema, WAF) must not take searches offline;
    # degrade to the browser transport.
    try:
        return await run_agent_scraper(request, settings)
    finally:
        if last_error is not None:
            logger.warning(
                "agent_http_fallback_used origin=%s destination=%s",
                request.origin,
                request.destination,
            )
