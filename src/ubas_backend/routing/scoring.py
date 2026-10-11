"""Explainable weighted utilities, each normalized into [0, 1]."""

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.schemas import RouteCandidate, RouteScore, ScoreComponent


class WeightedScorer:
    def score(self, route: RouteCandidate, config: PlannerConfig) -> RouteScore:
        metrics = route.metrics
        visit = metrics.daytime_hours
        tourism = (
            route.stopover.tourism_score
            * min(visit / config.target_daytime_hours, 1)
            * min(route.stopover.rewarding_visit_hours / max(visit, 0.001), 1)
        )
        protection = max(
            0.0,
            1
            - config.self_transfer_penalty * metrics.self_transfer
            - config.airport_change_penalty * metrics.airport_change,
        )
        values = {
            "price": 1 / (1 + float(route.total_cost.amount) / config.price_reference),
            "tourism": tourism,
            "daytime": metrics.daytime_fraction
            * min(visit / config.target_daytime_hours, 1),
            "visa": route.visa.ease,
            "duration": 1
            / (1 + metrics.total_trip_hours / config.duration_reference_hours),
            "protection": protection,
        }
        weights: dict[str, float] = config.weights.model_dump()
        total_weight = sum(weights.values())
        components = tuple(
            ScoreComponent(
                name=name,
                value=value,
                weight=weights[name],
                contribution=100 * value * weights[name] / total_weight,
            )
            for name, value in values.items()
        )
        return RouteScore(
            total=min(100, sum(item.contribution for item in components)),
            components=components,
        )
