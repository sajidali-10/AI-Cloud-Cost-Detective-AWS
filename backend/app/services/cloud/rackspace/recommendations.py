"""Rackspace canonical recommendation aggregator — Phase 9C.2.

Wires the Phase 9C.1 deterministic optimization engine into the
provider-neutral ``/api/cloud/recommendations`` contract so
``GET /api/cloud/recommendations?provider=RACKSPACE`` serves the
SAME canonical envelope AWS and Azure already use.

Layering (no rule is re-implemented here):

.. code-block:: text

    /api/cloud/recommendations?provider=RACKSPACE
        |
    this module (translation only)
        |
    Phase 9B.2 canonical resources   (aggregate_rackspace_resources)
    Phase 9B.1 canonical cost        (fetch_cost_snapshot — context only)
        |
    Phase 9C.1 deterministic engine  (evaluate_rackspace_optimization)
        |
    CloudRecommendation + rollup

Design rules (inherited from the AWS aggregator in
:mod:`app.services.cloud.aws.recommendation_aggregator`):

* **No duplicate rules.**  Every finding comes from the Phase 9C.1
  engine.  This module only maps a :class:`RackspaceFinding` onto
  the provider-neutral :class:`CloudRecommendation` shape and folds
  the engine result into a canonical rollup.
* **No fabricated savings.**  ``estimated_monthly_savings`` is
  ``None`` whenever the engine could not prove a figure — unknown
  is NEVER ``$0``.  The rollup therefore reports
  ``savings_status="EMPTY"`` with ``potential_savings=None``.
* **Deterministic.**  The engine's stable ``finding_id`` becomes the
  canonical ``recommendation_id`` verbatim, ordering is inherited
  from the engine's severity/size sort, and the evaluation
  timestamp stays the engine's deterministic sentinel (never
  wall-clock).  Same canonical input -> byte-identical output.
* **Sanitised errors.**  Per-service resource failures surface as
  ``{account_id, display_name, code, message}`` entries — the
  account id is the safe Cloud tenant, the display name names the
  Rackspace service that failed.  No credentials, X-Auth-Token
  values, the RAN, or raw upstream payloads ever appear.

Partial-data semantics
----------------------

When the Phase 9B.2 resource aggregation is ``PARTIAL_SUCCESS``
(e.g. servers inventoried, volumes unavailable) this module:

* evaluates ONLY the valid available resources through the engine;
* emits only evidence-supported findings (a missing volume service
  yields zero volume findings — nothing is fabricated);
* preserves the per-service failures in the rollup so the canonical
  envelope can answer ``PARTIAL_SUCCESS`` with ``account_errors``.

A TOTAL resource failure raises :class:`RackspaceResourceError`
(from the 9B.2 layer) so the canonical route maps it onto a precise
HTTP error envelope — an upstream resource error is never converted
into a misleading zero-recommendation success.

Zero-write guarantee
--------------------

The engine is deterministic and network-free; the canonical inputs
are read-only ``GET`` aggregations (plus the single Identity
authentication ``POST`` owned by the Phase 9A infrastructure).  No
Rackspace product write is ever issued from this path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.db.models import CloudAccount as _CloudAccountRow
from app.services.cloud.models import (
    CloudCostSummary,
    CloudProviderKind,
    CloudRecommendation,
    CloudServiceCost,
)
from app.services.cloud.rackspace.costs import (
    RACKSPACE_COST_SOURCE,
    RackspaceCostSnapshot,
    fetch_cost_snapshot,
)
from app.services.cloud.rackspace.metrics_capability import (
    RightsizingEvidenceStatus,
    probe_metrics_capability,
)
from app.services.cloud.rackspace.optimization import (
    RackspaceFinding,
    RackspaceOptimizationResult,
    RackspaceOptimizationStatus,
    evaluate_rackspace_optimization,
)
from app.services.cloud.rackspace.resources import (
    SERVICE_CLOUD_BLOCK_STORAGE,
    SERVICE_CLOUD_LOAD_BALANCERS,
    SERVICE_CLOUD_SERVERS,
    RACKSPACE_LOAD_BALANCER_TYPE,
    RACKSPACE_SERVER_TYPE,
    RACKSPACE_VOLUME_TYPE,
    RackspaceResourceError,
    RackspaceResourceErrorCategory,
    RackspaceServiceFailure,
    aggregate_rackspace_resources,
)
from app.services.rackspace import get_billing_client

logger = logging.getLogger("cost-detective.cloud.rackspace.recommendations")


#: Canonical action for every Phase 9C.1 finding.  All 9C.1 rules are
#: REVIEW candidates — the engine deliberately never recommends
#: deletion / stop / resize.  The long-form ``recommended_action``
#: text rides in ``extensions``.
RACKSPACE_RECOMMENDATION_ACTION = "REVIEW"

#: The savings period a Rackspace savings figure WOULD be denominated
#: in (mirrors the AWS monthly convention).  With every 9C.1/9C.2
#: finding carrying ``estimated_monthly_savings=None`` the rollup
#: reports ``savings_status="EMPTY"`` — the period is stated for
#: schema parity, never as a savings claim.
RACKSPACE_SAVINGS_PERIOD = "MONTHLY"

#: Summary-level source identifier for the canonical recommendation
#: surface (mirrors ``AWS_OPTIMIZATION`` / ``AZURE_ADVISOR``).  The
#: per-item ``source`` stays the finding's evidence source
#: (``RACKSPACE_CLOUD_API``).
RACKSPACE_RECOMMENDATION_SOURCE = "RACKSPACE_OPTIMIZATION"

#: resource_type -> service display name (same labels the Phase 9B.2
#: canonical resource inventory uses, so the two surfaces agree).
_SERVICE_BY_RESOURCE_TYPE: Dict[str, str] = {
    RACKSPACE_SERVER_TYPE: SERVICE_CLOUD_SERVERS,
    RACKSPACE_VOLUME_TYPE: SERVICE_CLOUD_BLOCK_STORAGE,
    RACKSPACE_LOAD_BALANCER_TYPE: SERVICE_CLOUD_LOAD_BALANCERS,
}


# ---------------------------------------------------------------------------
# Finding -> CloudRecommendation translation
# ---------------------------------------------------------------------------


def finding_to_cloud_recommendation(
    finding: RackspaceFinding,
    *,
    account_display_name: Optional[str] = None,
    savings_currency: Optional[str] = None,
) -> CloudRecommendation:
    """Translate a Phase 9C.1 :class:`RackspaceFinding` into the
    provider-neutral :class:`CloudRecommendation` shape.

    Field mapping (canonical schema reused — no global field added):

    ========================  =====================================
    canonical field           source
    ========================  =====================================
    ``recommendation_id``    ``finding_id`` (stable, verbatim)
    ``resource_id`` / ``resource_type`` / ``region`` / ``account_id``
                             preserved verbatim
    ``service``              service label for the resource type
    ``action``               ``"REVIEW"`` (9C.1 review-only rules)
    ``title``                finding title
    ``category``             finding category (STORAGE_REVIEW, ...)
    ``impact``               finding severity (LOW / MEDIUM / HIGH)
    ``description``          finding summary (long-form explanation)
    ``estimated_savings``    ``estimated_monthly_savings`` (``None``
                             when unknown — never ``$0``)
    ``currency``             ``""`` when savings are unknown (no
                             currency claim), else the account
                             billing currency
    ``source``               finding source (``RACKSPACE_CLOUD_API``)
    ``extensions``           rule_id, resource_name, severity,
                             confidence, recommended_action,
                             evidence, savings_basis, limitations,
                             savings_period, account_display_name
    ========================  =====================================

    ``severity`` AND ``confidence`` are both preserved — ``impact``
    carries the severity (the provider-native priority bucket, the
    same field AWS/Azure promote) while the evidence-driven
    ``confidence`` rides in ``extensions`` alongside the other
    9C.1 semantics (recommended action text, evidence, savings
    basis, limitations) that the shared schema has no column for.
    """
    savings = finding.estimated_monthly_savings
    return CloudRecommendation(
        provider=CloudProviderKind.RACKSPACE,
        account_id=finding.account_id,
        region=finding.region,
        recommendation_id=finding.finding_id,
        resource_id=finding.resource_id,
        resource_type=finding.resource_type,
        service=_SERVICE_BY_RESOURCE_TYPE.get(
            finding.resource_type, finding.resource_type
        ),
        action=RACKSPACE_RECOMMENDATION_ACTION,
        title=finding.title,
        category=finding.category,
        impact=finding.severity.value,
        description=finding.summary,
        estimated_savings=savings,
        # "" mirrors the Phase 9B.1 CloudCostTrend convention for "no
        # monetary figure is emitted" — the envelope boundary renders
        # it as ``None`` so the wire never claims a currency for a
        # savings figure that does not exist.
        currency=(savings_currency or "USD") if savings is not None else "",
        source=finding.source,
        extensions={
            "rule_id": finding.rule_id,
            "resource_name": finding.resource_name,
            "severity": finding.severity.value,
            "confidence": finding.confidence.value,
            "recommended_action": finding.recommended_action,
            "evidence": [dict(item) for item in finding.evidence],
            "savings_basis": finding.savings_basis,
            "limitations": list(finding.limitations),
            "savings_period": RACKSPACE_SAVINGS_PERIOD,
            "account_display_name": account_display_name,
        },
    )


# ---------------------------------------------------------------------------
# Account errors (sanitised)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RackspaceRecommendationAccountError:
    """A sanitised per-account failure surfaced in the rollup.

    ``account_id`` carries the safe Cloud tenant id; ``display_name``
    names the Rackspace service that failed (the provider-neutral
    analog of the AWS per-account error slot).
    """

    account_id: str
    display_name: str
    code: str
    message: str


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RackspaceRecommendationRollup:
    """Aggregated Rackspace recommendation rollup for the route envelope.

    Provider-neutral in shape (mirrors
    :class:`~app.services.cloud.aws.recommendation_aggregator.AwsRecommendationRollup`)
    but populated from the Phase 9C.1 engine result.
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
    account_errors: List[RackspaceRecommendationAccountError] = field(
        default_factory=list
    )


def _derive_status(
    engine_status: RackspaceOptimizationStatus,
    findings_total: int,
    *,
    resource_failures: Sequence[RackspaceServiceFailure],
) -> str:
    """Map the engine result onto the canonical status vocabulary.

    * ``PARTIAL_SUCCESS`` — at least one resource service failed; the
      valid services were still evaluated.
    * ``NO_DATA``         — clean run over valid resources with zero
      findings (the existing canonical zero-recommendation status —
      a *successful* empty result, never an error), including a
      valid empty inventory (engine ``EMPTY``).
    * ``SUCCESS``         — clean run with findings.
    """
    if resource_failures:
        return "PARTIAL_SUCCESS"
    if findings_total == 0:
        return "NO_DATA"
    return "SUCCESS"


def build_rackspace_recommendation_summary(
    result: RackspaceOptimizationResult,
    *,
    rows: Sequence[_CloudAccountRow],
    resource_failures: Sequence[RackspaceServiceFailure],
    savings_currency: Optional[str] = None,
) -> RackspaceRecommendationRollup:
    """Fold the engine result into a canonical rollup.

    Every count derives from the engine's findings — nothing is
    fabricated.  Savings are only totalled when findings actually
    carry a figure AND a currency is known; otherwise
    ``savings_status="EMPTY"`` with ``potential_savings=None``
    (unknown savings are never reported as ``$0``).
    """
    findings = list(result.findings)
    status = _derive_status(
        result.status, result.findings_total, resource_failures=resource_failures,
    )

    recommendations_with_savings = result.estimated_savings_known_count
    recommendations_without_savings = result.estimated_savings_unknown_count

    savings_by_currency: Dict[str, Decimal] = {}
    if savings_currency and recommendations_with_savings > 0:
        # Deterministic single-currency totalisation: the Rackspace
        # billing account reports one currency.  Findings without a
        # figure are never counted, and no FX rate is ever invented.
        total = Decimal("0")
        for finding in findings:
            if finding.estimated_monthly_savings is not None:
                total += finding.estimated_monthly_savings
        savings_by_currency[savings_currency] = total

    if not savings_by_currency:
        savings_status = "EMPTY"
        potential_savings: Optional[Decimal] = None
        currency: Optional[str] = None
    else:
        savings_status = "OK"
        currency = next(iter(savings_by_currency))
        potential_savings = savings_by_currency[currency]

    by_account: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    by_resource_type: Dict[str, int] = {}
    by_impact: Dict[str, int] = {}
    by_action: Dict[str, int] = {}
    for finding in findings:
        account_key = finding.account_id or "Unassigned"
        by_account[account_key] = by_account.get(account_key, 0) + 1
        by_source[finding.source] = by_source.get(finding.source, 0) + 1
        rt_key = finding.resource_type or "Unassigned"
        by_resource_type[rt_key] = by_resource_type.get(rt_key, 0) + 1
        by_impact[finding.severity.value] = (
            by_impact.get(finding.severity.value, 0) + 1
        )
        # Every 9C.1 finding maps to the single canonical REVIEW
        # action — counted from the mapping constant so the rollup
        # can never drift from finding_to_cloud_recommendation.
        by_action[RACKSPACE_RECOMMENDATION_ACTION] = (
            by_action.get(RACKSPACE_RECOMMENDATION_ACTION, 0) + 1
        )

    primary = rows[0] if rows else None
    account_errors = [
        RackspaceRecommendationAccountError(
            account_id=(primary.account_id if primary is not None else ""),
            display_name=failure.service,
            code=failure.category.value,
            message=failure.message,
        )
        for failure in resource_failures
    ]

    return RackspaceRecommendationRollup(
        status=status,
        savings_status=savings_status,
        currency=currency,
        savings_period=RACKSPACE_SAVINGS_PERIOD,
        total_recommendations=result.findings_total,
        recommendations_with_savings=recommendations_with_savings,
        recommendations_without_savings=recommendations_without_savings,
        potential_savings=potential_savings,
        savings_by_currency=dict(savings_by_currency),
        by_account=dict(by_account),
        by_source=dict(by_source),
        by_resource_type=dict(by_resource_type),
        by_impact=dict(by_impact),
        by_action=dict(by_action),
        account_errors=account_errors,
    )


# ---------------------------------------------------------------------------
# Cost context (fail-soft)
# ---------------------------------------------------------------------------


def _cost_context_from_snapshot(
    snapshot: RackspaceCostSnapshot,
    rows: Sequence[_CloudAccountRow],
) -> CloudCostSummary:
    """Build the engine's account-level cost context from a 9B.1 snapshot.

    Context ONLY — the engine never allocates this total across
    resources.  The translation mirrors the adapter's 9B.1 summary
    but stays lean: the engine reads presence + attribution, not
    envelope extras.
    """
    primary = rows[0] if rows else None
    account_id_value: Optional[str] = (
        primary.account_id if primary is not None and len(rows) == 1 else None
    )
    by_service: List[CloudServiceCost] = [
        CloudServiceCost(
            service=item.service,
            amount=item.amount,
            currency=snapshot.currency,
            provider=CloudProviderKind.RACKSPACE,
            account_id=account_id_value,
            region=None,
        )
        for item in snapshot.by_service
    ]
    return CloudCostSummary(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id_value,
        scope=(
            f"acct:{primary.account_id}"
            if primary is not None and len(rows) == 1
            else "all"
        ),
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        currency=snapshot.currency,
        current_total=snapshot.total,
        estimated=snapshot.estimated,
        previous_total=None,
        change_amount=None,
        change_percent=None,
        by_service=by_service,
        by_region=[],
        extensions={
            "status": "SUCCESS",
            "source": RACKSPACE_COST_SOURCE,
            "purpose": "optimization-context",
        },
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def aggregate_rackspace_recommendations_for_account(
    *,
    rows: Sequence[_CloudAccountRow],
    region: Optional[str] = None,
    days: Optional[int] = None,
) -> Tuple[
    List[CloudRecommendation],
    RackspaceRecommendationRollup,
    List[RackspaceServiceFailure],
]:
    """Aggregate canonical Rackspace recommendations for ``rows``.

    Pipeline (read-only, deterministic):

    1. Phase 9B.2 canonical resources (``aggregate_rackspace_resources``)
       — a TOTAL failure raises :class:`RackspaceResourceError` so the
       route can answer with a precise error envelope instead of a
       misleading zero-recommendation success.
    2. Phase 9B.1 canonical cost context (``fetch_cost_snapshot``) —
       best-effort account-level context; a billing failure degrades
       to ``cost_context=None`` and NEVER fails the recommendations.
    3. Phase 9C.1 deterministic engine (``evaluate_rackspace_optimization``).

    ``region`` and ``days`` are accepted for canonical route parity
    and deliberately ignored: Rackspace discovery enumerates every
    authorized datacenter from the Identity catalog (no regional
    filter), and the engine has no historical lookback — the same
    way the Azure path ignores ``days`` (Advisor is
    subscription-scoped).

    Returns ``(flat_recommendations, rollup, resource_failures)``.
    """
    rows = [row for row in (rows or []) if row is not None]
    if not rows:
        raise RackspaceResourceError(
            RackspaceResourceErrorCategory.NOT_CONFIGURED,
            "no Rackspace accounts are registered",
        )

    _summary, resources, resource_failures = await aggregate_rackspace_resources(
        rows=rows
    )

    # Cost context — best-effort.  The billing snapshot is account-level
    # CONTEXT for the engine's limitations; it is never allocated
    # across resources and its absence never fails recommendations.
    cost_context: Optional[CloudCostSummary] = None
    savings_currency: Optional[str] = None
    try:
        snapshot = await fetch_cost_snapshot(get_billing_client())
        cost_context = _cost_context_from_snapshot(snapshot, rows)
        savings_currency = (snapshot.currency or "").strip() or None
    except Exception as exc:  # noqa: BLE001 — context is fail-soft
        logger.warning(
            "rackspace.recommendations.cost_context_unavailable code=%s",
            type(exc).__name__,
        )

    capability = await probe_metrics_capability()
    if capability.status != RightsizingEvidenceStatus.DEFERRED:
        # Defensive: the probe is 9C.1-deferred by default.  If a
        # future subphase flips it on, log it — the engine still owns
        # the evidence gate.
        logger.info(
            "rackspace.recommendations.rightsizing_evidence status=%s",
            capability.status.value,
        )

    result = evaluate_rackspace_optimization(
        resources,
        cost_context=cost_context,
        rightsizing_evidence_status=capability.status.value,
    )

    display_name = rows[0].display_name if rows else None
    flat = [
        finding_to_cloud_recommendation(
            finding,
            account_display_name=display_name,
            savings_currency=savings_currency,
        )
        for finding in result.findings
    ]
    rollup = build_rackspace_recommendation_summary(
        result,
        rows=rows,
        resource_failures=resource_failures,
        savings_currency=savings_currency,
    )
    return flat, rollup, list(resource_failures)


__all__ = [
    "RACKSPACE_RECOMMENDATION_ACTION",
    "RACKSPACE_RECOMMENDATION_SOURCE",
    "RACKSPACE_SAVINGS_PERIOD",
    "RackspaceRecommendationAccountError",
    "RackspaceRecommendationRollup",
    "aggregate_rackspace_recommendations_for_account",
    "build_rackspace_recommendation_summary",
    "finding_to_cloud_recommendation",
]
