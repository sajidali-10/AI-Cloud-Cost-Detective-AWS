"""Conversion helpers between cost service dataclasses and Pydantic schemas.

Phase 7B — moved out of the API route layer so non-route callers
(AI service, evidence builder, optimization aggregator) can reuse
the same conversion without importing from :mod:`app.api.aws_costs`.
The Phase 7A conversion that used to live as the private helper
``_to_pydantic_report`` in ``app/api/aws_costs.py`` was removed
when that route was refactored to use :class:`MultiAccountCostReport`
for the all-accounts path; this module restores the helper in the
service layer where it belongs.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.schemas.cost import (
    CostPeriod,
    CostReport,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)


def to_pydantic_cost_report(
    raw,
    *,
    account_id: str,
) -> CostReport:
    """Convert a :class:`app.services.aws.cost_explorer.CostReport`
    dataclass to the Pydantic :class:`app.schemas.cost.CostReport`.

    The conversion is deterministic and free of side-effects:

    * ``change_amount`` is computed from the current and previous
      totals, not stored on the dataclass.
    * ``change_percent`` is ``None`` when the previous total is
      zero (avoids a ``DivisionByZero`` leak into the schema).
    * ``account_id`` is required — the dataclass does not carry an
      attribution.  Callers MUST pass the account id explicitly so
      no caller can accidentally attribute Account-A evidence to a
      Account-B response.
    """
    period = CostPeriod(
        start=raw.period.start, end=raw.period.end, days=raw.period.days
    )
    previous = CostPeriod(
        start=raw.previous.start, end=raw.previous.end, days=raw.previous.days
    )
    change_amount = raw.current_total - raw.previous_total
    if raw.previous_total == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change_amount / raw.previous_total).quantize(
            Decimal("0.01")
        )
    payload: dict = dict(
        account_id=account_id,
        period=period,
        previous_period=previous,
        currency=raw.unit,
        total_cost=raw.current_total,
        previous_period_cost=raw.previous_total,
        change_amount=change_amount,
        change_percent=change_percent,
        estimated=raw.estimated,
        daily_trend=[
            DailyCostPoint(date=day, amount=amount, unit=unit)
            for (day, amount, unit) in raw.daily
        ],
        by_service=[
            ServiceCost(service=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_service
        ],
        by_region=[
            RegionCost(region=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_region
        ],
        source="AWS_COST_EXPLORER",
    )
    return CostReport(**payload)


__all__ = ["to_pydantic_cost_report"]
