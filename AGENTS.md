# AGENTS.md

## Сервис

ubas-backend — учебный бэкенд для поиска транзитных маршрутов с туристической
оценкой городов. FastAPI-процесс принимает запросы и сохраняет их в Postgres;
фоновый worker строит маршруты по SQLite-каталогу городов и рейсов.

- Runtime: Python 3.14, FastAPI/uvicorn, pydantic v2, asyncpg, SQLAlchemy ORM + aiosqlite (каталог).
- Точки входа: `ubas-api`, `ubas-worker`, `ubas-migrate`, `ubas-import-cities`.

## Команды

```shell
uv sync                                                            # зависимости
uv run pytest                                                      # тесты
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                                                        # strict, src + tests
uv run ubas-migrate                                                # миграции Postgres
```

Тесты герметичные, инфраструктура не нужна: SQLite создаётся в `tmp_path`,
API тестируется через `TestClient` без lifespan (Postgres не поднимается),
внешний OpenTripMap API закрыт `httpx.MockTransport`.

## Индекс

- `src/ubas_backend/main.py` — FastAPI-приложение: lifespan, сборка рантайма.
- `src/ubas_backend/runtime.py` — композиционный корень (pool, worker, каталог).
- `src/ubas_backend/settings.py` — конфигурация из переменных окружения.
- `src/ubas_backend/api/` — HTTP-ручки: `router.py` (queries), `cities.py` (каталог городов, 503 без `UBAS_CATALOG_PATH`).
- `src/ubas_backend/query_manager/` — durable-запросы: `service.py`, `repository.py` (Postgres), `schemas.py`, `ports.py`.
- `src/ubas_backend/routing/` — домен маршрутизации: `planner.py`, `schemas.py`, `scoring.py`, `storage.py` (SqliteCatalog на SQLAlchemy ORM), `orm.py` (таблицы `city_profiles`/`visa_policies`/`flight_links`, схема v2 через `PRAGMA user_version`), `config.py`, `daytime.py`.
- `src/ubas_backend/import_cities.py` — CLI-импортёр туристических оценок из OpenTripMap.
- `src/ubas_backend/worker.py`, `migrate.py` — входные точки процессов.
- `migrations/` — SQL-миграции Postgres.
- `data/city_profiles.json` — проверяемый снапшот оценок городов (коммитится).
- `catalog.sqlite3` — генерируемый каталог, восстанавливается `ubas-import-cities --from-snapshot` (в `.gitignore`).
- `docs/` — `architecture.md`, `routing.md` (алгоритм и импорт городов), `query-manager.md`.
- `tests/` — `factories.py` (билдеры CityProfile), `conftest.py` (каталог-фикстура), `test_catalog.py`, `test_cities_api.py`, `test_import_cities.py`.

## Правила

- Сгенерированные файлы не редактировать напрямую.
- Тесты — только герметичные; внешние HTTP — через моки.
- Референсные данные (`data/`) не править под совместимость — оценки
  перегенерируются импортёром, а не руками.
