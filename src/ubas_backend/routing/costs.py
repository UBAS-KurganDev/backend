"""Known RUB amounts and explicit unknown components; no eligibility checks."""

from decimal import ROUND_HALF_UP, Decimal

from ubas_backend.routing.config import PlannerConfig
from ubas_backend.routing.schemas import (
    CityProfile,
    CostComponent,
    FlightSegment,
    Money,
    TicketOffer,
    VisaExpense,
)


class CostCalculator:
    def __init__(self, config: PlannerConfig) -> None:
        self.rates = config.rub_rates

    def component(
        self,
        key: str,
        amount: Money | None,
        *,
        visa: bool = False,
        unknown_reason: str | None = None,
    ) -> CostComponent:
        rate = self.rates.get(amount.currency) if amount else None
        rub = (
            (amount.amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount is not None and rate is not None
            else None
        )
        reason = unknown_reason
        if amount is None:
            reason = reason or "amount_unknown"
        elif rate is None:
            reason = f"exchange_rate_unknown:{amount.currency}"
        return CostComponent(
            key=key,
            kind="visa" if visa else "ticket",
            original=amount,
            rub_amount=rub,
            unknown_reason=reason,
        )

    def ticket_amount(self, ticket: TicketOffer) -> Decimal:
        # Used by the lower bound too: missing costs contribute zero, without a penalty.
        return self.component("ticket", ticket.total_price).rub_amount or Decimal(0)


class CatalogVisaCosts:
    """Conservative catalog adapter until OPEN-06 rules are agreed.

    Catalog fees describe one intermediate-country visit. Repeated-country visits
    remain explicitly unknown; a route-aware integration can replace this adapter.
    Destination visa rules are not inferred from intermediate-city policies.
    """

    def expenses(
        self,
        segments: tuple[FlightSegment, ...],
        profiles: dict[str, CityProfile],
    ) -> tuple[VisaExpense, ...]:
        expenses: list[VisaExpense] = []
        seen: set[str] = set()
        for index, segment in enumerate(segments[:-1]):
            code = segment.destination.city_code
            profile = profiles.get(code)
            country = profile.country_code if profile else code
            key = f"visa:{index}:{country}"
            if country in seen:
                expenses.append(
                    VisaExpense(key=key, unknown_reason="repeat_country_rules_unknown")
                )
                continue
            seen.add(country)
            policy = (
                next((item for item in profile.visas if item.citizenship == "RU"), None)
                if profile
                else None
            )
            expenses.append(
                VisaExpense(
                    key=key,
                    amount=policy.fee_per_person if policy else None,
                    unknown_reason=None
                    if policy and policy.fee_per_person is not None
                    else "required_visa_cost_unknown",
                )
            )
        return tuple(expenses)
