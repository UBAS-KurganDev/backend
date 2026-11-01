import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response, async_playwright

from ubas_backend.agent_scraper.config import settings
from ubas_backend.agent_scraper.models import CabinClass, SearchRequest

JsonResponse = dict[str, Any] | list[Any]

# Маска эндпоинта agent.ru, который содержит полезные данные
_TARGET_URL_MASK = "api/air/offers"
# Статусы ответа: "InProcess" — идёт поиск, "Ready" — поиск завершён
_IN_PROCESS_STATUS = "InProcess"
_READY_STATUS = "Ready"


def _merge_offers(accumulator: dict[str, Any], data: dict[str, Any]) -> None:
    """Накапливает офферы и метаданные из промежуточных ответов API.

    Сайт присылает пагинацию: несколько ответов со статусом InProcess,
    в каждом из которых может быть порция офферов (offers / ow_offers),
    а завершающий ответ Happy со статусом Ready содержит лишь статус,
    но пустой список offers. Поэтому копим всё в один результат.
    """
    for key in ("status", "request_id", "currency", "travel_policy_id", "is_schedule"):
        if key in data:
            accumulator[key] = data.get(key)

    for key in ("offers", "ow_offers"):
        batch = data.get(key)
        if not isinstance(batch, list):
            continue
        merged: list[Any] = (
            accumulator[key] if isinstance(accumulator.get(key), list) else []
        )
        # Допустим, что повторная порция из-за next_token уже включена —
        # оставлю простую конкатенацию (будут отдельные группы).
        merged.extend(batch)
        accumulator[key] = merged


async def run_agent_scraper(
    request: SearchRequest,
    on_response: Callable[[SearchRequest, str, JsonResponse], Awaitable[None]]
    | None = None,
    wait_seconds: int = 25,
) -> list[JsonResponse] | None:
    """Запускает headless-браузер, переходит по поисковой ссылке agent.ru
    и перехватывает ответы API offers.

    Логика перехвата:
      - ответы со статусом InProcess могут содержать порции офферов —
        они накапливаются.
      - ответ со статусом Ready сигнализирует о завершении поиска; ему
        передаётся объединённый результат (все накопленные офферы + статус
        Ready), поэтому данные появляются у on_response и в кэше.

    Браузер не закрывает блокирующая задача — закрытие происходит здесь.
    """
    target_url = request.search_url
    captured: list[JsonResponse] = []
    merged_ready: dict[str, Any] | None = None
    ready_list: list[JsonResponse] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(settings.user_data_dir),
            headless=settings.headless_mode,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            no_viewport=True,
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()

        async def handle_response(response: Response) -> None:
            nonlocal merged_ready
            if "json" not in response.headers.get("content-type", ""):
                return
            url = response.url
            if _TARGET_URL_MASK not in url:
                return
            try:
                data = await response.json()
            except (ValueError, TypeError) as e:
                logger.error("Не удалось распарсить JSON: {error}", error=e)
                return
            if not isinstance(data, dict):
                return

            status = data.get("status")
            if status == _READY_STATUS:
                # Финальный ответ — завершаем поиск и передаём результат
                if merged_ready is None:
                    merged_ready = {}
                _merge_offers(merged_ready, data)
                merged_ready["status"] = _READY_STATUS
                logger.info("Поиск завершён (Ready): {url}", url=url)
                ready_list.append(merged_ready)
                if on_response:
                    await on_response(request, url, merged_ready)
            elif status == _IN_PROCESS_STATUS:
                # Промежуточный ответ — в нём могут быть офферы
                logger.debug(
                    "Накоплен промежуточный ответ ({offers} офферов): {url}",
                    offers=len(data.get("offers") or []),
                    url=url,
                )
                if merged_ready is None:
                    merged_ready = {"status": _IN_PROCESS_STATUS}
                _merge_offers(merged_ready, data)
                captured.append(data)
            else:
                logger.debug("Пропущен посторонний ответ: {url}", url=url)

        page.on("response", handle_response)

        logger.info("URL Переходим по ссылке поиска: {url}", url=target_url)
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        except (TimeoutError, PlaywrightError) as e:
            logger.error("Ошибка при загрузке страницы: {error}", error=e)

        logger.info(
            "Ждём прогрузки результатов и ответов API ({wait} секунд)...",
            wait=wait_seconds,
        )
        await asyncio.sleep(wait_seconds)

        # Если за всё время так и не пришёл Ready, но были InProcess-порции,
        # соберём накопленное и отдадим его как есть.
        if merged_ready is not None and merged_ready.get("status") != _READY_STATUS:
            logger.info("Время ожидания истекло — фиксируем накопленные офферы.")
            merged_ready["status"] = _READY_STATUS
            ready_list.append(merged_ready)
            if on_response:
                await on_response(request, target_url, merged_ready)

        await browser.close()
        logger.info("Готово!")

    # Если Ready не пришёл вовсе (ни InProcess, ни Ready) — вернём None
    return ready_list or None


if __name__ == "__main__":
    # Пример: Екатеринбург -> Анталья, 16 октября 2026,
    # 2 взрослых, 1 ребёнок, эконом-класс
    test_request = SearchRequest(
        origin="SVX",
        destination="AYT",
        depart_date="2026-10-16",
        adults=2,
        children=1,
        cabin_class=CabinClass.ECONOMY,
    )
    asyncio.run(run_agent_scraper(test_request))
