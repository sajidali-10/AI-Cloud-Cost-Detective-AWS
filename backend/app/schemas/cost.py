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
    """How a given ``CostReportResponse`` was served by the cache layer.

    Phase 7D.3.3 adds ``STALE`` to the enum so the Azure cache
    layer can surface a last-known-good fallback.  The value is
    additive — every existing consumer that checked
    ``HIT``/``MISS``/``REFRESHED`` continues to work; ``STALE``
    is a new value that an explicitly-upgraded consumer (the
    frontend readiness derivation) maps to the "Stale" UI label.
    """

    HIT = "HIT"
    MISS = "MISS"
    REFRESHED = "REFRESHED"
    STALE = "STALE"


class DataFreshness(str, Enum):
    """Phase 7D.3.3 — coarse freshness signal the frontend renders.

    Distinct from :class:`CacheStatus` because HIT and REFRESHED
    both produce LIVE data; only STALE / MISS produce non-LIVE.
    AWS consumers that do not yet emit this field default to
    ``LIVE`` so the additive change is fully backward compatible.
    """

    LIVE = "LIVE"
    CACHED = "CACHED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


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


# ---------------------------------------------------------------------------
# Phase 7B — multi-account aggregate response.
# ---------------------------------------------------------------------------


class AccountCostShare(BaseModel):
    """A single account's contribution to the rollup."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    amount: Decimal


class AccountCostError(BaseModel):
    """A sanitized per-account failure surfaced in the aggregate."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    code: str
    message: str


class MultiAccountCostReport(BaseModel):
    """The full multi-account Cost Explorer response.

    Math is always deterministic:

    * ``total_cost = sum(per-account totals)`` (no averaging)
    * ``by_service`` sums identical service categories across accounts
    * ``by_region`` sums identical region keys across accounts
    * ``daily_trend`` aligns by date and fills missing days with zero
    * ``by_account`` preserves per-account totals for drill-down
    """

    model_config = ConfigDict(frozen=True)

    scope: str = Field(default="all", description="Either 'all' or 'acct:<id>'.")
    status: str = Field(description="SUCCESS | PARTIAL_SUCCESS | ERROR")
    account_id: Optional[str] = Field(
        default=None,
        description=(
            "Set when scope='acct:<id>'.  When scope='all' the value is "
            "the management account ID for backwards-compatibility."
        ),
    )
    account_display_name: Optional[str] = Field(default=None)
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
    by_account: List[AccountCostShare] = Field(default_factory=list)
    account_errors: List[AccountCostError] = Field(default_factory=list)
    source: str = "AWS_COST_EXPLORER"


class MultiAccountCostReportResponse(BaseModel):
    """The wire envelope for a multi-account aggregate."""

    model_config = ConfigDict(frozen=True)

    report: MultiAccountCostReport
    cache_status: CacheStatus
    cached_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Phase 7C.2B — provider-neutral Cloud cost envelope.
# ---------------------------------------------------------------------------


class CloudCostAccountShare(BaseModel):
    """A single subscription's contribution to a Cloud cost rollup.

    For AWS rows ``account_id`` carries the 12-digit AWS account ID.
    For Azure rows it carries the subscription GUID.  ``display_name``
    is the operator-supplied label.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    amount: Decimal
    currency: Optional[str] = None


class CloudCostAccountError(BaseModel):
    """A sanitised per-scope failure surfaced in the aggregate.

    For Azure rows ``account_id`` carries the subscription GUID.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    code: str
    message: str


class CloudCostReport(BaseModel):
    """The provider-neutral multi-account cost envelope.

    ``provider`` is one of ``"AWS"`` / ``"AZURE"`` (future
    ``"RACKSPACE"``).  ``scope_kind`` distinguishes
    ``"all"`` / ``"single"`` so the frontend can render
    "All Accounts" / "Account X" / "All Subscriptions" /
    "Subscription X" without re-parsing the scope token.

    ``by_account`` doubles as ``by_subscription`` for Azure —
    the shape is identical (id, display_name, amount).  We do
    NOT add a separate ``by_subscription`` field; the model
    stays single-shape so the shared UI does not need provider-
    specific branching.

    ``currency_status`` is ``"OK"`` / ``"MIXED_CURRENCY"`` /
    ``"EMPTY"`` / ``"NO_DATA"``.  ``MIXED_CURRENCY`` is a
    *distinct* state from ``OK`` — the totals are zeroed and
    the UI must render an explicit "mixed currency" message
    instead of inventing an FX rate.

    ``status`` is the same ``SUCCESS`` / ``PARTIAL_SUCCESS`` /
    ``ERROR`` vocabulary the Phase 7B AWS route uses.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(description="AWS | AZURE | RACKSPACE")
    scope: str = Field(description="all | acct:<id> | sub:<id>")
    scope_kind: str = Field(description="all | single")
    status: str
    currency_status: str = "OK"
    account_id: Optional[str] = None
    account_display_name: Optional[str] = None
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
    by_account: List[CloudCostAccountShare] = Field(default_factory=list)
    account_errors: List[CloudCostAccountError] = Field(default_factory=list)
    source: str = "CLOUD_COST"

    # ------------------------------------------------------------------
    # Phase 7D.3.3 — additive completeness + freshness metadata.
    # All Optional so existing AWS clients that do not populate them
    # continue to deserialise without changes.
    # ------------------------------------------------------------------

    #: Number of subscriptions / accounts that returned real cost
    #: data (already populated in Phase 7D.3.2; duplicated here so
    #: the canonical envelope is self-describing without an
    #: adapter-specific projection).
    successful_accounts: Optional[int] = None

    #: Number of subscriptions / accounts that returned an error
    #: (THROTTLED / AUTH / ...).
    failed_accounts: Optional[int] = None

    #: Number of Azure Cost Management POSTs in the last successful
    #: refresh (BEFORE/AFTER measurement).  Zero for AWS responses.
    azure_post_count: Optional[int] = None


class CloudCostReportResponse(BaseModel):
    """The wire envelope for a provider-neutral cost report."""

    model_config = ConfigDict(frozen=True)

    report: CloudCostReport
    cache_status: CacheStatus
    cached_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Phase 7D.3.3 — additive cache-freshness metadata.
    # All Optional so existing clients that ignore the new fields
    # continue to deserialise without changes.
    # ------------------------------------------------------------------

    #: Coarse freshness signal the frontend renders.  ``None`` for
    #: clients that do not yet populate it (treated as ``LIVE``).
    data_freshness: Optional["DataFreshness"] = None

    #: Timestamp of the most recent successful upstream call.
    #: Populated by the Azure cache layer; ``None`` for AWS or for
    #: legacy envelopes.
    last_successful_at: Optional[datetime] = None


__all__ = [
    "AccountCostError",
    "AccountCostShare",
    "CacheStatus",
    "CloudCostAccountError",
    "CloudCostAccountShare",
    "CloudCostReport",
    "CloudCostReportResponse",
    "CostPeriod",
    "DataFreshness",
    "CostReport",
    "CostReportResponse",
    "DailyCostPoint",
    "MultiAccountCostReport",
    "MultiAccountCostReportResponse",
    "RegionCost",
    "ServiceCost",
]
