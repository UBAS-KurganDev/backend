"""Capture interfaces shared by the in-process scraper and remote nodes."""

from typing import Protocol

from ubas_backend.agent_scraper.models import CapturedOffers, SearchRequest


class OfferService(Protocol):
    """One airport-pair capture, regardless of where the browser runs."""

    async def get_offers(self, request: SearchRequest) -> CapturedOffers: ...

    async def aclose(self) -> None: ...
