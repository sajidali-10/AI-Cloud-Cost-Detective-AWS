"""AWS canonical recommendation aggregator — Phase 7D.1.

Mirrors :mod:`app.services.cloud.azure.advisor_aggregator` for the
AWS side of the provider-neutral ``/api/cloud/recommendations``
route.  It re-uses — never re-implements — the Phase 7B multi-account
optimization aggregation logic in
:mod:`app.services.aws.optimization_aggregator` (Compute Optimizer +
Cost Optimization Hub + deterministic engine) and folds the result
into a provider-neutral :class:`CloudRecommendation` list plus a
rollup suitable for the canonical envelope.

Design rules (inherited from the Azure aggregator):

* **No duplicate implementation.**  The per-account fan-out, dedup
  merge, source precedence, ContextVar account routing and partial
  failure isolation all live in the Phase 7B aggregator.  This module
  only translates its output.
* **No fabricated recommendations.**  A recommendation without a
  provider-reported savings figure keeps ``estimated_savings=None``.
* **Currency-safe.**  AWS reports monthly savings in a single
  currency per recommendation.  We group by currency and surface
  ``MIXED_CURRENCY`` rather than inventing an FX rate.
* **Sanitised errors only.**  Per-account failures come from the
  Phase 7B aggregator already reduced to ``{code, message}`` — no
  raw Boto3 payload, no credentials.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Set, Tuple

from app.db.models import CloudAccount
from app.schemas.optimization import (
    Confidence,
    Recommendation,
    SavingsSource,
)
from app.services.aws.cost_aggregator import AccountError
from app.services.aws.optimization_aggregator import (
    MultiAccountOptimizationReport,
    aggregate_recommendations_for_context,
)
from app.services.cloud.models import (
    CloudProviderKind,
    CloudRecommendation,
)

logger = logging.getLogger("cost-detective.cloud.aws.recommendation_aggregator")


# AWS reports a *monthly* savings figure (``estimated_monthly_savings``)
# whereas Azure Advisor reports an *annual* figure.  The canonical
# envelope carries an explicit ``savings_period`` so a consumer never
# confuses the two.
AWS_SAVINGS_PERIOD = "MONTHLY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimal(value: object) -> Decimal:
    """Coerce ``value`` to Decimal without binary-float corruption."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _enum_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def recommendation_to_cloud(rec: Recommendation) -> CloudRecommendation:
    """Translate a Phase 7B :class:`Recommendation` into the
    provider-neutral :class:`CloudRecommendation` shape.

    Provider-native fields that do not fit the canonical model
    (``confidence``, ``data_quality``, ``savings_percentage``,
    ``reason_codes``, ``finding``, ``restart_needed``,
    ``rollback_possible``) are preserved under ``extensions`` so no
    information is lost.
    """
    savings = rec.estimated_monthly_savings
    resource_type = _enum_value(rec.resource_type)
    action = _enum_value(rec.action)
    return CloudRecommendation(
        provider=CloudProviderKind.AWS,
        account_id=rec.account_id,
        region=rec.region,
        recommendation_id=rec.recommendation_id,
        resource_id=rec.resource_id,
        resource_type=resource_type,
        service=resource_type,
        action=action,
        title=rec.title,
        category=action,
        description=rec.finding,
        impact=_enum_value(rec.confidence),
        estimated_savings=_decimal(savings) if savings is not None else None,
        currency=rec.currency or "USD",
        source=_enum_value(rec.primary_source),
        extensions={
            "account_display_name": getattr(rec, "account_display_name", None),
            "confidence": _enum_value(rec.confidence),
            "data_quality": rec.data_quality,
            "savings_percentage": float(rec.savings_percentage)
            if rec.savings_percentage is not None
            else None,
            "restart_needed": rec.restart_needed,
            "rollback_possible": rec.rollback_possible,
            "reason_codes": list(rec.reason_codes),
            "finding": rec.finding,
            "sources": [_enum_value(s) for s in rec.sources],
            "savings_source": _enum_value(rec.savings_source),
            "savings_period": AWS_SAVINGS_PERIOD,
        },
    )


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AwsRecommendationRollup:
    """Aggregated AWS recommendation rollup ready for the route envelope.

    Provider-neutral in shape but populated from AWS-native output.
    ``by_account`` preserves per-account attribution (the AWS analog
    of Azure's ``by_subscription``); ``account_errors`` preserves
    sanitised per-account failures (the AWS analog of Azure's
    ``subscription_errors``).
    """

    status: str  # SUCCESS | PARTIAL_SUCCESS | NO_DATA | ERROR
    savings_status: str  # OK | MIXED_CURRENCY | EMPTY
    currency: Optional[str]
    savings_period: str
    total_recommendations: int
    recommendations_with_savings: int
    recommendations_without_savings: int
    potential_savings: Optional[Decimal]
    savings_by_currency: Dict[str, Decimal]
    by_account: Dict[str, int]
    by_source: Dict[str, int]
    by_resource_type: Dict[str, int]
    by_impact: Dict[str, int]
    by_action: Dict[str, int]
    account_errors: List[AccountError] = field(default_factory=list)

    @property
    def has_mixed_currencies(self) -> bool:
        return self.savings_status == "MIXED_CURRENCY"


def _empty_rollup(
    *, status: str, account_errors: List[AccountError]
) -> AwsRecommendationRollup:
    return AwsRecommendationRollup(
        status=status,
        savings_status="EMPTY",
        currency=None,
        savings_period=AWS_SAVINGS_PERIOD,
        total_recommendations=0,
        recommendations_with_savings=0,
        recommendations_without_savings=0,
        potential_savings=None,
        savings_by_currency={},
        by_account={},
        by_source={},
        by_resource_type={},
        by_impact={},
        by_action={},
        account_errors=list(account_errors),
    )


def _derive_status(report: MultiAccountOptimizationReport) -> str:
    """Map the Phase 7B aggregator status onto the canonical status set.

    * ``ERROR``            — every account failed.
    * ``PARTIAL_SUCCESS``  — at least one account failed.
    * ``NO_DATA``          — clean run, but zero recommendations.
    * ``SUCCESS``          — clean run with recommendations.
    """
    if report.status == "ERROR":
        return "ERROR"
    if report.account_errors:
        return "PARTIAL_SUCCESS"
    if not report.recommendations:
        return "NO_DATA"
    return "SUCCESS"


def build_aws_recommendation_summary(
    report: MultiAccountOptimizationReport,
) -> AwsRecommendationRollup:
    """Fold a Phase 7B report into a canonical rollup.

    The report's recommendations are already deduplicated and
    source-precedence-merged, so we never double count.
    """
    status = _derive_status(report)
    recommendations: Sequence[Recommendation] = report.recommendations
    account_errors = list(report.account_errors)

    if status == "ERROR" and not recommendations:
        return _empty_rollup(status="ERROR", account_errors=account_errors)

    savings_by_currency: Dict[str, Decimal] = {}
    recommendations_with_savings = 0
    recommendations_without_savings = 0
    by_account: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    by_resource_type: Dict[str, int] = {}
    by_impact: Dict[str, int] = {}
    by_action: Dict[str, int] = {}

    for rec in recommendations:
        savings = rec.estimated_monthly_savings
        if savings is None:
            recommendations_without_savings += 1
        else:
            cur = (rec.currency or "USD").upper()
            savings_by_currency[cur] = savings_by_currency.get(
                cur, Decimal("0")
            ) + _decimal(savings)
            recommendations_with_savings += 1

        acct_key = rec.account_id or "Unassigned"
        by_account[acct_key] = by_account.get(acct_key, 0) + 1
        source_key = _enum_value(rec.primary_source)
        by_source[source_key] = by_source.get(source_key, 0) + 1
        rt_key = _enum_value(rec.resource_type)
        by_resource_type[rt_key] = by_resource_type.get(rt_key, 0) + 1
        by_impact[_enum_value(rec.confidence)] = (
            by_impact.get(_enum_value(rec.confidence), 0) + 1
        )
        by_action[_enum_value(rec.action)] = (
            by_action.get(_enum_value(rec.action), 0) + 1
        )

    distinct_currencies = list(savings_by_currency.keys())
    if len(distinct_currencies) == 0:
        savings_status = "EMPTY"
        potential_savings: Optional[Decimal] = None
        currency: Optional[str] = None
    elif len(distinct_currencies) == 1:
        savings_status = "OK"
        potential_savings = savings_by_currency[distinct_currencies[0]]
        currency = distinct_currencies[0]
    else:
        savings_status = "MIXED_CURRENCY"
        potential_savings = None
        currency = None

    return AwsRecommendationRollup(
        status=status,
        savings_status=savings_status,
        currency=currency,
        savings_period=AWS_SAVINGS_PERIOD,
        total_recommendations=len(recommendations),
        recommendations_with_savings=recommendations_with_savings,
        recommendations_without_savings=recommendations_without_savings,
        potential_savings=potential_savings,
        savings_by_currency=dict(savings_by_currency),
        by_account=by_account,
        by_source=by_source,
        by_resource_type=by_resource_type,
        by_impact=by_impact,
        by_action=by_action,
        account_errors=account_errors,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def aggregate_aws_recommendations_for_accounts(
    *,
    rows: Sequence[CloudAccount],
    region: Optional[str],
    days: int,
) -> Tuple[List[CloudRecommendation], AwsRecommendationRollup, List[AccountError]]:
    """Aggregate AWS recommendations across ``rows``.

    Delegates the bounded-concurrency fan-out, dedup-merge and
    partial-failure isolation to the Phase 7B
    :func:`aggregate_recommendations_for_context`.  Returns
    ``(flat_recommendations, rollup, account_errors)``.
    """
    if not rows:
        return [], _empty_rollup(status="NO_DATA", account_errors=[]), []

    report = await aggregate_recommendations_for_context(
        rows=list(rows), region=region, days=days
    )
    flat = [recommendation_to_cloud(rec) for rec in report.recommendations]
    rollup = build_aws_recommendation_summary(report)
    return flat, rollup, list(report.account_errors)


__all__ = [
    "AWS_SAVINGS_PERIOD",
    "AwsRecommendationRollup",
    "aggregate_aws_recommendations_for_accounts",
    "build_aws_recommendation_summary",
    "recommendation_to_cloud",
]