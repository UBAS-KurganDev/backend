# syntax=docker/dockerfile:1
FROM python:3.14-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 10001 ubas \
    && useradd --uid 10001 --gid ubas --no-create-home \
        --shell /usr/sbin/nologin ubas

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.py /app/docker-entrypoint.py

USER ubas
EXPOSE 8000

ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["--migrate", "uvicorn", "ubas_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
