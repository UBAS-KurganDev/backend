# UBAS Backend

Python-бэкенд UBAS: подбор маршрутов с туристической остановкой до 3 дней.
Исходный код расположен в `src/ubas_backend`.

Планировщик сравнивает единые и отдельные билеты, учитывает стоимость въездной визы,
дневное время для прогулок, привлекательность города и риск стыковки.
Есть SQLite-каталог топологии/городов и заменяемые интерфейсы провайдера и рейтинга.
Реальный поставщик билетов и HTTP API пока не подключены.

```shell
uv sync --locked
uv run python -m ubas_backend.routing.demo
```

Пример использует только вымышленные данные. [Контракты, алгоритм и настройки](docs/routing.md).

## Стек

- Python 3.14.
- [uv](https://docs.astral.sh/uv/) — зависимости, виртуальное окружение и сборка.
- [Ruff](https://docs.astral.sh/ruff/) — линтинг, сортировка импортов и форматирование.
- [mypy](https://mypy.readthedocs.io/) — строгая статическая проверка типов.

## Быстрый старт

Установите uv по [официальной инструкции](https://docs.astral.sh/uv/getting-started/installation/).
Затем выполните:

```shell
git clone https://github.com/hardPrecision/ubas-backend.git
cd ubas-backend
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

Настройки инструментов находятся в `pyproject.toml`. mypy проверяет `src`
в строгом режиме; при добавлении тестов включите их каталог в `tool.mypy.files`.

## Зависимости

```shell
uv add <package>
uv add --dev <package>
uv remove <package>
```

Команды обновляют `pyproject.toml` и `uv.lock`; оба файла нужно коммитить.
Для целевого обновления зависимости используйте `uv lock --upgrade-package <package>`,
после чего выполните `uv sync --locked` и проверки.

## Сборка и CI

```shell
uv build --no-sources
```

Архив исходников и wheel создаются в `dist/`.
GitHub Actions при каждом push и pull request устанавливает зависимости по lock-файлу,
запускает Ruff и mypy, затем собирает пакет.
