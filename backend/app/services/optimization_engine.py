"""Optimization engine orchestrator — Phase 3.

Joins three recommendation sources into a single deduplicated
``List[Recommendation]``:

1. AWS Cost Optimization Hub (preferred authoritative source for
   standardized savings).
2. AWS Compute Optimizer (secondary AWS-native source).
3. Deterministic rule engine (Phase 1 inventory + Phase 2 evidence).

The orchestrator is the only place where deduplication happens.  The
algorithm:

* Each source emits a ``RecommendationCandidate`` row.
* Candidates are keyed by ``(resource_id, action_slug)`` after a
  deterministic action-translation step (Compute Optimizer's
  ``RIGHTSIZING`` maps to ``RIGHTSIZE``; Cost Optimization Hub's
  ``Rightsize`` maps to ``RIGHTSIZE``; etc.).
* When two candidates share a key, the one with the highest
  ``SavingsSource`` precedence (``AWS_COST_OPTIMIZATION_HUB`` >
  ``AWS_COMPUTE_OPTIMIZER`` > ``CALCULATED`` > ``UNKNOWN``) is kept
  as the primary.  The other source's metadata is folded into the
  ``sources`` list so the UI can still attribute the overlap.
* Savings numbers are NEVER summed across duplicates — only the
  primary candidate's ``estimated_monthly_savings`` flows into the
  aggregate.

The orchestrator also computes the summary breakdown (by resource
type / action / source / confidence) using the same deduplicated
list, so the savings total in the summary exactly matches the
recommendations endpoint.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.schemas.optimization import (
    CapabilitiesResponse,
    Confidence,
    OptimizationStatus,
    Recommendation,
    RecommendationAction,
    RecommendationEvidence,
    RecommendationsResponse,
    ResourceType,
    SavingsSource,
    ServiceCapability,
    SummaryByCategory,
    SummaryResponse,
    OptimizationNotice,
    action_slug,
)
from app.services.aws.compute_optimizer import (
    ComputeOptimizerError,
    NormalizedRecommendation as CONormalized,
    get_compute_optimizer_client,
    get_ebs_volume_recommendations,
    get_ec2_instance_recommendations,
    get_enrollment_status as get_co_enrollment_status,
    get_lambda_function_recommendations,
    get_rds_database_recommendations,
    get_recommendation_summaries,
)
from app.services.aws.cost_optimization_hub import (
    CostOptimizationHubError,
    NormalizedHubRecommendation,
    get_cost_optimization_hub_client,
    get_recommendation,
    list_enrollment_statuses,
    list_recommendation_summaries,
    list_recommendations,
)
from app.services.optimization_rules import (
    DeterministicCandidate,
    deterministic_recommendation_id,
    rule_idle_load_balancer,
    rule_idle_nat_gateway,
    rule_low_utilization_ec2,
    rule_rds_underutilization,
    rule_unattached_ebs,
    rule_unused_eip,
)

logger = logging.getLogger("cost-detective-backend.optimization_engine")


# ---------------------------------------------------------------------------
# Precedence for primary source selection.
# ---------------------------------------------------------------------------

SOURCE_PRECEDENCE: Dict[SavingsSource, int] = {
    SavingsSource.AWS_COST_OPTIMIZATION_HUB: 0,
    SavingsSource.AWS_COMPUTE_OPTIMIZER: 1,
    SavingsSource.CALCULATED: 2,
    SavingsSource.UNKNOWN: 3,
}


# ---------------------------------------------------------------------------
# Mapping helpers — translate raw AWS labels into our enum values.
# ---------------------------------------------------------------------------

_CO_FINDING_TO_ACTION: Dict[str, RecommendationAction] = {
    "OVER_PROVISIONED": RecommendationAction.RIGHTSIZE,
    "OVERPROVISIONED": RecommendationAction.RIGHTSIZE,
    "UNDER_PROVISIONED": RecommendationAction.RIGHTSIZE,
    "UNDERPROVISIONED": RecommendationAction.RIGHTSIZE,
    "NOT_OPTIMIZED": RecommendationAction.RIGHTSIZE,
    "NOTOPTIMIZED": RecommendationAction.RIGHTSIZE,
    "OPTIMIZED": RecommendationAction.RIGHTSIZE,  # surfaced for completeness
}


_COH_ACTION_TO_ACTION: Dict[str, RecommendationAction] = {
    "RIGHTSIZE": RecommendationAction.RIGHTSIZE,
    "DELETE_UNUSED": RecommendationAction.DELETE_UNUSED,
    "STOP_IDLE": RecommendationAction.STOP_IDLE,
    "RELEASE_EIP": RecommendationAction.RELEASE_UNUSED,
    "PURCHASE_SAVINGS_PLAN": RecommendationAction.RIGHTSIZE,
    "PURCHASE_RESERVED_INSTANCE": RecommendationAction.RIGHTSIZE,
    "MIGRATE_TO_GRAVITON": RecommendationAction.RIGHTSIZE,
}


_COH_RESOURCE_TYPE_TO_ENUM: Dict[str, ResourceType] = {
    "EC2": ResourceType.EC2,
    "INSTANCE": ResourceType.EC2,
    "EBS_VOLUME": ResourceType.EBS_VOLUME,
    "VOLUME": ResourceType.EBS_VOLUME,
    "LAMBDA_FUNCTION": ResourceType.LAMBDA_FUNCTION,
    "FUNCTION": ResourceType.LAMBDA_FUNCTION,
    "RDS_DB_INSTANCE": ResourceType.RDS_DB_INSTANCE,
    "DB_INSTANCE": ResourceType.RDS_DB_INSTANCE,
    "ELASTIC_IP": ResourceType.ELASTIC_IP,
    "EIP": ResourceType.ELASTIC_IP,
    "NAT_GATEWAY": ResourceType.NAT_GATEWAY,
    "NAT": ResourceType.NAT_GATEWAY,
    "ELASTIC_LOAD_BALANCER": ResourceType.LOAD_BALANCER,
    "LOAD_BALANCER": ResourceType.LOAD_BALANCER,
    "ALB": ResourceType.LOAD_BALANCER,
    "NLB": ResourceType.LOAD_BALANCER,
}


_CO_RESOURCE_TYPE_TO_ENUM: Dict[str, ResourceType] = {
    "EC2": ResourceType.EC2,
    "EC2INSTANCE": ResourceType.EC2,
    "INSTANCE": ResourceType.EC2,
    "EBS": ResourceType.EBS_VOLUME,
    "EBSVOLUME": ResourceType.EBS_VOLUME,
    "VOLUME": ResourceType.EBS_VOLUME,
    "LAMBDA": ResourceType.LAMBDA_FUNCTION,
    "LAMBDASFUNCTION": ResourceType.LAMBDA_FUNCTION,
    "RDS": ResourceType.RDS_DB_INSTANCE,
    "RDS_DB_INSTANCE": ResourceType.RDS_DB_INSTANCE,
    "RDSDBINSTANCE": ResourceType.RDS_DB_INSTANCE,
}


def _co_resource_type(raw: str) -> ResourceType:
    if not raw:
        return ResourceType.EC2
    return _CO_RESOURCE_TYPE_TO_ENUM.get(raw.upper().replace(" ", ""), ResourceType.EC2)


def _coh_resource_type(raw: str) -> ResourceType:
    if not raw:
        return ResourceType.EC2
    return _COH_RESOURCE_TYPE_TO_ENUM.get(raw.upper(), ResourceType.EC2)


def _co_action(finding: str, recommended_options: List[Dict[str, Any]]) -> RecommendationAction:
    """Map a Compute Optimizer finding to a ``RecommendationAction``.

    The hub's action model is richer than Compute Optimizer's.  When
    Compute Optimizer reports ``OPTIMIZED`` we still emit a
    ``RIGHTSIZE`` row with savings=0 so the UI can show "already
    optimized" and a Phase 4 AI layer can explain why nothing should
    change.
    """
    if not finding:
        return RecommendationAction.RIGHTSIZE
    action = _CO_FINDING_TO_ACTION.get(finding.upper())
    if action is not None:
        return action
    return RecommendationAction.RIGHTSIZE


def _coh_action(raw: str) -> RecommendationAction:
    if not raw:
        return RecommendationAction.RIGHTSIZE
    # The hub uses both CamelCase (``Rightsize``, ``StopIdle``) and
    # UPPER_SNAKE (``RIGHTSIZE`` / ``STOP_IDLE``) for the same actions.
    # Normalize both forms to a canonical UPPER_SNAKE before lookup.
    upper = raw.upper()
    snake = re.sub(r"(?<!^)(?=[A-Z][a-z])", "_", raw).upper()
    for key in (upper, snake):
        action = _COH_ACTION_TO_ACTION.get(key)
        if action is not None:
            return action
    return RecommendationAction.RIGHTSIZE


def _confidence_from_co_finding(finding: str, has_savings: bool) -> Confidence:
    """Map a Compute Optimizer finding + savings availability to confidence.

    AWS supplies savings only when the recommendation is actionable;
    ``OPTIMIZED`` rows return no number.  Confidence is HIGH whenever
    AWS emitted a real recommendation row (i.e. the data is AWS's).
    """
    if finding.upper() == "OPTIMIZED" and not has_savings:
        return Confidence.LOW
    if has_savings:
        return Confidence.HIGH
    return Confidence.MEDIUM


def _confidence_from_hub(has_savings: bool) -> Confidence:
    if has_savings:
        return Confidence.HIGH
    return Confidence.MEDIUM


def _confidence_from_deterministic(quality: str) -> Confidence:
    mapping = {"high": Confidence.HIGH, "medium": Confidence.MEDIUM, "low": Confidence.LOW}
    return mapping.get((quality or "").lower(), Confidence.LOW)


# ---------------------------------------------------------------------------
# Candidate dataclass — the orchestrator's internal representation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationCandidate:
    """Pre-dedup candidate produced by one source.

    ``key`` is ``(resource_id, action_slug)`` and is the dedup key.
    The orchestrator keeps the candidate with the highest precedence
    ``source`` and folds the other(s) into ``alternate_sources``.
    """

    key: tuple[str, str]
    source: SavingsSource
    resource_id: str
    resource_arn: Optional[str]
    resource_type: ResourceType
    region: str
    account_id: Optional[str]
    action: RecommendationAction
    title: str
    finding: str
    current_configuration: Dict[str, Any]
    recommended_configuration: Dict[str, Any]
    estimated_monthly_savings: Optional[Decimal]
    currency: str
    savings_percentage: Optional[Decimal]
    confidence: Confidence
    data_quality: str
    reason_codes: List[str]
    restart_needed: Optional[bool]
    rollback_possible: Optional[bool]
    evidence: List[RecommendationEvidence]
    aws_recommendation_ids: List[str]


# ---------------------------------------------------------------------------
# Translators — AWS rows / rule candidates -> RecommendationCandidate.
# ---------------------------------------------------------------------------


def _co_to_candidate(n: CONormalized) -> RecommendationCandidate:
    """Translate a Compute Optimizer normalized row to a candidate."""
    action = _co_action(n.finding, [n.recommended_configuration])
    has_savings = n.estimated_monthly_savings is not None and n.estimated_monthly_savings > 0
    confidence = _confidence_from_co_finding(n.finding, bool(has_savings))
    evidence = [
        RecommendationEvidence(
            source=SavingsSource.AWS_COMPUTE_OPTIMIZER,
            confidence=confidence,
            data={
                "finding": n.finding,
                "lookback_period_days": n.lookback_period_days,
                "performance_risk": str(n.performance_risk) if n.performance_risk is not None else None,
                "current_instance_type": n.current_configuration.get("instance_type"),
                "recommended_options": n.recommended_configuration,
            },
            reason_codes=list(n.reason_codes),
        )
    ]
    title = (
        f"Rightsize {n.resource_type} {n.resource_id}"
        if action == RecommendationAction.RIGHTSIZE
        else f"{action.value} {n.resource_type} {n.resource_id}"
    )
    finding = n.finding or "Compute Optimizer recommendation."
    return RecommendationCandidate(
        key=(n.resource_id, action.value),
        source=SavingsSource.AWS_COMPUTE_OPTIMIZER,
        resource_id=n.resource_id,
        resource_arn=n.resource_arn,
        resource_type=_co_resource_type(n.resource_type),
        region=n.region or "unknown",
        account_id=n.account_id,
        action=action,
        title=title,
        finding=finding,
        current_configuration=n.current_configuration,
        recommended_configuration=n.recommended_configuration,
        estimated_monthly_savings=n.estimated_monthly_savings,
        currency=n.currency or "USD",
        savings_percentage=n.savings_percentage,
        confidence=confidence,
        data_quality="high",
        reason_codes=list(n.reason_codes),
        restart_needed=None,
        rollback_possible=True,
        evidence=evidence,
        aws_recommendation_ids=[n.aws_recommendation_id] if n.aws_recommendation_id else [],
    )


def _coh_to_candidate(n: NormalizedHubRecommendation) -> RecommendationCandidate:
    """Translate a Cost Optimization Hub row to a candidate."""
    action = _coh_action(n.action_type)
    has_savings = n.estimated_monthly_savings is not None and n.estimated_monthly_savings > 0
    confidence = _confidence_from_hub(bool(has_savings))
    evidence = [
        RecommendationEvidence(
            source=SavingsSource.AWS_COST_OPTIMIZATION_HUB,
            confidence=confidence,
            data={
                "current_resource_summary": n.current_resource_summary,
                "recommended_resource_summary": n.recommended_resource_summary,
                "implementation_effort": n.implementation_effort,
                "restart_needed": n.restart_needed,
                "rollback_possible": n.rollback_possible,
            },
            reason_codes=[],
        )
    ]
    title = (
        f"Rightsize {n.resource_type} {n.resource_id}"
        if action == RecommendationAction.RIGHTSIZE
        else f"{action.value} {n.resource_type} {n.resource_id}"
    )
    finding = (
        f"AWS Cost Optimization Hub action '{n.action_type or 'Rightsize'}'."
    )
    return RecommendationCandidate(
        key=(n.resource_id, action.value),
        source=SavingsSource.AWS_COST_OPTIMIZATION_HUB,
        resource_id=n.resource_id,
        resource_arn=n.resource_arn,
        resource_type=_coh_resource_type(n.resource_type),
        region=n.region or "unknown",
        account_id=n.account_id,
        action=action,
        title=title,
        finding=finding,
        current_configuration=n.current_resource_summary,
        recommended_configuration=n.recommended_resource_summary,
        estimated_monthly_savings=n.estimated_monthly_savings,
        currency=n.currency or "USD",
        savings_percentage=n.savings_percentage,
        confidence=confidence,
        data_quality="high",
        reason_codes=[],
        restart_needed=n.restart_needed,
        rollback_possible=n.rollback_possible,
        evidence=evidence,
        aws_recommendation_ids=[n.recommendation_id] if n.recommendation_id else [],
    )


def _deterministic_to_candidate(
    candidate: DeterministicCandidate,
    *,
    account_id: Optional[str],
) -> RecommendationCandidate:
    """Translate a deterministic rule candidate to a candidate."""
    rid = deterministic_recommendation_id(
        account_id=account_id,
        region=candidate.region,
        resource_id=candidate.resource_id,
        action=candidate.action,
    )
    evidence = [
        RecommendationEvidence(
            source=SavingsSource.UNKNOWN,
            confidence=candidate.confidence,
            data=candidate.evidence,
            reason_codes=list(candidate.reason_codes),
        )
    ]
    return RecommendationCandidate(
        key=(rid, candidate.action.value),
        source=SavingsSource.UNKNOWN,
        resource_id=candidate.resource_id,
        resource_arn=candidate.resource_arn,
        resource_type=candidate.resource_type,
        region=candidate.region,
        account_id=account_id,
        action=candidate.action,
        title=candidate.title,
        finding=candidate.finding,
        current_configuration=candidate.current_configuration,
        recommended_configuration=candidate.recommended_configuration,
        estimated_monthly_savings=None,
        currency="USD",
        savings_percentage=None,
        confidence=candidate.confidence,
        data_quality=candidate.data_quality,
        reason_codes=list(candidate.reason_codes),
        restart_needed=None,
        rollback_possible=True,
        evidence=evidence,
        aws_recommendation_ids=[],
    )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate(
    candidates: Sequence[RecommendationCandidate],
) -> List[RecommendationCandidate]:
    """Return deduplicated candidates.

    The key is ``(resource_id, action_slug)``.  When two candidates
    share the key, the one with the lowest ``SOURCE_PRECEDENCE``
    wins and the loser's source is folded into ``evidence`` /
    ``aws_recommendation_ids``.  Savings are kept from the primary
    only — never summed.
    """
    by_key: Dict[tuple[str, str], RecommendationCandidate] = {}
    for cand in candidates:
        existing = by_key.get(cand.key)
        if existing is None:
            by_key[cand.key] = cand
            continue
        # Pick the higher-precedence (lower numeric precedence).
        if SOURCE_PRECEDENCE[cand.source] < SOURCE_PRECEDENCE[existing.source]:
            primary, secondary = cand, existing
        else:
            primary, secondary = existing, cand
        # Merge the secondary's source attribution into the primary.
        merged_evidence = list(primary.evidence) + list(secondary.evidence)
        merged_aws_ids = list(primary.aws_recommendation_ids) + list(secondary.aws_recommendation_ids)
        # Update reason_codes conservatively — keep primary, append
        # any new reasons from the secondary.
        merged_reasons = list(primary.reason_codes)
        for r in secondary.reason_codes:
            if r not in merged_reasons:
                merged_reasons.append(r)
        # If the primary is UNKNOWN and the secondary has a savings
        # figure, prefer the secondary's number — keeps deterministic
        # candidates from masking a real AWS estimate.
        primary_savings = primary.estimated_monthly_savings
        primary_currency = primary.currency
        primary_pct = primary.savings_percentage
        primary_confidence = primary.confidence
        if (
            primary.source == SavingsSource.UNKNOWN
            and secondary.estimated_monthly_savings is not None
        ):
            primary_savings = secondary.estimated_monthly_savings
            primary_pct = secondary.savings_percentage or primary_pct
            primary_confidence = max(primary_confidence, secondary.confidence, key=_confidence_rank)
        by_key[cand.key] = RecommendationCandidate(
            key=primary.key,
            source=primary.source,
            resource_id=primary.resource_id,
            resource_arn=primary.resource_arn,
            resource_type=primary.resource_type,
            region=primary.region,
            account_id=primary.account_id,
            action=primary.action,
            title=primary.title,
            finding=primary.finding,
            current_configuration=primary.current_configuration,
            recommended_configuration=primary.recommended_configuration,
            estimated_monthly_savings=primary_savings,
            currency=primary_currency,
            savings_percentage=primary_pct,
            confidence=primary_confidence,
            data_quality=primary.data_quality,
            reason_codes=merged_reasons,
            restart_needed=primary.restart_needed if primary.restart_needed is not None else secondary.restart_needed,
            rollback_possible=(
                primary.rollback_possible
                if primary.rollback_possible is not None
                else secondary.rollback_possible
            ),
            evidence=merged_evidence,
            aws_recommendation_ids=merged_aws_ids,
        )
    return list(by_key.values())


def _confidence_rank(c: Confidence) -> int:
    return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(c.value, 3)


# ---------------------------------------------------------------------------
# Candidate -> Pydantic Recommendation
# ---------------------------------------------------------------------------


def _candidate_to_recommendation(
    cand: RecommendationCandidate,
    *,
    index: int,
) -> Recommendation:
    """Convert an internal candidate to the public Pydantic model.

    The ``recommendation_id`` is a stable, deterministic string.  For
    candidates originating from a deterministic rule, the id embeds
    the resource identity and action.  For AWS-native candidates, the
    public id embeds the deterministic key plus a short random suffix
    so we never expose raw AWS recommendation ids as immutable
    business keys (AWS may rotate them).
    """
    rid = deterministic_recommendation_id(
        account_id=cand.account_id,
        region=cand.region,
        resource_id=cand.resource_id,
        action=cand.action,
        suffix=f"{index:04d}",
    )
    sources = [cand.source]
    # Append the source of every evidence block (which may include
    # both primary and secondary sources after dedup).
    for ev in cand.evidence:
        if ev.source not in sources:
            sources.append(ev.source)
    # Compute a savings_source label:
    #   - If the candidate's primary source is AWS-native and has a
    #     savings figure, use that.
    #   - Otherwise UNKNOWN (deterministic rules).
    if cand.estimated_monthly_savings is not None and cand.source != SavingsSource.UNKNOWN:
        savings_source = cand.source
    elif cand.estimated_monthly_savings is not None:
        savings_source = SavingsSource.CALCULATED
    else:
        savings_source = SavingsSource.UNKNOWN
    return Recommendation(
        recommendation_id=rid,
        resource_id=cand.resource_id,
        resource_arn=cand.resource_arn,
        resource_type=cand.resource_type,
        region=cand.region,
        account_id=cand.account_id,
        action=cand.action,
        title=cand.title,
        finding=cand.finding,
        current_configuration=cand.current_configuration,
        recommended_configuration=cand.recommended_configuration,
        estimated_monthly_savings=cand.estimated_monthly_savings,
        currency=cand.currency,
        savings_percentage=cand.savings_percentage,
        savings_source=savings_source,
        primary_source=cand.source,
        sources=sources,
        confidence=cand.confidence,
        data_quality=cand.data_quality,
        reason_codes=cand.reason_codes,
        restart_needed=cand.restart_needed,
        rollback_possible=cand.rollback_possible,
        evidence=cand.evidence,
        aws_recommendation_ids=cand.aws_recommendation_ids,
    )


# ---------------------------------------------------------------------------
# Capability gating — expected non-active AWS-native source states.
# ---------------------------------------------------------------------------

# These ``CapabilityStatus`` values represent legitimate, expected
# enrollment / availability states for an AWS-native source.  When
# the resolved status falls into this set the engine MUST NOT:
#   * call any of the source's recommendation APIs
#     (``GetEC2InstanceRecommendations``,
#      ``GetEBSVolumeRecommendations``,
#      ``GetLambdaFunctionRecommendations``,
#      ``GetRDSDatabaseRecommendations`` for Compute Optimizer;
#      ``ListRecommendations`` / ``ListRecommendationSummaries`` /
#      ``GetRecommendation`` for Cost Optimization Hub)
#   * emit a spurious ``OptimizationNotice`` (no
#     ``AccessDeniedException`` / ``ComputeOptimizerError`` warnings)
# Only ``ACTIVE`` triggers a real fetcher call.  ``ACCESS_DENIED``
# and ``UNAVAILABLE`` are NOT in this set — they represent actual
# failures and the existing fetcher-error path still emits a
# warning for them, but only when the engine has no prior knowledge
# of the enrollment state (defense-in-depth).
_EXPECTED_NON_ACTIVE_STATES: frozenset[str] = frozenset(
    {"INACTIVE", "NOT_ENROLLED", "PENDING", "FAILED"}
)


def _should_fetch_aws_source(status: Optional[str]) -> bool:
    """Return ``True`` only when an AWS-native source is fully ACTIVE.

    The orchestrator uses this gate before calling any of the
    per-source recommendation APIs so an expected ``INACTIVE`` /
    ``NOT_ENROLLED`` enrollment state never produces a real AWS call
    (and therefore never a spurious ``AccessDeniedException``
    warning).
    """
    if status is None:
        return False
    return str(status).strip().upper() == "ACTIVE"


# ---------------------------------------------------------------------------
# Source fetchers (one per AWS service).  Each returns a list of
# ``RecommendationCandidate`` or raises its own sanitized error.
# ---------------------------------------------------------------------------


def _fetch_compute_optimizer_candidates(
    *,
    region: str,
    account_id: Optional[str],
) -> List[RecommendationCandidate]:
    client = get_compute_optimizer_client(region=region)
    out: List[RecommendationCandidate] = []
    for n in get_ec2_instance_recommendations(client, region=region):
        out.append(_co_to_candidate(n))
    for n in get_ebs_volume_recommendations(client, region=region):
        out.append(_co_to_candidate(n))
    for n in get_lambda_function_recommendations(client, region=region):
        out.append(_co_to_candidate(n))
    for n in get_rds_database_recommendations(client, region=region):
        out.append(_co_to_candidate(n))
    return out


def _fetch_cost_optimization_hub_candidates(
    *,
    region: str,
    account_id: Optional[str],
) -> List[NormalizedHubRecommendation]:
    client = get_cost_optimization_hub_client(region=region)
    rows = list_recommendations(client, region=region)
    return rows


def _fetch_deterministic_candidates(
    *,
    account_id: Optional[str],
    region: str,
    phase1_services: Dict[str, Any],
    utilization_by_resource: Dict[str, List[MetricSeries]],
    lookback_days: int,
) -> List[DeterministicCandidate]:
    """Run every deterministic rule and return the candidates."""
    ec2_items = (phase1_services.get("ec2").items if phase1_services.get("ec2") else []) or []
    ebs_items = (phase1_services.get("ebs").items if phase1_services.get("ebs") else []) or []
    eip_items = (phase1_services.get("eip").items if phase1_services.get("eip") else []) or []
    nat_items = (phase1_services.get("nat").items if phase1_services.get("nat") else []) or []
    elb_items = (phase1_services.get("elbv2").items if phase1_services.get("elbv2") else []) or []
    rds_items = (phase1_services.get("rds").items if phase1_services.get("rds") else []) or []
    out: List[DeterministicCandidate] = []
    out.extend(
        rule_unattached_ebs(account_id=account_id, region=region, ebs_volumes=ebs_items)
    )
    out.extend(
        rule_unused_eip(account_id=account_id, region=region, elastic_ips=eip_items)
    )
    out.extend(
        rule_low_utilization_ec2(
            account_id=account_id,
            region=region,
            ec2_instances=ec2_items,
            utilization_by_resource=utilization_by_resource,
            lookback_days=lookback_days,
        )
    )
    out.extend(
        rule_idle_nat_gateway(
            account_id=account_id,
            region=region,
            nat_gateways=nat_items,
            utilization_by_resource=utilization_by_resource,
        )
    )
    out.extend(
        rule_idle_load_balancer(
            account_id=account_id,
            region=region,
            load_balancers=elb_items,
            utilization_by_resource=utilization_by_resource,
        )
    )
    out.extend(
        rule_rds_underutilization(
            account_id=account_id,
            region=region,
            rds_instances=rds_items,
            utilization_by_resource=utilization_by_resource,
            lookback_days=lookback_days,
        )
    )
    return out


# ---------------------------------------------------------------------------
# Capabilities endpoint
# ---------------------------------------------------------------------------


def _co_capability(region: str, account_id: Optional[str]) -> tuple[ServiceCapability, Optional[str]]:
    """Return ``(ServiceCapability, sanitized_error_code_or_None)``.

    The error code is exposed separately so the orchestrator can
    emit a structured ``Warning`` without leaking the original Boto3
    message.
    """
    try:
        client = get_compute_optimizer_client(region=region)
        status = get_co_enrollment_status(client, account_id=account_id)
        return ServiceCapability(status=status.upper()), None
    except ComputeOptimizerError as exc:
        # ``AccessDeniedException`` -> ACCESS_DENIED; anything else -> UNAVAILABLE.
        cap_status = "ACCESS_DENIED" if exc.code == "AccessDeniedException" else "UNAVAILABLE"
        return ServiceCapability(status=cap_status, error_code=exc.code), exc.code
    except Exception as exc:  # pragma: no cover - defensive
        return (
            ServiceCapability(status="UNAVAILABLE", error_code=type(exc).__name__),
            type(exc).__name__,
        )


def _coh_capability(region: str, account_id: Optional[str]) -> tuple[ServiceCapability, Optional[str]]:
    try:
        client = get_cost_optimization_hub_client(region=region)
        status = list_enrollment_statuses(client, account_id=account_id)
        # AWS returns ``NOT_ENROLLED`` as a separate signal — map it
        # to the more explicit ``CapabilityStatus.NOT_ENROLLED`` so
        # the UI can show "hub not enrolled".
        if status == "INACTIVE":
            return ServiceCapability(status="NOT_ENROLLED"), None
        return ServiceCapability(status=status.upper()), None
    except CostOptimizationHubError as exc:
        cap_status = "ACCESS_DENIED" if exc.code == "AccessDeniedException" else "UNAVAILABLE"
        return ServiceCapability(status=cap_status, error_code=exc.code), exc.code
    except Exception as exc:  # pragma: no cover - defensive
        return (
            ServiceCapability(status="UNAVAILABLE", error_code=type(exc).__name__),
            type(exc).__name__,
        )


def build_capabilities(
    *,
    region: str,
    account_id: Optional[str],
) -> CapabilitiesResponse:
    """Return the capabilities endpoint payload."""
    co_cap, co_err = _co_capability(region, account_id)
    coh_cap, coh_err = _coh_capability(region, account_id)
    warnings: List[OptimizationNotice] = []
    if co_err:
        warnings.append(
            OptimizationNotice(
                source="compute_optimizer",
                code=co_err,
                message="Compute Optimizer enrollment status could not be determined.",
                region=region,
            )
        )
    if coh_err:
        warnings.append(
            OptimizationNotice(
                source="cost_optimization_hub",
                code=coh_err,
                message="Cost Optimization Hub enrollment status could not be determined.",
                region=region,
            )
        )
    return CapabilitiesResponse(
        region=region,
        account_id=account_id,
        compute_optimizer=co_cap,
        cost_optimization_hub=coh_cap,
        deterministic_engine=ServiceCapability(status="AVAILABLE"),
        supported_resource_types=[
            ResourceType.EC2,
            ResourceType.EBS_VOLUME,
            ResourceType.LAMBDA_FUNCTION,
            ResourceType.RDS_DB_INSTANCE,
            ResourceType.ELASTIC_IP,
            ResourceType.NAT_GATEWAY,
            ResourceType.LOAD_BALANCER,
        ],
        supported_lookback_days=[7, 30, 60, 90],
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Public entry point — assemble recommendations + summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptimizationInputs:
    """Inputs to the optimization engine.

    The route layer wires these from the FastAPI request.  ``days``
    is one of ``{7, 30, 60, 90}`` (Phase 2 + 3 invariant).
    """

    region: str
    account_id: Optional[str]
    days: int
    phase1_services: Dict[str, Any]
    utilization_by_resource: Dict[str, List[MetricSeries]]
    include_compute_optimizer: bool = True
    include_cost_optimization_hub: bool = True
    include_deterministic: bool = True


def build_recommendations(
    inputs: OptimizationInputs,
) -> tuple[List[Recommendation], List[OptimizationNotice]]:
    """Return ``(recommendations, warnings)``.

    Each AWS-native source is tried independently; a failure becomes
    a structured ``Warning`` and is NOT fatal.  Deterministic rules
    are always attempted (they have no remote dependency).

    Expected non-active enrollment states (``INACTIVE``,
    ``NOT_ENROLLED``, ``PENDING``, ``FAILED``) are checked FIRST
    using the resolved capability status from
    :func:`_co_capability` / :func:`_coh_capability`.  When the
    status is in that set the engine does NOT call the source's
    recommendation APIs and does NOT emit a warning — these are
    legitimate, non-error capability states.  Only ``ACTIVE`` (or an
    undetected status that happens to come back clean) triggers a
    real fetcher call.
    """
    warnings: List[OptimizationNotice] = []
    candidates: List[RecommendationCandidate] = []

    # AWS-native sources.  We tolerate failures from each.
    co_candidates: List[RecommendationCandidate] = []
    coh_candidates: List[RecommendationCandidate] = []

    if inputs.include_compute_optimizer:
        # Resolve the capability FIRST.  An expected non-active
        # status short-circuits before any recommendation API is
        # touched and produces no warning.
        co_cap, _co_err = _co_capability(inputs.region, inputs.account_id)
        if _should_fetch_aws_source(co_cap.status.value):
            try:
                co_candidates = _fetch_compute_optimizer_candidates(
                    region=inputs.region,
                    account_id=inputs.account_id,
                )
            except ComputeOptimizerError as exc:
                warnings.append(
                    OptimizationNotice(
                        source="compute_optimizer",
                        code=exc.code,
                        message="Compute Optimizer recommendations were not available.",
                        region=inputs.region,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(
                    OptimizationNotice(
                        source="compute_optimizer",
                        code=type(exc).__name__,
                        message="Compute Optimizer recommendations were not available.",
                        region=inputs.region,
                    )
                )
    candidates.extend(co_candidates)

    if inputs.include_cost_optimization_hub:
        # Same gating as Compute Optimizer: empty enrollment list
        # (``NOT_ENROLLED``) and ``INACTIVE`` are expected states
        # and must not surface as warnings.
        coh_cap, _coh_err = _coh_capability(inputs.region, inputs.account_id)
        if _should_fetch_aws_source(coh_cap.status.value):
            try:
                hub_rows = _fetch_cost_optimization_hub_candidates(
                    region=inputs.region,
                    account_id=inputs.account_id,
                )
                coh_candidates = [_coh_to_candidate(r) for r in hub_rows]
            except CostOptimizationHubError as exc:
                warnings.append(
                    OptimizationNotice(
                        source="cost_optimization_hub",
                        code=exc.code,
                        message="Cost Optimization Hub recommendations were not available.",
                        region=inputs.region,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive
                warnings.append(
                    OptimizationNotice(
                        source="cost_optimization_hub",
                        code=type(exc).__name__,
                        message="Cost Optimization Hub recommendations were not available.",
                        region=inputs.region,
                    )
                )
    candidates.extend(coh_candidates)

    # Deterministic rules.  Always run; pure local code.
    if inputs.include_deterministic:
        try:
            det_candidates = _fetch_deterministic_candidates(
                account_id=inputs.account_id,
                region=inputs.region,
                phase1_services=inputs.phase1_services,
                utilization_by_resource=inputs.utilization_by_resource,
                lookback_days=inputs.days,
            )
            candidates.extend(
                _deterministic_to_candidate(c, account_id=inputs.account_id)
                for c in det_candidates
            )
        except Exception as exc:  # pragma: no cover - defensive
            warnings.append(
                OptimizationNotice(
                    source="deterministic_rules",
                    code=type(exc).__name__,
                    message="Deterministic rule engine failed.",
                    region=inputs.region,
                )
            )

    # Dedup.
    deduped = _deduplicate(candidates)

    # Stable ordering: highest savings first (UNKNOWN last), then by
    # resource_id so the response is deterministic across calls.
    deduped.sort(
        key=lambda c: (
            -(c.estimated_monthly_savings or Decimal("0")),
            c.resource_id,
            c.action.value,
        )
    )

    recommendations = [_candidate_to_recommendation(c, index=i) for i, c in enumerate(deduped)]
    return recommendations, warnings


def build_summary(
    *,
    region: str,
    account_id: Optional[str],
    days: int,
    recommendations: List[Recommendation],
    warnings: List[OptimizationNotice],
) -> SummaryResponse:
    """Aggregate a deduplicated recommendation list into a summary."""
    by_resource_type: Dict[str, dict[str, Any]] = {}
    by_action: Dict[str, dict[str, Any]] = {}
    by_source: Dict[str, dict[str, Any]] = {}
    by_confidence: Dict[str, dict[str, Any]] = {}
    total_savings = Decimal("0")
    without_savings = 0
    status = OptimizationStatus.SUCCESS if not warnings else OptimizationStatus.PARTIAL_SUCCESS
    if not recommendations and warnings:
        status = OptimizationStatus.FAILED

    for rec in recommendations:
        savings = rec.estimated_monthly_savings
        if savings is None:
            without_savings += 1
        else:
            total_savings += savings
        _bump(by_resource_type, rec.resource_type.value, savings)
        _bump(by_action, rec.action.value, savings)
        # Source attribution: walk the sources list so dedup is
        # reflected (a row attributed to BOTH hub and CO shows up
        # under each source).
        for src in rec.sources:
            _bump(by_source, src.value, savings)
        _bump(by_confidence, rec.confidence.value, savings)

    return SummaryResponse(
        region=region,
        account_id=account_id,
        days=days,
        status=status,
        total_recommendations=len(recommendations),
        total_estimated_monthly_savings=total_savings if total_savings > 0 else None,
        currency="USD",
        by_resource_type=_to_summary_list(by_resource_type),
        by_action=_to_summary_list(by_action),
        by_source=_to_summary_list(by_source),
        by_confidence=_to_summary_list(by_confidence),
        recommendations_without_savings=without_savings,
        warnings=warnings,
    )


def _bump(bucket: Dict[str, dict[str, Any]], key: str, savings: Optional[Decimal]) -> None:
    entry = bucket.setdefault(key, {"count": 0, "savings": Decimal("0")})
    entry["count"] += 1
    if savings is not None:
        entry["savings"] += savings


def _to_summary_list(bucket: Dict[str, dict[str, Any]]) -> List[SummaryByCategory]:
    out: List[SummaryByCategory] = []
    for key, value in bucket.items():
        savings = value["savings"]
        out.append(
            SummaryByCategory(
                key=key,
                count=value["count"],
                estimated_monthly_savings=savings if savings > 0 else None,
                currency="USD",
            )
        )
    out.sort(key=lambda r: (r.key or ""))
    return out


__all__ = [
    "OptimizationInputs",
    "SOURCE_PRECEDENCE",
    "build_capabilities",
    "build_recommendations",
    "build_summary",
]
