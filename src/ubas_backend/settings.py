"""Explicit runtime configuration; secrets stay outside source control."""

import math
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ubas_backend.routing.config import PlannerConfig

DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def load_cors_origins() -> tuple[str, ...]:
    raw = os.getenv("UBAS_CORS_ORIGINS")
    if raw is None:
        return DEFAULT_CORS_ORIGINS
    return tuple(
        origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes"}:
        return True
    if raw.lower() in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    database_url: str
    catalog_path: Path | None
    flight_provider_factory: str
    embedded_worker: bool
    worker_concurrency: int
    worker_poll_interval: float
    worker_lease_duration: timedelta
    worker_retry_delay: timedelta
    worker_max_attempts: int
    worker_execution_timeout: float
    planner_config: PlannerConfig

    @classmethod
    def from_environment(cls, *, need_planner: bool | None = True) -> Settings:
        embedded = _flag("UBAS_EMBEDDED_WORKER", False)
        need_planner = embedded if need_planner is None else need_planner
        provider = os.getenv("UBAS_FLIGHT_PROVIDER_FACTORY", "")
        if need_planner and not provider:
            raise RuntimeError("UBAS_FLIGHT_PROVIDER_FACTORY must be configured")
        return cls(
            database_url=_required("UBAS_DATABASE_URL"),
            catalog_path=(
                Path(_required("UBAS_CATALOG_PATH")) if need_planner else None
            ),
            flight_provider_factory=provider,
            embedded_worker=embedded,
            worker_concurrency=_positive_int("UBAS_WORKER_CONCURRENCY", 2),
            worker_poll_interval=_positive_float("UBAS_WORKER_POLL_INTERVAL", 1),
            worker_lease_duration=timedelta(
                seconds=_positive_float("UBAS_WORKER_LEASE_SECONDS", 90)
            ),
            worker_retry_delay=timedelta(
                seconds=_positive_float("UBAS_WORKER_RETRY_SECONDS", 5)
            ),
            worker_max_attempts=_positive_int("UBAS_WORKER_MAX_ATTEMPTS", 3),
            worker_execution_timeout=_positive_float(
                "UBAS_WORKER_TIMEOUT_SECONDS", 300
            ),
            planner_config=load_planner_config(),
        )


def load_planner_config() -> PlannerConfig:
    path = os.getenv("UBAS_PLANNER_CONFIG_PATH")
    if path is None:
        return PlannerConfig()
    return PlannerConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))
