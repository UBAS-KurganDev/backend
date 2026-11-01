"""Regression checks for the agent.ru scraper adapter (mapping and provider)."""

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from ubas_backend.agent_scraper.mapping import map_response
from ubas_backend.agent_scraper.models import SearchRequest
from ubas_backend.agent_scraper.provider import (
    AgentFlightProvider,
    create_flight_provider,
)
from ubas_backend.routing.schemas import FlightQuery, FlightSearchResult

_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

_RESPONSE = {
    "status": "Ready",
    "currency": "RUB",
    "offers": [
        {
            "carrier_code": "U6",
            "offers": [
                {
                    "min_price": 11123,
                    "segments": [
                        {
                            "departure_airport": "SVX",
                            "arrival_airport": "AYT",
                            "departure_date": "2026-11-05",
                            "departure_time": "14:30:00",
                            "arrival_date": "2026-11-05",
                            "arrival_time": "17:35:00",
                            "marketing_airline_code": "U6",
                            "flight_number": "1575",
                        }
                    ],
                }
            ],
        }
    ],
    "ow_offers": [],
}


def _query(**overrides: object) -> FlightQuery:
    values: dict = {
        "origin": "SVX",
        "destination": "AYT",
        "departure_from": date(2026, 11, 5),
        "departure_to": date(2026, 11, 5),
        "passengers": 1,
        "limit": 10,
    }
    values.update(overrides)
    return FlightQuery.model_validate(values)


class MappingTests(unittest.TestCase):
    def test_response_maps_to_contract_offers(self):
        offers = map_response(_RESPONSE, now=_NOW, passengers=1)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer.provider, "agent-ru")
        self.assertEqual(offer.total_price.amount, Decimal("11123"))
        self.assertEqual(offer.total_price.currency, "RUB")
        segment = offer.segments[0]
        self.assertEqual(segment.origin.code, "SVX")
        self.assertEqual(segment.origin.city_code, "SVX")
        self.assertEqual(segment.destination.code, "AYT")
        self.assertEqual(segment.destination.city_code, "AYT")
        self.assertEqual(segment.departure_at.utcoffset().total_seconds(), 5 * 3600)

    def test_unknown_airport_drops_offer(self):
        broken = {
            "currency": "RUB",
            "offers": [
                {
                    "carrier_code": "XX",
                    "offers": [
                        {
                            "min_price": 100,
                            "segments": [
                                {
                                    "departure_airport": "SVX",
                                    "arrival_airport": "ZZZ",
                                    "departure_date": "2026-11-05",
                                    "departure_time": "10:00:00",
                                    "arrival_date": "2026-11-05",
                                    "arrival_time": "12:00:00",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        self.assertEqual(map_response(broken, now=_NOW, passengers=1), ())

    def test_bad_price_drops_offer(self):
        broken = {
            "currency": "RUB",
            "offers": [
                {
                    "carrier_code": "XX",
                    "offers": [{"min_price": "N/A", "segments": []}],
                }
            ],
        }
        self.assertEqual(map_response(broken, now=_NOW, passengers=1), ())

    def test_ow_offers_are_included(self):
        data = {"currency": "RUB", "offers": [], "ow_offers": _RESPONSE["offers"]}
        offers = map_response(data, now=_NOW, passengers=2)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].passengers, 2)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_implements_flight_provider_protocol(self):
        provider = create_flight_provider()
        self.assertTrue(hasattr(provider, "search"))
        self.assertTrue(callable(provider.search))

    async def test_search_filters_mapped_offers_by_query(self):
        provider = AgentFlightProvider()

        async def fake_raw(origin: str, destination: str, depart_date: str) -> dict:
            return _RESPONSE

        provider._raw_response = fake_raw  # noqa: SLF001 - проверка изоляции
        result = await provider.search(_query())
        self.assertIsInstance(result, FlightSearchResult)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.offers), 1)

    async def test_search_reports_miss_when_dates_outside_query(self):
        provider = AgentFlightProvider()

        async def fake_raw(origin: str, destination: str, depart_date: str) -> dict:
            return _RESPONSE

        provider._raw_response = fake_raw  # noqa: SLF001 - проверка изоляции
        result = await provider.search(
            _query(departure_from=date(2026, 12, 1), departure_to=date(2026, 12, 1))
        )
        self.assertEqual(result.offers, ())
        self.assertEqual(result.warnings, ("agent_no_matching_offers",))

    async def test_transport_error_becomes_provider_error(self):
        from ubas_backend.routing.ports import FlightProviderError

        provider = AgentFlightProvider()

        async def broken(origin: str, destination: str, depart_date: str) -> dict:
            raise RuntimeError("browser crashed")

        provider._raw_response = broken  # noqa: SLF001 - проверка изоляции
        with self.assertRaises(FlightProviderError):
            await provider.search(_query())

    async def test_depart_date_uses_middle_of_range(self):
        self.assertEqual(
            AgentFlightProvider._depart_date(
                _query(departure_from=date(2026, 11, 1), departure_to=date(2026, 11, 9))
            ),
            "2026-11-05",
        )


class RequestModelTests(unittest.TestCase):
    def test_search_request_urls_and_cache_key(self):
        request = SearchRequest(
            origin="svx", destination="AYT", depart_date="2026-11-05"
        )
        self.assertEqual(
            request.search_url,
            "https://www.agent.ru/air/avia-results/0511svxayt%7Ca1E/?lang=ru&curr=RUB",
        )
        self.assertEqual(request.cache_key, "flights:SVX:AYT:2026-11-05:E:1:0:0:any")


if __name__ == "__main__":
    unittest.main()
