"""Scraper settings; environment is supplied by the launcher, not read on import."""

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UBAS_AGENT_", extra="ignore")

    headless: bool = True
    topology_snapshot_path: Path | None = None
    redis_url: SecretStr | None = None
    # http: pure-HTTP agent.ru transport with automatic browser fallback.
    transport: Literal["browser", "http"] = "browser"
    # Operational cache retention, not a promise that a fare is still available.
    cache_ttl_seconds: int = Field(default=1800, ge=1, le=172800)
    cache_max_entries: int = Field(default=128, ge=1, le=10000)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    # Three shorter attempts fit into the planner's per-city query budget.
    timeout_seconds: float = Field(default=30, gt=0, le=180)
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_response_batches: int = Field(default=100, ge=1, le=1000)

    # Empty keeps the original in-process scraper; otherwise the provider fans
    # airport pairs out to these independent HTTP nodes.
    node_urls: Annotated[tuple[str, ...], NoDecode] = ()
    node_concurrency: int = Field(default=2, ge=1, le=32)
    node_timeout_seconds: float = Field(default=150, gt=0, le=600)
    node_max_attempts: int = Field(default=2, ge=1, le=5)
    node_token: SecretStr | None = None
    node_host: str = "127.0.0.1"
    node_port: int = Field(default=8101, ge=1, le=65535)

    @field_validator("topology_snapshot_path", mode="before")
    @classmethod
    def empty_snapshot_path(cls, value: object) -> object:
        # Compose passes an empty value when this optional path is not configured.
        return None if value == "" else value

    @field_validator("node_token", mode="before")
    @classmethod
    def empty_node_token(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("node_urls", mode="before")
    @classmethod
    def split_node_urls(cls, value: object) -> object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("node_urls")
    @classmethod
    def validate_node_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for url in value:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("node URLs must use http(s) with a host")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("node URLs must not contain a path, query or fragment")
            normalized.append(url.rstrip("/"))
        return tuple(normalized)
