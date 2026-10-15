# UBAS Backend

Python-бэкенд UBAS: подбор маршрутов с туристической остановкой до 3 дней.
Исходный код расположен в `src/ubas_backend`.

Планировщик сравнивает единые и отдельные билеты, учитывает стоимость въездной визы,
дневное время для прогулок, привлекательность города и риск стыковки.
Есть SQLite-каталог топологии/городов и заменяемые интерфейсы провайдера и рейтинга.
HTTP API с тремя ручками запросов уже доступен; реальный поставщик билетов пока
не подключён.

```shell
uv sync --locked
uv run python -m ubas_backend.routing.demo
```

Пример использует только вымышленные данные. Документация:

- [контракт билетов, настройки и запуск](docs/routing.md);
- [устройство, логика работы и алгоритмы](docs/architecture.md).

## Запуск API

После `uv sync --locked` запустите сервер:

```shell
uv run ubas-api
```

API будет доступен на `http://127.0.0.1:8000`, интерактивная документация — на
`http://127.0.0.1:8000/docs`.

Для разработки с автоматической перезагрузкой:

```shell
uv run uvicorn ubas_backend.main:app --reload
```

Также работает запуск модуля:

```shell
uv run python -m ubas_backend.main
```

## Стек

- Python 3.14.
- [uv](https://docs.astral.sh/uv/) — зависимости, виртуальное окружение и сборка.
- [Ruff](https://docs.astral.sh/ruff/) — линтинг, сортировка импортов и форматирование.
- [mypy](https://mypy.readthedocs.io/) — строгая статическая проверка типов.

## Быстрый старт

Установите uv по [официальной инструкции](https://docs.astral.sh/uv/getting-started/installation/).
Затем выполните:

```shell
git clone https://github.com/UBAS-KurganDev/backend.git
cd backend
uv python install
uv sync --locked
```

`uv sync --locked` создаёт `.venv` и устанавливает проект вместе с зависимостями
разработки из `uv.lock`. Команды через `uv run` не требуют ручной активации окружения.

## Проверки

```shell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

Автоматическое исправление поддерживаемых ошибок и форматирование:

```shell
uv run --locked ruff check --fix .
uv run --locked ruff format .
```

Настройки Ruff и mypy находятся в `pyproject.toml`.

## Зависимости

```shell
uv add <package>
uv add --dev <package>
uv remove <package>
```

Команды обновляют `pyproject.toml` и `uv.lock`; оба файла нужно коммитить.
Для целевого обновления зависимости используйте `uv lock --upgrade-package <package>`,
после чего выполните `uv sync --locked` и проверки.

## CI

GitHub Actions при каждом push и pull request устанавливает зависимости по lock-файлу
и запускает Ruff (линтинг и проверку форматирования) и mypy. Тесты и сборка пока
не запускаются.
