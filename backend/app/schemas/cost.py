"""Pydantic schemas for the Cost Explorer API surface — Phase 2.

All monetary amounts are typed as ``Decimal`` end-to-end.  We never
let a ``float`` sneak into the response because binary-floating-point
arithmetic would silently corrupt totals (e.g. ``0.1 + 0.2 != 0.3``).

``model_config`` disables ORM-mode so the route layer is responsible
for serializing the ``CostReport`` dataclass into these Pydantic
shapes (the boundary is intentionally explicit).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CacheStatus(str, Enum):
    """How a given ``CostReportResponse`` was served by the cache layer."""

    HIT = "HIT"
    MISS = "MISS"
    REFRESHED = "REFRESHED"


# ---------------------------------------------------------------------------
# Component models
# ---------------------------------------------------------------------------


class CostPeriod(BaseModel):
    """A UTC cost period: ``start`` inclusive, ``end`` exclusive."""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    days: int = Field(ge=1, le=365)


class DailyCostPoint(BaseModel):
    """One day's spend for the daily-trend series."""

    model_config = ConfigDict(frozen=True)

    date: date
    amount: Decimal
    unit: str = "USD"


class ServiceCost(BaseModel):
    """Spend grouped by AWS service name."""

    model_config = ConfigDict(frozen=True)

    service: str
    amount: Decimal
    unit: str = "USD"


class RegionCost(BaseModel):
    """Spend grouped by AWS region.

    ``region`` may be the literal string ``"no_region"`` for global
    services (e.g. Route 53, IAM) that do not have a meaningful
    region dimension.
    """

    model_config = ConfigDict(frozen=True)

    region: str
    amount: Decimal
    unit: str = "USD"


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


class CostReport(BaseModel):
    """The full Cost Explorer response for a given period.

    ``change_amount`` is computed as ``current - previous``; both
    components carry the same unit (usually ``USD``).

    ``change_percent`` is computed as ``change_amount / previous``
    when ``previous > 0``; when ``previous == 0`` it is reported as
    ``None`` (NOT infinity, NOT a divide-by-zero exception) so the
    frontend can decide how to display the edge case.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    period: CostPeriod
    previous_period: CostPeriod
    currency: str = "USD"
    total_cost: Decimal
    previous_period_cost: Decimal
    change_amount: Decimal
    change_percent: Optional[Decimal]
    estimated: bool = False
    daily_trend: List[DailyCostPoint]
    by_service: List[ServiceCost]
    by_region: List[RegionCost]
    source: str = "AWS_COST_EXPLORER"


class CostReportResponse(BaseModel):
    """The wire envelope — ``CostReport`` plus the cache envelope."""

    model_config = ConfigDict(frozen=True)

    report: CostReport
    cache_status: CacheStatus
    cached_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


__all__ = [
    "CacheStatus",
    "CostPeriod",
    "CostReport",
    "CostReportResponse",
    "DailyCostPoint",
    "RegionCost",
    "ServiceCost",
]
