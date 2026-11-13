# UBAS Backend

Python-бэкенд поиска дешёвых авиамаршрутов: прямые рейсы и маршруты максимум с одной остановкой,
единые и отдельные билеты. Выдача — десять предложений по известной стоимости
билетов и виз. Привлекательность фильтрует остановки от 12 часов; временный порог
60/100. Дубли сохраняются, неизвестные расходы явно отмечаются.
Для поиска выбираются до 30 промежуточных городов по привлекательности и связности;
охват явно возвращается в search_scope. Это не гарантия лучшей цены во всём графе.

Требования — [UBAS-KurganDev/docs](https://github.com/UBAS-KurganDev/docs).
Авторизации, покупки, подбора отелей и проверки права въезда нет. При смене
аэропорта в одном городе требуется минимум два часа.
Реальный билетный источник agent.ru работает внутри worker; демо содержит вымышленные данные.

Исходники: src/ubas_backend. Python 3.14+, FastAPI, Pydantic 2,
SQLAlchemy ORM/Core, Alembic. Зависимости через uv; Ruff и строгий mypy.

## Документация

- [Импорт туристической привлекательности](docs/tourism-import.md), формула и HTTP городов.

- [Устройство](docs/architecture.md), [контракты и настройки](docs/routing.md).
- [Алгоритм и безопасные отсечения](docs/planner-search.md).
- [Новый контракт для фронта](docs/direct-routes.md).
- [Подмена источников](docs/repositories.md).
- [Сборщик топологии: источники, загрузка и обновление](docs/topology.md).
- [Очередь, HTTP и deployment](docs/query-manager.md).
- [Покрытие MVP и открытые вопросы](docs/mvp-coverage.md).
- [Скрапер agent.ru: запуск, формат, кэш и ограничения](docs/agent-scraper.md).
- [Правила разработки](AGENTS.md).
- [Охват расширенной топологии и ограничения поиска](docs/topology-coverage.md).

## Локальный запуск

### Запустить бэкенд и сразу получить результат

Нужны установленный `uv`, доступный PostgreSQL и `.env` с `UBAS_DATABASE_URL`.
Скрипт применяет миграции, запускает API со встроенным worker на свободном
локальном порту, отправляет MOW → TYO с вылетом через 14 дней в окне четырёх
дат, ждёт завершения и печатает JSON. Затем останавливает свой сервер.

Проверка всей цепочки с вымышленными билетами и временным каталогом:

```shell
uv run --locked --env-file .env python scripts/run_backend_query.py --demo
```

Реальный поиск использует каталог, конфигурацию планнера и провайдер из `.env`:

```shell
uv run --locked --env-file .env python scripts/run_backend_query.py --origin SVX --destination TYO
```

Для agent.ru заранее установите Chromium: `uv run --locked playwright install chromium`.
Параметры `--date-from YYYY-MM-DD --date-to YYYY-MM-DD`, `--timeout 1320`,
`--startup-timeout 60`, `--port 8001` меняют даты и ожидание/порт. По умолчанию скрипт ждёт до 1320 секунд. Планнер ограничен 1200 секундами,
worker — 1260 секундами; при обновлении старого .env замените
UBAS_WORKER_TIMEOUT_SECONDS=300 на 1260 и обновите JSON планнера.
Ожидание скрипта не увеличивает внутренние бюджеты планнера и worker. Ошибка поиска выводится явно,
код завершения будет 1; успешная пустая выдача остаётся возможной.
JSON идёт в stdout, прогресс и журналы — в stderr; для сохранения добавьте
`> result.json`. Скрипт не запускает PostgreSQL и не очищает очередь.
При таймауте/прерывании задача остаётся в БД, её сможет подхватить следующий worker.
Если параллельно работает другой worker, используйте отдельную БД: иначе он может
забрать демонстрационный запрос со своим провайдером. `.env` скрипт не меняет.

### Обычный запуск

```shell
uv sync --locked
```

Подготовьте .env по [.env.example](.env.example), PostgreSQL и каталог.
API нужен каталог с часовыми зонами городов; worker также читает топологию и
привлекательность. Источники заменяются обычным кодом в runtime.py, отдельно
для топологии, профилей и географии.

Для реальных связей используйте [сборщик топологии](docs/topology.md):
он создаёт каталог направлений и географии, сохраняя существующие туристические
профили при обновлении. Оценки привлекательности и визы загружаются отдельно.

Для реальных билетов установите браузер командой
`uv run --locked playwright install chromium` и задайте
`UBAS_FLIGHT_PROVIDER_FACTORY=ubas_backend.agent_scraper.provider:create_flight_provider`.
Скопируйте `config/planner.json` в `./var/planner.json` и укажите
`UBAS_PLANNER_CONFIG_PATH=./var/planner.json` для API и worker. Актуальный файл
задаёт 150 секунд на городской запрос и 20 минут на весь поиск.
Это подключает скрапер к существующим трём HTTP-ручкам через очередь.
Реальный каталог городов и топологии нужен отдельно; скрапер его не создаёт.
Покрытие аэропортов, лимиты и настройка описаны в [инструкции](docs/agent-scraper.md).

Для демонстрации создайте отдельный синтетический каталог:

```shell
uv run --locked python -m ubas_backend.routing.demo --catalog ./var/demo-catalog.sqlite3
```

Укажите:

```dotenv
UBAS_CATALOG_PATH=./var/demo-catalog.sqlite3
UBAS_FLIGHT_PROVIDER_FACTORY=ubas_backend.routing.demo:create_flight_provider
```

Это не реальные цены или визовые сведения. Генератор создаёт географию
MOW/ALA/TYO и маршруты для вылета через 14 дней от текущего UTC.
Без --overwrite существующий файл не изменяется. Флаг используйте только для
отдельного демо-каталога; он заменяет выбранный файл целиком.

Примените миграции и запустите API/worker в разных терминалах:

```shell
uv run --env-file .env ubas-migrate
uv run --env-file .env ubas-api
uv run --env-file .env ubas-worker
```

Приложение само .env не читает. Swagger: http://127.0.0.1:8000/docs.
Для локального встроенного worker задайте UBAS_EMBEDDED_WORKER=true.
API без worker принимает запросы, но они остаются queued.

Самостоятельный пример планировщика без PostgreSQL:

```shell
uv run --locked python -m ubas_backend.routing.demo
```

## Docker Compose

Нужны Docker Engine и Compose. Подготовьте .env, непустой POSTGRES_PASSWORD
и каталог в ./var до запуска. ./var и ./config монтируются в /app/var и
/app/config read-only для API и worker. Можно использовать
`UBAS_PLANNER_CONFIG_PATH=./config/planner.json` или свой JSON в ./var.
Compose передаёт пути из .env, включая `UBAS_AGENT_TOPOLOGY_SNAPSHOT_PATH`.
Пустой путь планнера включает стандартные настройки; пустой путь снимка —
встроенный справочник аэропортов скрапера.

Файлы должны читаться пользователем контейнера с UID 10001. Сборщик создаёт
JSON-снимок с правами только владельца; для публичного снимка аэропортов
добавьте право чтения, например:

```shell
chmod a+r ./var/topology/scraper-airports-2026-11-10.json
```

Укажите фактическое имя своего снимка. Для реального поиска нужны каталог,
снимок с покрытием выбранных городов и фабрика agent.ru из .env.example.

Если uv на хосте нет, демо-каталог можно создать командой:

```shell
docker compose run --rm --no-deps --build --user 0:0 --volume ./var:/app/var:rw api python -m ubas_backend.routing.demo --catalog ./var/demo-catalog.sqlite3
```

Задайте переменные демонстрационного источника выше, затем:

```shell
docker compose --profile worker up --build -d
docker compose ps
docker compose logs -f api
```

Без профиля worker запускаются только PostgreSQL и API.
Compose строит внутренний PostgreSQL URL из POSTGRES_*; UBAS_DATABASE_URL нужен
локальным командам uv. PostgreSQL не публикует порт на хост.
UBAS_HTTP_PORT меняет HTTP-порт (по умолчанию 8000).

Имя проекта — ubas-backend; PostgreSQL сохраняется в volume
ubas-backend_postgres_data. Перед обновлением остановите старые процессы:

```shell
docker compose --profile worker down
docker compose --profile worker up --build -d
```

Не добавляйте --volumes, если нужно сохранить БД.
Обе версии процессов должны обновляться вместе. Изменений схемы очереди в
переходе к новому MVP нет; старые UUID, ключи и результаты сохранены.
Старый демо-каталог пересоздайте явно: раньше в нём не было географии MOW/TYO.

## Запрос и фронтенд

```json
{
  "origin": "MOW",
  "destination": "TYO",
  "date_from": "2027-06-01",
  "date_to": "2027-06-30"
}
```

POST /api/v1/queries возвращает 202 с query_id. Клиент ждёт завершения через
GET /api/v1/queries/{id}, затем запрашивает /{id}/result.
Карточки появляются только после завершения всего поиска.
Незавершённый поиск — failed/search_incomplete, а не пустая успешная выдача.
Полный контракт версии 2 — [для фронта](docs/direct-routes.md).

Клиент на стандартной библиотеке Python:

```shell
python scripts/query_route.py
python scripts/query_route.py --origin MOW --destination TYO --date-from 2027-06-01 --date-to 2027-06-30
```

По умолчанию используется демо-окно через 14 дней. --base-url задаёт адрес API,
--timeout — время ожидания. Продолжить ожидание: --query-id UUID.
Ключ идемпотентности сохраняет повтор POST; после завершения новый поиск требует
нового ключа. JSON идёт в stdout, служебные сообщения — в stderr.

CORS по умолчанию разрешает http://localhost:5173 и http://127.0.0.1:5173.
Другие origin задаются списком UBAS_CORS_ORIGINS; пустое значение отключает CORS.
Фронт обращается к API на порту 8000, а не к собственному серверу.

## Конфигурация и проверки

UBAS_PLANNER_CONFIG_PATH — необязательный JSON, общий для API и worker.
Настройки: tourism_threshold, rub_rates, бюджеты вызовов/предложений/поиска.
Старые weights, price_references, квоты, буферы и предел 72 часа удалены.
По умолчанию RUB=1; реальные курсы автоматически не загружаются.
Подробнее — [routing.md](docs/routing.md).

```shell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked python -m unittest discover -s tests -v
```

PostgreSQL-проверки запускаются только с UBAS_TEST_DATABASE_URL на отдельной БД
ubas_test_* и очищают её очередь. Без настройки пропускаются.
CI содержит только Ruff (линтинг/формат) и mypy.

```shell
uv add <package>
uv add --dev <package>
uv build --no-sources
```

Alembic-ревизии входят в пакет и не требуют исходного checkout.
