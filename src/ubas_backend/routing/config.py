"""All planner and heuristic tuning lives in serializable configuration."""

from typing import Self

from pydantic import Field, model_validator

from ubas_backend.routing.schemas import (
    Code,
    Model,
    NonNegative,
    Positive,
    PositiveHours,
    UnitScore,
)


class ScoreWeights(Model):
    price: NonNegative = 0.40
    tourism: NonNegative = 0.25
    daytime: NonNegative = 0.15
    visa: NonNegative = 0.10
    duration: NonNegative = 0.05
    protection: NonNegative = 0.05

    @model_validator(mode="after")
    def nonzero_weights(self) -> Self:
        if sum(self.model_dump().values()) == 0:
            raise ValueError("At least one scoring weight must be positive")
        return self


class PlannerConfig(Model):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    # Fixed scales keep scores stable when unrelated alternatives are added.
    price_references: dict[Code, Positive] = Field(
        default_factory=lambda: {"RUB": 60_000.0}
    )
    duration_reference_hours: PositiveHours = 72
    target_daytime_hours: PositiveHours = 10
    self_transfer_penalty: UnitScore = 0.7
    airport_change_penalty: UnitScore = 0.3
    min_stopover_hours: PositiveHours = 8
    max_stopover_hours: PositiveHours = 72
    min_daytime_fraction: UnitScore = 0.5
    min_daytime_hours: NonNegative = 3
    min_tourism_score: UnitScore = 0.2
    arrival_buffer_hours: NonNegative = 2
    departure_buffer_hours: NonNegative = 2
    self_transfer_buffer_hours: NonNegative = 1
    airport_change_buffer_hours: NonNegative = 2
    daytime_start_hour: int = Field(ge=0, le=23, default=8)
    daytime_end_hour: int = Field(ge=1, le=24, default=22)
    allow_self_transfer: bool = True
    allow_airport_change: bool = True
    search_through_tickets: bool = True
    max_hubs: int = Field(ge=1, default=20)
    max_offers_per_query: int = Field(ge=1, default=100)
    max_candidates: int = Field(ge=1, default=20_000)
    max_concurrent_queries: int = Field(ge=1, default=8)
    query_timeout_seconds: float = Field(gt=0, default=15)
    max_search_days: int = Field(ge=1, default=31)
    max_results_per_city: int = Field(ge=1, default=3)

    @model_validator(mode="after")
    def valid_limits(self) -> Self:
        if not self.price_references:
            raise ValueError("Configure at least one currency price reference")
        if not (self.allow_self_transfer or self.search_through_tickets):
            raise ValueError("Enable at least one ticket search mode")
        if self.max_stopover_hours < self.min_stopover_hours:
            raise ValueError("Stopover bounds are reversed")
        if self.daytime_start_hour >= self.daytime_end_hour:
            raise ValueError("Daytime window must end after it starts")
        return self
