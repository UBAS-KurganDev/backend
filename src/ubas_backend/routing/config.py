"""Operational budgets and explicit MVP calibration; no ranking weights."""

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, model_validator

from ubas_backend.routing.schemas import Code, Model

type ExchangeRate = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class PlannerConfig(Model):
    # Temporary prototype value chosen by the project owner, not a calibrated metric.
    tourism_threshold: float = Field(ge=0, le=100, default=60)
    rub_rates: dict[Code, ExchangeRate] = Field(
        default_factory=lambda: {"RUB": Decimal(1)}
    )
    max_hubs: int = Field(ge=1, default=30)
    max_topology_cities: int = Field(ge=2, default=2000)
    max_topology_edges: int = Field(ge=1, default=100_000)
    max_offers_per_query: int = Field(ge=1, default=200)
    max_offers_per_search: int = Field(ge=1, default=2000)
    max_total_offers: int = Field(ge=1, default=50_000)
    max_provider_queries: int = Field(ge=1, default=4000)
    max_candidates: int = Field(ge=1, default=100_000)
    max_search_nodes: int = Field(ge=1, default=200_000)
    max_frontier: int = Field(ge=1, default=200_000)
    max_concurrent_queries: int = Field(ge=1, default=8)
    query_timeout_seconds: float = Field(gt=0, default=30)
    search_timeout_seconds: float = Field(gt=0, default=1200)

    @model_validator(mode="after")
    def rub_is_identity(self) -> Self:
        if self.rub_rates.get("RUB") != Decimal(1):
            raise ValueError("RUB exchange rate must be 1")
        return self
