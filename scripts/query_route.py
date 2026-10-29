"""Submit a route query to the local API and print its eventual JSON result."""

import argparse
import json
import math
import sys
import time
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Укажите положительное конечное число секунд.")
    return seconds


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--origin", default="MOW", help="IATA-код города отправления")
    parser.add_argument(
        "--destination", default="TYO", help="IATA-код города назначения"
    )
    parser.add_argument("--date-from", type=date.fromisoformat)
    parser.add_argument("--date-to", type=date.fromisoformat)
    parser.add_argument("--passengers", type=int, default=1)
    parser.add_argument("--citizenship", default="RU")
    parser.add_argument("--currency", default="RUB")
    parser.add_argument(
        "--budget", help="Общий бюджет билетов и виз для всех пассажиров"
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--exclude-city", action="append", default=[])
    parser.add_argument("--checked-baggage-required", action="store_true")
    parser.add_argument(
        "--idempotency-key", help="Ключ для повторного создания запроса"
    )
    parser.add_argument(
        "--query-id", type=UUID, help="Продолжить ожидание прежнего запроса"
    )
    parser.add_argument("--timeout", type=positive_seconds, default=600)
    parser.add_argument("--poll-interval", type=positive_seconds, default=2)
    return parser.parse_args()


def request_json(
    url: str,
    *,
    timeout: float,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {"Accept": "application/json"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers=headers)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        body: object = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError(f"Ожидался JSON-объект от {url}; HTTP {status}.")
    return status, body


def print_json(body: dict[str, object]) -> None:
    print(json.dumps(body, ensure_ascii=False, indent=2))


def submit_query(arguments: argparse.Namespace) -> UUID:
    date_from = arguments.date_from or datetime.now(UTC).date() + timedelta(days=14)
    date_to = arguments.date_to or date_from + timedelta(days=3)
    payload = {
        "origin": arguments.origin,
        "destination": arguments.destination,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "citizenship": arguments.citizenship,
        "passengers": arguments.passengers,
        "currency": arguments.currency,
        "budget": arguments.budget,
        "limit": arguments.limit,
        "excluded_cities": arguments.exclude_city,
        "checked_baggage_required": arguments.checked_baggage_required,
    }
    key = arguments.idempotency_key or str(uuid4())
    print(f"Idempotency-Key: {key}", file=sys.stderr)
    status, body = request_json(
        arguments.base_url.rstrip("/") + "/api/v1/queries",
        timeout=min(10, arguments.timeout),
        payload=payload,
        idempotency_key=key,
    )
    if status != 202:
        print_json(body)
        raise RuntimeError(f"Не удалось создать запрос: HTTP {status}.")
    query_id = body.get("query_id")
    if not isinstance(query_id, str):
        raise RuntimeError("В ответе API отсутствует query_id.")
    return UUID(query_id)


def request_before_deadline(
    url: str, deadline: float, query_id: UUID
) -> tuple[int, dict[str, object]]:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            return request_json(url, timeout=min(10, remaining))
        except OSError:
            if time.monotonic() < deadline:
                raise
    raise TimeoutError(
        f"Истекло время ожидания запроса {query_id}. "
        "Проверьте worker, каталог и провайдер билетов. "
        f"Для продолжения используйте --query-id {query_id}."
    )


def wait_for_result(arguments: argparse.Namespace, query_id: UUID) -> int:
    url = f"{arguments.base_url.rstrip('/')}/api/v1/queries/{query_id}"
    deadline = time.monotonic() + arguments.timeout
    previous_status = None
    while True:
        status, metadata = request_before_deadline(url, deadline, query_id)
        if status != 200:
            print_json(metadata)
            raise RuntimeError(f"Не удалось получить статус: HTTP {status}.")
        query_status = metadata.get("status")
        if query_status != previous_status:
            print(f"Статус: {query_status}", file=sys.stderr)
            previous_status = query_status
        if query_status in {"completed", "failed"}:
            status, result = request_before_deadline(
                url + "/result", deadline, query_id
            )
            print_json(result)
            if query_status == "completed" and status == 200:
                return 0
            raise RuntimeError(f"Запрос завершился ошибкой: HTTP {status}.")
        if query_status not in {"queued", "running"}:
            raise RuntimeError(f"Неизвестный статус запроса: {query_status!r}.")
        time.sleep(min(arguments.poll_interval, max(0, deadline - time.monotonic())))


def main() -> int:
    arguments = parse_arguments()
    try:
        query_id = arguments.query_id or submit_query(arguments)
        print(f"Номер запроса: {query_id}", file=sys.stderr)
        return wait_for_result(arguments, query_id)
    except KeyboardInterrupt:
        print(
            "Ожидание прервано; задача в бэкенде продолжает существовать.",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
