"""Explicit single-day diagnostic search, with the same provider used by workers."""

import argparse
import asyncio
import json
from datetime import date

from ubas_backend.agent_scraper.provider import create_flight_provider
from ubas_backend.routing.schemas import FlightQuery


async def search(origin: str, destination: str, day: date) -> None:
    provider = create_flight_provider()
    try:
        result = await provider.search(
            FlightQuery(
                origin=origin.upper(),
                destination=destination.upper(),
                departure_from=day,
                departure_to=day,
                passengers=1,
                limit=200,
            )
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    finally:
        await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="City IATA")
    parser.add_argument("--destination", required=True, help="City IATA")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    asyncio.run(search(args.origin, args.destination, args.date))


if __name__ == "__main__":
    main()
