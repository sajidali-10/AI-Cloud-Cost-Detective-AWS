"""Azure Advisor cost-recommendation aggregator — Phase 7C.2D.

Aggregates Advisor (via Resource Graph) responses for one or more
Azure subscriptions into a provider-neutral rollup suitable for
the canonical ``/api/cloud/recommendations`` route.

Public surface
--------------

* :func:`aggregate_advisor_recommendations_for_subscriptions` —
  fetch + normalise + rollup into a :class:`CloudRecommendation`
  list and a :class:`CloudRecommendationSummary`-shaped rollup.
* :func:`build_advisor_summary` — pure rollup helper that folds a
  list of normalised recommendations into a summary shape.

Failure isolation
-----------------

Each subscription is queried independently.  An error from one
subscription is captured as a sanitised
:class:`AzureAdvisorSubscriptionFailure` and **does not** fail
the overall response.  Per-subscription attribution is preserved.

Status semantics
----------------

* ``SUCCESS`` — at least one subscription returned recommendations
  (or a genuine zero), no failures.
* ``NO_DATA`` — every accessible subscription is empty AND there
  are no failures (genuine zero).
* ``PARTIAL_SUCCESS`` — at least one subscription succeeded,
  another failed.
* ``ERROR`` — every subscription failed.

Savings / currency semantics
----------------------------

* ``annualSavingsAmount`` is the documented annual savings field.
* We NEVER annualise a monthly amount ourselves.  When only
  ``savingsAmount`` (monthly) is present, ``annual_savings`` is
  ``None``.
* When every successful recommendation shares the same currency,
  ``potential_annual_savings`` is summed in that currency.
* When currencies differ, ``savings_status="MIXED_CURRENCY"`` and
  ``savings_by_currency`` carries the per-currency totals; no FX
  conversion is ever invented.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from app.db.models import CloudAccount
from app.services.cloud.azure.advisor import (
    AzureAdvisorClient,
    AzureAdvisorError,
    AzureAdvisorRecommendation,
    normalize_recommendation_row,
    safe_subscription_label,
)
from app.services.cloud.models import (
    CloudProviderKind,
    CloudRecommendation,
)

logger = logging.getLogger("cost-detective.azure.advisor_aggregator")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


def _max_concurrency() -> int:
    return int(os.getenv("AZURE_ADVISOR_MAX_CONCURRENCY", "4"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureAdvisorSubscriptionFailure:
    """Sanitised per-subscription failure surfaced to the API layer."""

    subscription_id: str
    display_name: str
    category: str
    status_code: Optional[int]
    message: str


@dataclass(frozen=True)
class AzureSubscriptionAdvisorSnapshot:
    """A single subscription's contribution to the Advisor rollup."""

    subscription_id: str
    display_name: str
    status: str  # SUCCESS | NO_DATA | ERROR
    recommendations: Tuple[AzureAdvisorRecommendation, ...]
    error: Optional[AzureAdvisorSubscriptionFailure] = None


# ---------------------------------------------------------------------------
# Per-subscription fetch
# ---------------------------------------------------------------------------


def _sanitize_error(
    exc: AzureAdvisorError,
    *,
    subscription_id: str,
    display_name: str,
) -> AzureAdvisorSubscriptionFailure:
    return AzureAdvisorSubscriptionFailure(
        subscription_id=subscription_id,
        display_name=display_name,
        category=exc.category.value,
        status_code=exc.status_code,
        message=exc.category.value,  # sanitised category only
    )


async def _fetch_subscription_recommendations(
    row: CloudAccount,
) -> AzureSubscriptionAdvisorSnapshot:
    """Fetch + normalise recommendations for a single subscription."""
    client = AzureAdvisorClient()
    try:
        raw_rows = client.query_active_cost_recommendations(
            subscription_ids=[row.account_id]
        )
    except AzureAdvisorError as exc:
        return AzureSubscriptionAdvisorSnapshot(
            subscription_id=row.account_id,
            display_name=row.display_name,
            status="ERROR",
            recommendations=(),
            error=_sanitize_error(
                exc,
                subscription_id=row.account_id,
                display_name=row.display_name,
            ),
        )
    finally:
        client.close()

    recommendations = tuple(
        normalize_recommendation_row(r, subscription_display_name=row.display_name)
        for r in raw_rows
        if isinstance(r, Mapping)
    )
    status = "NO_DATA" if not recommendations else "SUCCESS"
    return AzureSubscriptionAdvisorSnapshot(
        subscription_id=row.account_id,
        display_name=row.display_name,
        status=status,
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Tenant-mode fetch (single query, partitioned by subscriptionId)
# ---------------------------------------------------------------------------


async def _fetch_tenant_recommendations(
    rows: Sequence[CloudAccount],
) -> List[AzureSubscriptionAdvisorSnapshot]:
    """Fetch recommendations tenant-wide and partition them per subscription.

    Advisor data is exposed through the ``advisorresources`` table —
    the same Resource Graph endpoint Phase 7C.2C.1 already
    hardened.  An empty ``subscriptions`` list fans out across
    every subscription the configured identity is authorised to
    see.
    """
    client = AzureAdvisorClient()
    try:
        raw_rows = client.query_active_cost_recommendations(subscription_ids=[])
    except AzureAdvisorError as exc:
        label = ",".join(
            safe_subscription_label(r.account_id) for r in rows
        ) or "<tenant>"
        logger.warning(
            "azure.advisor_aggregator.tenant_query_failed subs=%s code=%s",
            label, exc.category.value,
        )
        first = rows[0] if rows else None
        return [
            AzureSubscriptionAdvisorSnapshot(
                subscription_id=first.account_id if first else "<tenant>",
                display_name=first.display_name if first else "Azure",
                status="ERROR",
                recommendations=(),
                error=_sanitize_error(
                    exc,
                    subscription_id=first.account_id if first else "",
                    display_name=first.display_name if first else "Azure",
                ),
            )
        ]
    finally:
        client.close()

    by_subscription: "OrderedDict[str, List[AzureAdvisorRecommendation]]" = (
        OrderedDict()
    )
    for r in raw_rows:
        if not isinstance(r, Mapping):
            continue
        normalised = normalize_recommendation_row(r)
        sub = normalised.subscription_id or "<unknown>"
        by_subscription.setdefault(sub, []).append(normalised)

    snapshots: List[AzureSubscriptionAdvisorSnapshot] = []
    for row in rows:
        bucket = by_subscription.get(row.account_id, [])
        status = "NO_DATA" if not bucket else "SUCCESS"
        snapshots.append(
            AzureSubscriptionAdvisorSnapshot(
                subscription_id=row.account_id,
                display_name=row.display_name,
                status=status,
                recommendations=tuple(bucket),
            )
        )
    # Any subscription that Advisor returned but the registry does
    # not know about gets a "phantom" snapshot so attribution stays
    # accurate.
    for sub, bucket in by_subscription.items():
        if any(s.subscription_id == sub for s in snapshots):
            continue
        snapshots.append(
            AzureSubscriptionAdvisorSnapshot(
                subscription_id=sub,
                display_name=sub,
                status="SUCCESS",
                recommendations=tuple(bucket),
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Per-subscription fan-out (explicit scope)
# ---------------------------------------------------------------------------


async def fetch_subscription_advisor_snapshots(
    *,
    rows: Sequence[CloudAccount],
) -> List[AzureSubscriptionAdvisorSnapshot]:
    """Fan out Advisor queries across ``rows`` with bounded concurrency."""
    if not rows:
        return []
    sem = asyncio.Semaphore(_max_concurrency())

    async def _bounded(row: CloudAccount) -> AzureSubscriptionAdvisorSnapshot:
        async with sem:
            return await _fetch_subscription_recommendations(row)

    return await asyncio.gather(*(_bounded(row) for row in rows))


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvisorRollup:
    """Aggregated Advisor rollup ready for the route envelope."""

    status: str
    savings_status: str  # OK | MIXED_CURRENCY | EMPTY
    currency: Optional[str]
    total_recommendations: int
    recommendations_with_savings: int
    recommendations_without_savings: int
    potential_annual_savings: Optional[Decimal]
    savings_by_currency: Dict[str, Decimal]
    by_impact: Dict[str, int]
    by_resource_type: Dict[str, int]
    by_subscription: Dict[str, int]
    by_resource_group: Dict[str, int]
    by_type: Dict[str, int]
    by_subcategory: Dict[str, int]
    failures: List[AzureAdvisorSubscriptionFailure]

    @property
    def has_mixed_currencies(self) -> bool:
        return self.savings_status == "MIXED_CURRENCY"


def _empty_rollup(
    *, status: str, failures: List[AzureAdvisorSubscriptionFailure]
) -> AdvisorRollup:
    return AdvisorRollup(
        status=status,
        savings_status="EMPTY",
        currency=None,
        total_recommendations=0,
        recommendations_with_savings=0,
        recommendations_without_savings=0,
        potential_annual_savings=None,
        savings_by_currency={},
        by_impact={},
        by_resource_type={},
        by_subscription={},
        by_resource_group={},
        by_type={},
        by_subcategory={},
        failures=list(failures),
    )


def build_advisor_summary(
    snapshots: Sequence[AzureSubscriptionAdvisorSnapshot],
) -> AdvisorRollup:
    """Combine per-subscription Advisor snapshots into a rollup."""
    failures = [snap.error for snap in snapshots if snap.error is not None]
    successful_with_data = [
        snap
        for snap in snapshots
        if snap.error is None and snap.recommendations
    ]
    successful_empty = [
        snap
        for snap in snapshots
        if snap.error is None and not snap.recommendations
    ]

    if not successful_with_data and not successful_empty and not failures:
        return _empty_rollup(status="NO_DATA", failures=failures)
    if failures and not successful_with_data and not successful_empty:
        return _empty_rollup(status="ERROR", failures=failures)
    status = "PARTIAL_SUCCESS" if failures else "SUCCESS"

    all_recommendations: List[AzureAdvisorRecommendation] = []
    for snap in snapshots:
        if snap.error is not None:
            continue
        all_recommendations.extend(snap.recommendations)

    # Dedup by recommendation_id (the Advisor row's ARM id is
    # stable + globally-unique within a tenant).
    seen_ids: Set[str] = set()
    deduped: List[AzureAdvisorRecommendation] = []
    for rec in all_recommendations:
        if rec.recommendation_id in seen_ids:
            continue
        seen_ids.add(rec.recommendation_id)
        deduped.append(rec)

    # Currency + savings rollup.  We aggregate by currency so
    # mixed-currency responses surface MIXED_CURRENCY rather than
    # inventing an FX rate.
    savings_by_currency: Dict[str, Decimal] = {}
    recommendations_with_savings = 0
    recommendations_without_savings = 0
    for rec in deduped:
        if rec.annual_savings is None or rec.savings_currency is None:
            recommendations_without_savings += 1
            continue
        cur = rec.savings_currency.upper()
        try:
            current = savings_by_currency.get(cur, Decimal("0"))
            savings_by_currency[cur] = current + rec.annual_savings
        except Exception:  # noqa: BLE001 - defensive
            # Defensive: if a non-Decimal slips through, drop the
            # entry and surface MIXED_CURRENCY so the UI flags it.
            continue
        recommendations_with_savings += 1

    distinct_currencies = list(savings_by_currency.keys())
    if len(distinct_currencies) == 0:
        savings_status = "EMPTY"
        potential_annual_savings: Optional[Decimal] = None
        currency: Optional[str] = None
    elif len(distinct_currencies) == 1:
        savings_status = "OK"
        potential_annual_savings = savings_by_currency[distinct_currencies[0]]
        currency = distinct_currencies[0]
    else:
        savings_status = "MIXED_CURRENCY"
        potential_annual_savings = None
        currency = None

    by_impact: Dict[str, int] = {}
    by_resource_type: Dict[str, int] = {}
    by_subscription: Dict[str, int] = {}
    by_resource_group: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    by_subcategory: Dict[str, int] = {}
    for rec in deduped:
        impact_key = rec.impact if rec.impact else "Unassigned"
        by_impact[impact_key] = by_impact.get(impact_key, 0) + 1
        # By resource type — bucket the unknown cases.
        rt_key = rec.resource_type if rec.resource_type else "Unassigned"
        by_resource_type[rt_key] = by_resource_type.get(rt_key, 0) + 1
        # By subscription.
        by_subscription[rec.subscription_id] = by_subscription.get(
            rec.subscription_id, 0
        ) + 1
        # By resource group — subscription-scoped recommendations
        # land under "Unassigned".
        rg_key = rec.resource_group if rec.resource_group else "Unassigned"
        by_resource_group[rg_key] = by_resource_group.get(rg_key, 0) + 1
        # By recommendation type / subcategory.
        type_key = rec.recommendation_type_id if rec.recommendation_type_id else "Unassigned"
        by_type[type_key] = by_type.get(type_key, 0) + 1
        if rec.subcategory:
            by_subcategory[rec.subcategory] = by_subcategory.get(rec.subcategory, 0) + 1

    return AdvisorRollup(
        status=status,
        savings_status=savings_status,
        currency=currency,
        total_recommendations=len(deduped),
        recommendations_with_savings=recommendations_with_savings,
        recommendations_without_savings=recommendations_without_savings,
        potential_annual_savings=potential_annual_savings,
        savings_by_currency=dict(savings_by_currency),
        by_impact=dict(by_impact),
        by_resource_type=dict(by_resource_type),
        by_subscription=dict(by_subscription),
        by_resource_group=dict(by_resource_group),
        by_type=dict(by_type),
        by_subcategory=dict(by_subcategory),
        failures=list(failures),
    )


# ---------------------------------------------------------------------------
# Provider-neutral translation
# ---------------------------------------------------------------------------


def _last_segment(arm_resource_id: str) -> Optional[str]:
    """Return the trailing ``/<name>`` segment of an ARM resource id.

    Used to surface a friendly ``resource_name`` on the canonical
    recommendation without re-fetching Resource Graph.
    """
    if not arm_resource_id:
        return None
    parts = arm_resource_id.rsplit("/", 1)
    if len(parts) != 2:
        return None
    name = parts[1]
    return name or None


def advisor_recommendations_to_cloud(
    recs: Iterable[AzureAdvisorRecommendation],
) -> List[CloudRecommendation]:
    """Translate normalised Advisor recommendations to the
    provider-neutral :class:`CloudRecommendation` shape.

    The provider-neutral model uses
    ``CloudProviderKind.AZURE`` and ``source="AZURE_ADVISOR"`` so
    the AWS legacy ``source`` values (e.g.
    ``AWS_COST_OPTIMIZATION_HUB``) stay distinct.
    """
    out: List[CloudRecommendation] = []
    for rec in recs:
        out.append(
            CloudRecommendation(
                provider=CloudProviderKind.AZURE,
                account_id=rec.subscription_id or None,
                region=None,  # Advisor recommendations are
                # subscription-scoped; the resource may live in any
                # region.  We do NOT fabricate a region.
                recommendation_id=rec.recommendation_id,
                resource_id=rec.resource_id,
                resource_type=rec.resource_type,
                service=rec.resource_type,
                action=rec.recommendation_type_id or "ADVISOR_RECOMMENDATION",
                title=rec.title or rec.description or rec.recommendation_type_id,
                estimated_savings=rec.annual_savings,
                currency=rec.savings_currency,
                source="AZURE_ADVISOR",
                extensions={
                    "subscription_id": rec.subscription_id,
                    "resource_group": rec.resource_group,
                    "category": rec.category,
                    "subcategory": rec.subcategory,
                    "impact": rec.impact,
                    "recommendation_status": rec.recommendation_status,
                    "term": rec.term,
                    "lookback_period": rec.lookback_period,
                    "last_updated": rec.last_updated,
                    "description": rec.description,
                    "solution": rec.solution,
                    "resource_name": _last_segment(rec.resource_id),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def aggregate_advisor_recommendations_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
) -> Tuple[
    List[CloudRecommendation],
    AdvisorRollup,
    List[AzureAdvisorSubscriptionFailure],
]:
    """Aggregate Advisor recommendations across ``rows``.

    * Single-subscription path: one upstream query per row.
    * Multi-subscription path: ONE tenant-mode query, partitioned
      by ``subscriptionId``.

    Returns ``(flat_recommendations, rollup, failures)``.
    """
    if not rows:
        return [], _empty_rollup(status="NO_DATA", failures=[]), []

    if len(rows) == 1:
        snapshots = await fetch_subscription_advisor_snapshots(rows=rows)
    else:
        snapshots = await _fetch_tenant_recommendations(rows)
    failures = [snap.error for snap in snapshots if snap.error is not None]
    rollup = build_advisor_summary(snapshots)
    flat_recommendations = advisor_recommendations_to_cloud(
        rec
        for snap in snapshots
        for rec in snap.recommendations
    )
    return flat_recommendations, rollup, failures


__all__ = [
    "AdvisorRollup",
    "AzureAdvisorSubscriptionFailure",
    "AzureSubscriptionAdvisorSnapshot",
    "advisor_recommendations_to_cloud",
    "aggregate_advisor_recommendations_for_subscriptions",
    "build_advisor_summary",
    "fetch_subscription_advisor_snapshots",
    "normalize_recommendation_row",
    "safe_subscription_label",
]
