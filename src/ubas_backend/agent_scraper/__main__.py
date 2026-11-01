import asyncio
import sys

from loguru import logger

from ubas_backend.agent_scraper.models import CabinClass, SearchRequest
from ubas_backend.agent_scraper.presenter import print_offers
from ubas_backend.agent_scraper.service import get_flights


async def main() -> None:
    request = SearchRequest(
        origin="SVX",
        destination="AYT",
        depart_date="2026-11-05",
        adults=1,
        cabin_class=CabinClass.ECONOMY,
    )
    logger.info("Ищем билеты: {request}", request=request)

    data = await get_flights(request)
    print_offers(data)

    print("\n" + "-" * 50)
    logger.info("Повторный запуск — должны получить данные из кэша:")
    cached = await get_flights(request)
    print_offers(cached)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    asyncio.run(main())
