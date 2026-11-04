"""Source requests and versioned cached observations, independent of routing models."""

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class SearchRequest(BaseModel):
    """One airport pair, one local departure date, one adult in economy."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    depart_date: date

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def normalize_iata(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def search_url(self) -> str:
        code = f"{self.depart_date:%d%m}{self.origin.lower()}{self.destination.lower()}"
        return f"https://www.agent.ru/air/avia-results/{code}%7Ca1E/?lang=ru&curr=RUB"

    @property
    def cache_key(self) -> str:
        return (
            f"ubas:agent:v2:{self.origin}:{self.destination}:"
            f"{self.depart_date.isoformat()}:E:1:0:0:RUB"
        )


class CapturedOffers(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[2] = 2
    fetched_at: AwareDatetime
    data: dict[str, Any]
