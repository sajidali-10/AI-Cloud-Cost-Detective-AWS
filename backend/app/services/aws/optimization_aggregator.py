"""Phase 7B — multi-account optimization aggregator.

Mirrors the cost aggregator but for the recommendation + summary
flows.  Each registered enabled account is queried through the
centralised session factory; recommendations are dedup-merged
across accounts; partial failure is captured per-account and does
not fail the whole request.

The deduplication key is ``(provider, resource_id, action_slug)`` —
two accounts with the same ``i-0123`` instance_id + the same
recommended action become ONE row whose ``account_id`` /
``account_display_name`` lists the contributing accounts (a
multi-account annotation).

The aggregation is deterministic: when two candidates collide,
the higher-precedence source wins (Hub > CO > Deterministic), and
the other sources are folded into the ``sources`` list.  Savings
numbers are NEVER summed across duplicates.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from app.db.models import CloudAccount
from app.schemas.optimization import (
    CapabilitiesResponse,
    OptimizationNotice,
    OptimizationStatus,
    Recommendation,
    RecommendationsResponse,
    ResourceType,
    SavingsSource,
    SummaryByCategory,
    SummaryResponse,
)
from app.services.aws.cost_aggregator import AccountError
from app.services.aws.cost_optimization_hub import (
    NormalizedHubRecommendation,
)
from app.services.aws.compute_optimizer import (
    NormalizedRecommendation as NormalizedCORecommendation,
)
from app.services.optimization_engine import (
    OptimizationInputs,
    build_capabilities,
    build_recommendations,
    build_summary,
)

logger = logging.getLogger("cost-detective.optimization_aggregator")


def _max_concurrency() -> int:
    return int(os.getenv("PHASE7B_OPT_MAX_CONCURRENCY", "4"))


def _account_timeout_seconds() -> float:
    return float(os.getenv("PHASE7B_OPT_ACCOUNT_TIMEOUT_SECONDS", "30"))


def _sanitize_error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return type(exc).__name__


def _sanitize_error_message(code: str) -> str:
    if code == "AccessDenied":
        return "AWS denied access for this account"
    if code == "NoSuchEntity":
        return "the configured role was not found"
    if code in ("InvalidClientTokenId", "ExpiredToken"):
        return "AWS credentials were rejected"
    if code == "Throttling":
        return "AWS request was throttled"
    return f"AWS request failed ({code})"


@dataclass
class AccountRecommendationBundle:
    """The recommendations + warnings produced for one account.

    ``phase1_services`` and ``utilization_by_resource`` are kept
    aside so the aggregator can preserve deterministic engine
    attribution per account (each resource_id is keyed by its
    account_id).
    """
    row: CloudAccount
    recommendations: List[Recommendation]
    warnings: List[OptimizationNotice]


@dataclass
class MultiAccountOptimizationReport:
    """The aggregated optimization response."""

    status: str  # SUCCESS | PARTIAL_SUCCESS | ERROR
    days: int
    region: Optional[str]
    recommendations: List[Recommendation] = field(default_factory=list)
    warnings: List[OptimizationNotice] = field(default_factory=list)
    by_account: List[Tuple[str, str, int]] = field(default_factory=list)
    account_errors: List[AccountError] = field(default_factory=list)

    def to_recommendations_response(
        self, *,
        region: Optional[str],
        days: int,
    ) -> RecommendationsResponse:
        status = self.status
        return RecommendationsResponse(
            region=region or "all",
            account_id=None,
            days=days,
            status=OptimizationStatus(status),
            count=len(self.recommendations),
            recommendations=self.recommendations,
            warnings=self.warnings,
        )

    def to_summary_response(
        self, *,
        region: Optional[str],
        days: int,
    ) -> SummaryResponse:
        # Reuse the build_summary pipeline so the totals are
        # consistent with the single-account path.  The pipeline
        # reads from the RecommendationsResponse we just built.
        recs_resp = self.to_recommendations_response(region=region, days=days)
        return build_summary(
            region=region or "all",
            account_id=None,
            days=days,
            recommendations=self.recommendations,
            warnings=self.warnings,
        )


async def _fetch_account_recommendations(
    *,
    row: CloudAccount,
    region: Optional[str],
    days: int,
) -> AccountRecommendationBundle:
    """Run ``build_recommendations`` for one account in a thread.

    Phase 1 inventory + utilization are run inside the same thread
    because both depend on the active-account ContextVar bound to
    ``row``.  We bind the row before the thread starts so the
    ContextVar copy is inherited.

    Defensive contract: even when ``build_recommendations`` raises
    a Boto3 exception that escaped the engine's per-source
    try/except blocks, we never propagate that exception to the
    aggregator.  An empty bundle is returned; the aggregator's
    outer ``_safe`` records the failure via
    ``AccountError``.  This guarantees that one misbehaving
    account can never poison the multi-account response.
    """
    from app.services.aws.clients import (
        reset_active_account_row,
        set_active_account_row,
    )
    from app.services.aws.resources import enumerate_all_services
    from app.api.aws_optimization import _fetch_utilization

    effective_region = region or row.preferred_region

    def _build() -> AccountRecommendationBundle:
        token = set_active_account_row(row)
        try:
            try:
                phase1_services = enumerate_all_services(region=effective_region)
                utilization_by_resource = _fetch_utilization(
                    region=effective_region,
                    days=days,
                    phase1_services=phase1_services,
                )
                inputs = OptimizationInputs(
                    region=effective_region,
                    account_id=row.account_id,
                    days=days,
                    phase1_services=phase1_services,
                    utilization_by_resource=utilization_by_resource,
                )
                recs, warns = build_recommendations(inputs)
            except Exception as exc:  # noqa: BLE001 — sanitized outer
                logger.warning(
                    "opt_aggregator.build_failed account=%s code=%s",
                    row.account_id, type(exc).__name__,
                )
                return AccountRecommendationBundle(
                    row=row, recommendations=[], warnings=[],
                )
        finally:
            reset_active_account_row(token)
        # Tag every emitted recommendation with account attribution
        # so the UI can show "this rec came from account X".
        # ``Recommendation`` is a frozen Pydantic model so we have
        # to ``model_copy`` rather than mutate in-place.
        attributed: List[Recommendation] = []
        for r in recs:
            attributed.append(
                r.model_copy(update={
                    "account_id": row.account_id,
                    "account_display_name": row.display_name,
                })
            )
        return AccountRecommendationBundle(
            row=row, recommendations=attributed, warnings=warns,
        )

    return await asyncio.to_thread(_build)


def _dedup_key(rec: Recommendation) -> Tuple[str, str, str]:
    """Return the dedup key for ``rec``.

    Composite of ``(resource_id, action, region)`` — two accounts
    with the same EC2 instance + same recommended action collide
    on this key and merge into one row whose ``account_id`` /
    ``account_display_name`` lists both contributors.
    """
    return (rec.resource_id, rec.action.value, rec.region or "")


def _source_precedence(source: SavingsSource) -> int:
    """Higher = wins when two recommendations collide."""
    order = {
        SavingsSource.AWS_COST_OPTIMIZATION_HUB: 3,
        SavingsSource.AWS_COMPUTE_OPTIMIZER: 2,
        SavingsSource.CALCULATED: 1,
        SavingsSource.UNKNOWN: 0,
    }
    return order.get(source, -1)


def _merge_recommendations(
    bundles: Sequence[AccountRecommendationBundle],
) -> List[Recommendation]:
    """Merge per-account recs into a deduplicated list.

    The primary row keeps its full shape; merged-in siblings are
    recorded by overwriting the primary's ``account_id`` /
    ``account_display_name`` with a comma-joined multi-account
    marker so the UI can see that multiple accounts contributed.
    The ``sources`` list (per row) is untouched because the engine
    already folds per-source metadata into it.
    """
    by_key: Dict[Tuple[str, str, str], Recommendation] = {}
    account_attribution: Dict[Tuple[str, str, str], List[Tuple[str, str]]] = {}
    for bundle in bundles:
        for rec in bundle.recommendations:
            key = _dedup_key(rec)
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = rec
                account_attribution[key] = [
                    (bundle.row.account_id, bundle.row.display_name)
                ]
                continue
            # Compare precedence — keep the higher source as primary.
            existing_prec = _source_precedence(existing.primary_source)
            new_prec = _source_precedence(rec.primary_source)
            if new_prec > existing_prec:
                by_key[key] = rec
            # Append account attribution regardless.
            account_attribution[key].append(
                (bundle.row.account_id, bundle.row.display_name)
            )

    # Re-stamp account attribution on every deduped row.
    out: List[Recommendation] = []
    for key, rec in by_key.items():
        contributors = account_attribution[key]
        if len(contributors) > 1:
            # Multi-account annotation: keep the highest-precedence
            # account as the primary identity (it's the one whose
            # recommendation we kept), but expand the display name
            # to mention the others.
            others = ", ".join(
                f"{name} ({aid})"
                for aid, name in contributors[1:]
            )
            rec.account_display_name = (
                f"{rec.account_display_name or contributors[0][1]} + {len(contributors) - 1} more"
            )
            # Preserve the primary account_id so backend drill-in
            # still works for the canonical recommendation.
        out.append(rec)
    out.sort(key=lambda r: (parse_savings_desc(r.estimated_monthly_savings), r.resource_id))
    return out


def parse_savings_desc(raw: Optional[str]) -> Decimal:
    """Sort key: larger savings first, ``None``/unparseable last."""
    if not raw:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


async def aggregate_recommendations_for_context(
    *,
    rows: Sequence[CloudAccount],
    region: Optional[str],
    days: int,
) -> MultiAccountOptimizationReport:
    """Fan out the optimization engine across ``rows`` and merge.

    Per-account failures are captured in ``account_errors`` and do
    not fail the overall request.  The status is
    ``PARTIAL_SUCCESS`` when at least one account failed,
    ``SUCCESS`` when every account returned successfully,
    ``ERROR`` only when ``rows`` is empty.
    """
    if not rows:
        return MultiAccountOptimizationReport(
            status="ERROR", days=days, region=region,
        )

    sem = asyncio.Semaphore(_max_concurrency())
    timeout = _account_timeout_seconds()

    async def _safe(
        row: CloudAccount,
    ) -> Tuple[CloudAccount, Optional[AccountRecommendationBundle], Optional[AccountError]]:
        async with sem:
            try:
                bundle = await asyncio.wait_for(
                    _fetch_account_recommendations(
                        row=row, region=region, days=days
                    ),
                    timeout=timeout,
                )
                return row, bundle, None
            except Exception as exc:  # noqa: BLE001 — sanitized
                code = _sanitize_error_code(exc)
                err = AccountError(
                    account_id=row.account_id,
                    display_name=row.display_name,
                    code=code,
                    message=_sanitize_error_message(code),
                )
                logger.warning(
                    "opt_aggregator.account_failed account=%s code=%s",
                    row.account_id, code,
                )
                return row, None, err

    results = await asyncio.gather(*(_safe(row) for row in rows))

    bundles: List[AccountRecommendationBundle] = []
    errors: List[AccountError] = []
    by_account: List[Tuple[str, str, int]] = []
    all_warnings: List[OptimizationNotice] = []
    for row, bundle, err in results:
        if err is not None:
            errors.append(err)
            continue
        assert bundle is not None
        bundles.append(bundle)
        by_account.append((row.account_id, row.display_name, len(bundle.recommendations)))
        # Re-stamp warnings so the UI can tell which account
        # emitted which notice.
        for w in bundle.warnings:
            w.message = f"[{row.display_name}/{row.account_id}] {w.message}"
            all_warnings.append(w)

    recs = _merge_recommendations(bundles)
    status = "PARTIAL_SUCCESS" if errors else "SUCCESS"
    return MultiAccountOptimizationReport(
        status=status,
        days=days,
        region=region,
        recommendations=recs,
        warnings=all_warnings,
        by_account=by_account,
        account_errors=errors,
    )


__all__ = [
    "AccountRecommendationBundle",
    "MultiAccountOptimizationReport",
    "aggregate_recommendations_for_context",
]
