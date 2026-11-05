"""Scraper settings; environment is supplied by the launcher, not read on import."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UBAS_AGENT_", extra="ignore")

    headless: bool = True
    topology_snapshot_path: Path | None = None
    redis_url: SecretStr | None = None
    # Operational cache retention, not a promise that a fare is still available.
    cache_ttl_seconds: int = Field(default=1800, ge=1, le=172800)
    cache_max_entries: int = Field(default=128, ge=1, le=10000)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    timeout_seconds: float = Field(default=60, gt=0, le=180)
    max_response_batches: int = Field(default=100, ge=1, le=1000)
