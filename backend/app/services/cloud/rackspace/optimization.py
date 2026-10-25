"""Rackspace deterministic optimization engine — Phase 9C.1.

Internal-only.  Not wired into the public ``/api/cloud/recommendations``
route in this phase; that integration is Phase 9C.2.

This module consumes already-normalized canonical Rackspace inputs:

* :class:`~app.services.cloud.models.CloudResource` — the canonical
  resource inventory produced by Phase 9B.2.  Volumes already carry
  ``metadata.attachment_state == 'DETACHED' | 'ATTACHED'`` so the
  detached-volume rule does not need to make any attachment inference.
* :class:`~app.services.cloud.models.CloudCostSummary` — the
  canonical current-billing-cycle cost snapshot from Phase 9B.1.
  Only the aggregate ``current_total`` and ``currency`` are read
  here as account-level context; no per-resource allocation is ever
  attempted.

It produces deterministic, evidence-backed findings.  No AWS / Azure /
Rackspace product API calls are made by the engine itself.

Determinism contract
--------------------

* Every :class:`RackspaceFinding` carries a stable ``finding_id``
  derived from ``SHA-256(provider|account|rule|region|resource_type|resource_id)``.
* Output order is deterministic: severity desc, then rule_id, then
  a stable priority key (e.g. volume size desc), then region, then
  resource_id.
* ``estimated_monthly_savings`` is ``None`` whenever pricing evidence
  is insufficient — never ``Decimal("0")`` to mean "unknown".
* No fabrication of CPU / memory / network / disk utilization, idle
  duration, or historical behavior.  When evidence is missing the
  engine emits a review candidate with explicit limitations.

Rules in scope for 9C.1
-----------------------

* ``RS-VOL-001`` — detached block storage review
* ``RS-SRV-001`` — non-active server review
* ``RS-LB-001``  — load balancer review (no finding for empty inventory)

Rules deliberately deferred
---------------------------

* ``RS-SRV-002`` rightsizing — gated by
  :func:`app.services.cloud.rackspace.metrics_capability.probe_metrics_capability`.
  In 9C.1 that probe returns ``DEFERRED`` so ACTIVE servers never
  produce a rightsizing candidate.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.services.cloud.models import (
    CloudCostSummary,
    CloudProviderKind,
    CloudResource,
)

logger = logging.getLogger("cost-detective.cloud.rackspace.optimization")


# ---------------------------------------------------------------------------
# Severity / confidence vocabulary
# ---------------------------------------------------------------------------


class RackspaceFindingSeverity(str, Enum):
    """Deterministic severity levels for Rackspace findings.

    Vocabulary reuses the AWS / Azure pattern (LOW / MEDIUM / HIGH).
    Security / availability severity is NOT conflated with FinOps
    priority: a detached volume is a cost candidate, not an outage.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RackspaceFindingConfidence(str, Enum):
    """Evidence-driven confidence levels.

    * ``HIGH`` — the rule's primary signal is authoritative (e.g.
      volume attachment_state is a direct API observation) and every
      secondary claim is also supported.
    * ``MEDIUM`` — the primary signal is authoritative but at least
      one secondary claim (e.g. resource-level price, historical
      duration) is unsupported.
    * ``LOW`` — the primary signal is observational but not
      cost-asserting (e.g. non-ACTIVE server state without usage).
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RackspaceOptimizationStatus(str, Enum):
    """Overall engine status for a single evaluation."""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    EMPTY = "EMPTY"


# Severity rank for sorting (higher number == more urgent).
SEVERITY_RANK: Dict[RackspaceFindingSeverity, int] = {
    RackspaceFindingSeverity.LOW: 0,
    RackspaceFindingSeverity.MEDIUM: 1,
    RackspaceFindingSeverity.HIGH: 2,
}


# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RackspaceFinding:
    """A single deterministic Rackspace optimization finding.

    The shape mirrors the canonical optimization envelope closely
    enough that Phase 9C.2 can map a finding onto a public
    recommendation without field translation, but the model lives in
    this internal module so the public schema cannot be polluted
    while 9C.1 is still under construction.
    """

    finding_id: str
    provider: str  # always "RACKSPACE" — kept as a plain str so the
    # type stays literal-narrow and easier to serialize.
    account_id: Optional[str]
    rule_id: str
    category: str
    resource_type: str
    resource_id: str
    resource_name: Optional[str]
    region: Optional[str]
    severity: RackspaceFindingSeverity
    confidence: RackspaceFindingConfidence
    title: str
    summary: str
    recommended_action: str
    evidence: Tuple[Dict[str, Any], ...]
    estimated_monthly_savings: Optional[Decimal]
    savings_basis: str  # "UNAVAILABLE" or a documented string.
    limitations: Tuple[str, ...]
    source: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict (Decimals stringified)."""
        return {
            "finding_id": self.finding_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "rule_id": self.rule_id,
            "category": self.category,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_name": self.resource_name,
            "region": self.region,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "title": self.title,
            "summary": self.summary,
            "recommended_action": self.recommended_action,
            "evidence": list(self.evidence),
            "estimated_monthly_savings": (
                str(self.estimated_monthly_savings)
                if self.estimated_monthly_savings is not None
                else None
            ),
            "savings_basis": self.savings_basis,
            "limitations": list(self.limitations),
            "source": self.source,
        }


# ---------------------------------------------------------------------------
# Result summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RackspaceOptimizationResult:
    """The internal deterministic summary of a single engine run."""

    status: RackspaceOptimizationStatus
    provider: str  # always "RACKSPACE"
    account_id: Optional[str]
    region: Optional[str]
    resources_evaluated: int
    findings: Tuple[RackspaceFinding, ...]
    findings_total: int
    findings_by_rule: Dict[str, int]
    findings_by_resource_type: Dict[str, int]
    findings_by_severity: Dict[str, int]
    estimated_savings_total: Optional[Decimal]
    estimated_savings_known_count: int
    estimated_savings_unknown_count: int
    limitations: Tuple[str, ...]
    evaluated_at_iso: str  # ISO-8601 UTC stamp; set by caller for determinism

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "provider": self.provider,
            "account_id": self.account_id,
            "region": self.region,
            "resources_evaluated": self.resources_evaluated,
            "findings_total": self.findings_total,
            "findings": [f.to_dict() for f in self.findings],
            "findings_by_rule": dict(self.findings_by_rule),
            "findings_by_resource_type": dict(self.findings_by_resource_type),
            "findings_by_severity": dict(self.findings_by_severity),
            "estimated_savings_total": (
                str(self.estimated_savings_total)
                if self.estimated_savings_total is not None
                else None
            ),
            "estimated_savings_known_count": self.estimated_savings_known_count,
            "estimated_savings_unknown_count": self.estimated_savings_unknown_count,
            "limitations": list(self.limitations),
            "evaluated_at_iso": self.evaluated_at_iso,
        }


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def deterministic_finding_id(
    *,
    provider: str,
    account_id: Optional[str],
    rule_id: str,
    region: Optional[str],
    resource_type: str,
    resource_id: str,
) -> str:
    """Return a stable finding id of the form ``rs-det-<rule>-<hash12>``.

    The hash is SHA-256 truncated to 12 hex chars over the stable
    tuple ``provider|account|rule|region|resource_type|resource_id``.
    Same inputs always yield the same id; different inputs never
    collide in practice.
    """
    payload = "|".join(
        [
            provider.upper(),
            account_id or "unknown-account",
            rule_id,
            (region or "unknown-region").upper(),
            resource_type.upper(),
            resource_id,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"rs-det-{rule_id.lower()}-{digest}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RACKSPACE_PROVIDER_LABEL = "RACKSPACE"


def _provider_label() -> str:
    return _RACKSPACE_PROVIDER_LABEL


def _safe_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata(resource: CloudResource) -> Dict[str, Any]:
    meta = resource.metadata
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return {k: v for k, v in meta.items()}
    return dict(meta)


def _priority_key_volume(resource: CloudResource) -> Tuple[int, str, str]:
    """Sort key for detached volumes: size desc, region, resource_id.

    A negative size makes ascending sort give largest first.
    """
    size = _safe_int(_metadata(resource).get("size_gb"))
    return (
        -1 * (size if size is not None else -1),
        (resource.region or "").upper(),
        resource.resource_id or "",
    )


def _priority_key_server(resource: CloudResource) -> Tuple[str, str]:
    return (
        (resource.region or "").upper(),
        resource.resource_id or "",
    )


def _priority_key_lb(resource: CloudResource) -> Tuple[str, str]:
    return (
        (resource.region or "").upper(),
        resource.resource_id or "",
    )


# ---------------------------------------------------------------------------
# Rule RS-VOL-001 — Detached block storage review
# ---------------------------------------------------------------------------

RULE_VOL_DETACHED = "RS-VOL-001"
CATEGORY_VOLUME = "STORAGE_REVIEW"
RULE_VOL_TITLE = "Detached block storage volume requires review"


def _evidence_for_volume(resource: CloudResource) -> Tuple[Dict[str, Any], ...]:
    meta = _metadata(resource)
    evidence: List[Dict[str, Any]] = [
        {"kind": "volume_status", "value": resource.state},
        {
            "kind": "attachment_state",
            "value": meta.get("attachment_state"),
        },
    ]
    size_gb = _safe_int(meta.get("size_gb"))
    if size_gb is not None:
        evidence.append({"kind": "size_gb", "value": size_gb})
    if meta.get("volume_type"):
        evidence.append({"kind": "volume_type", "value": meta.get("volume_type")})
    if resource.region:
        evidence.append({"kind": "region", "value": resource.region})
    created = meta.get("created")
    if created:
        # NOTE: created is "resource age", NOT "detached duration".
        evidence.append({"kind": "created_at", "value": created})
    if meta.get("availability_zone"):
        evidence.append({"kind": "availability_zone", "value": meta.get("availability_zone")})
    return tuple(evidence)


def rule_rs_vol_001_detached(
    resources: Iterable[CloudResource],
) -> List[RackspaceFinding]:
    """Emit a REVIEW candidate for every detached volume.

    Criteria (deterministic, evidence-backed):

    * ``resource_type == 'VOLUME'``
    * ``metadata.attachment_state == 'DETACHED'``

    The API state (``state``) is captured as evidence but is not the
    sole trigger — a volume in 'in-use' state with a populated
    attachments list is attached even if a transient field were
    blank.  When both signals agree we treat the volume as
    genuinely detached.

    Findings are explicitly REVIEW only.  We do NOT recommend
    deletion, we do NOT claim detached duration, we do NOT compute
    savings.  Severity escalates to ``HIGH`` only when the size is
    known AND > 100 GB — a conservative threshold documented in the
    phase 9C.1 plan.
    """
    out: List[RackspaceFinding] = []
    for resource in resources:
        if resource.resource_type != "VOLUME":
            continue
        meta = _metadata(resource)
        if meta.get("attachment_state") != "DETACHED":
            continue
        # Defense in depth: if the API status is clearly attached,
        # do not surface a false positive.
        api_status = (resource.state or "").lower()
        if api_status in {"in-use", "attaching", "detaching"}:
            # "in-use" / "attaching" / "detaching" contradicts DETACHED.
            # The canonical resource layer is authoritative but a
            # residual row would be a real inconsistency; skip rather
            # than emit a misleading finding.
            continue
        size_gb = _safe_int(meta.get("size_gb"))
        severity = RackspaceFindingSeverity.MEDIUM
        if size_gb is not None and size_gb > 100:
            severity = RackspaceFindingSeverity.HIGH
        finding_id = deterministic_finding_id(
            provider=_provider_label(),
            account_id=resource.account_id,
            rule_id=RULE_VOL_DETACHED,
            region=resource.region,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )
        limitations = (
            "detached-now does not imply detached-for-N-days",
            "resource-level price is unavailable; monthly savings cannot be proven",
            "created_at reflects resource age, not detachment duration",
        )
        out.append(
            RackspaceFinding(
                finding_id=finding_id,
                provider=_provider_label(),
                account_id=resource.account_id,
                rule_id=RULE_VOL_DETACHED,
                category=CATEGORY_VOLUME,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                resource_name=resource.name,
                region=resource.region,
                severity=severity,
                confidence=RackspaceFindingConfidence.MEDIUM,
                title=RULE_VOL_TITLE,
                summary=(
                    f"Block storage volume {resource.resource_id} in "
                    f"{resource.region or 'unknown region'} is currently "
                    f"detached. Owner validation is required before any "
                    f"retirement decision."
                ),
                recommended_action=(
                    "Review this detached volume. Confirm whether it is still "
                    "required, whether a snapshot/backup is needed, and remove "
                    "it only after owner validation if it is no longer needed."
                ),
                evidence=_evidence_for_volume(resource),
                estimated_monthly_savings=None,
                savings_basis="UNAVAILABLE",
                limitations=limitations,
                source="RACKSPACE_CLOUD_API",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule RS-SRV-001 — Non-active server review
# ---------------------------------------------------------------------------

RULE_SRV_STATE = "RS-SRV-001"
CATEGORY_SERVER = "SERVER_REVIEW"
RULE_SRV_TITLE = "Non-active server requires review"

NON_ACTIVE_STATES: Tuple[str, ...] = (
    "SHUTOFF",
    "SUSPENDED",
    "PAUSED",
    "ERROR",
)


def _server_summary(resource: CloudResource, state: str) -> str:
    flavor = _metadata(resource).get("flavor_name") or _metadata(resource).get(
        "flavor_id"
    )
    if flavor:
        return (
            f"Cloud Server {resource.resource_id} in "
            f"{resource.region or 'unknown region'} is in state "
            f"{state} (flavor={flavor}). Review server purpose "
            f"before retaining or retiring."
        )
    return (
        f"Cloud Server {resource.resource_id} in "
        f"{resource.region or 'unknown region'} is in state "
        f"{state}. Review server purpose before retaining or retiring."
    )


def _is_error_state(state: str) -> bool:
    return state.upper() == "ERROR"


def rule_rs_srv_001_non_active(
    resources: Iterable[CloudResource],
) -> List[RackspaceFinding]:
    """Emit a REVIEW candidate for every non-ACTIVE server.

    States considered:

    * ``SHUTOFF``, ``SUSPENDED``, ``PAUSED`` — cost-idle candidates;
      the review action covers retention vs retirement.
    * ``ERROR`` — operational state that may require remediation,
      not automatic retirement.

    ``ACTIVE`` is intentionally NOT a rightsizing signal: a flavor
    alone does not prove underutilization.  Rightsizing is gated by
    metrics evidence (see
    :func:`app.services.cloud.rackspace.metrics_capability.probe_metrics_capability`).
    """
    out: List[RackspaceFinding] = []
    for resource in resources:
        if resource.resource_type != "SERVER":
            continue
        state = (resource.state or "").upper()
        if state not in NON_ACTIVE_STATES:
            continue
        finding_id = deterministic_finding_id(
            provider=_provider_label(),
            account_id=resource.account_id,
            rule_id=RULE_SRV_STATE,
            region=resource.region,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )
        error_state = _is_error_state(state)
        limitations_list = [
            "utilization metrics are not part of the canonical evidence layer; "
            "historical usage is unknown",
            "resource-level price is unavailable; monthly savings cannot be proven",
            "non-ACTIVE state alone does not prove the resource is wasted",
        ]
        if error_state:
            limitations_list.append(
                "ERROR state may require remediation rather than retirement"
            )
        recommended_action = (
            "Review server purpose and state. Confirm whether retention, "
            "remediation, or retirement is the appropriate action."
        )
        evidence: List[Dict[str, Any]] = [
            {"kind": "server_state", "value": state},
            {"kind": "region", "value": resource.region},
        ]
        flavor = _metadata(resource).get("flavor_name") or _metadata(
            resource
        ).get("flavor_id")
        if flavor:
            evidence.append({"kind": "flavor", "value": flavor})
        created = _metadata(resource).get("created")
        if created:
            evidence.append({"kind": "created_at", "value": created})
        out.append(
            RackspaceFinding(
                finding_id=finding_id,
                provider=_provider_label(),
                account_id=resource.account_id,
                rule_id=RULE_SRV_STATE,
                category=CATEGORY_SERVER,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                resource_name=resource.name,
                region=resource.region,
                severity=RackspaceFindingSeverity.LOW,
                confidence=RackspaceFindingConfidence.LOW,
                title=RULE_SRV_TITLE,
                summary=_server_summary(resource, state),
                recommended_action=recommended_action,
                evidence=tuple(evidence),
                estimated_monthly_savings=None,
                savings_basis="UNAVAILABLE",
                limitations=tuple(limitations_list),
                source="RACKSPACE_CLOUD_API",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule RS-LB-001 — Load balancer review
# ---------------------------------------------------------------------------

RULE_LB_REVIEW = "RS-LB-001"
CATEGORY_LOAD_BALANCER = "LOAD_BALANCER_REVIEW"


def rule_rs_lb_001_review(
    resources: Iterable[CloudResource],
) -> List[RackspaceFinding]:
    """Emit a review candidate ONLY when a load balancer has strong
    evidence of being unused.

    The Phase 9C.1 catalog exposes descriptive configuration
    (protocol / port / algorithm / created) — not connectivity or
    traffic state.  Without a reliable API signal (zero backend
    nodes, ``status='DISABLED'``, etc.) we deliberately produce NO
    finding rather than fabricate one.  An empty LB inventory is a
    successful outcome, not an opportunity.
    """
    out: List[RackspaceFinding] = []
    for resource in resources:
        if resource.resource_type != "LOAD_BALANCER":
            continue
        # Empty status == DELETE candidate; otherwise there is no
        # current API signal in scope.  Future subphases that
        # surface backend-node counts or status='DISABLED' may
        # tighten this filter.
        status = (resource.state or "").upper()
        if status != "DELETED":
            continue
        finding_id = deterministic_finding_id(
            provider=_provider_label(),
            account_id=resource.account_id,
            rule_id=RULE_LB_REVIEW,
            region=resource.region,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )
        out.append(
            RackspaceFinding(
                finding_id=finding_id,
                provider=_provider_label(),
                account_id=resource.account_id,
                rule_id=RULE_LB_REVIEW,
                category=CATEGORY_LOAD_BALANCER,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                resource_name=resource.name,
                region=resource.region,
                severity=RackspaceFindingSeverity.LOW,
                confidence=RackspaceFindingConfidence.HIGH,
                title="Deleted load balancer still listed in inventory",
                summary=(
                    f"Load balancer {resource.resource_id} is in DELETED "
                    f"state but still appears in the canonical inventory. "
                    f"Confirm whether the resource should be purged."
                ),
                recommended_action=(
                    "Confirm whether this deleted load balancer should be "
                    "purged from the account."
                ),
                evidence=(
                    {"kind": "lb_status", "value": status},
                    {"kind": "region", "value": resource.region},
                ),
                estimated_monthly_savings=None,
                savings_basis="UNAVAILABLE",
                limitations=(
                    "LB rule only triggers on DELETED status in Phase 9C.1",
                ),
                source="RACKSPACE_CLOUD_API",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def _sort_key(finding: RackspaceFinding) -> Tuple[int, str, int, str, str, str]:
    """Stable multi-axis sort key.

    Order: severity desc, rule_id, priority integer desc, region, resource_id.
    The priority integer is volume size (or 0 when not applicable) so
    larger detached volumes surface first.
    """
    priority_size = 0
    for ev in finding.evidence:
        if ev.get("kind") == "size_gb":
            try:
                priority_size = int(ev.get("value") or 0)
            except (TypeError, ValueError):
                priority_size = 0
            break
    return (
        -SEVERITY_RANK[finding.severity],
        finding.rule_id,
        -priority_size,
        (finding.region or "").upper(),
        finding.resource_id,
        finding.finding_id,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def evaluate_rackspace_optimization(
    resources: Sequence[CloudResource],
    *,
    cost_context: Optional[CloudCostSummary] = None,
    rightsizing_evidence_status: str = "DEFERRED",
    evaluated_at_iso: str = "",
) -> RackspaceOptimizationResult:
    """Run every deterministic rule and return a stable summary.

    Parameters
    ----------
    resources:
        The canonical Rackspace resource inventory (Phase 9B.2
        normalized).  Resources belonging to other providers are
        silently ignored.
    cost_context:
        Optional canonical cost summary (Phase 9B.1).  Only the
        aggregate current total and currency are exposed as
        account-level context in the summary limitations.
    rightsizing_evidence_status:
        Result of the metrics capability probe.  In 9C.1 this is
        always ``"DEFERRED"`` so ACTIVE servers never produce a
        rightsizing candidate.
    evaluated_at_iso:
        Optional fixed evaluation timestamp (UTC, ISO-8601) used by
        the caller to keep determinism tests stable.  When empty the
        engine sets a stable sentinel (``"1970-01-01T00:00:00+00:00"``)
        so repeated invocations remain byte-identical.
    """
    evaluated_at = evaluated_at_iso or "1970-01-01T00:00:00+00:00"

    # Provider isolation: silently ignore any non-Rackspace resource.
    rackspace_resources: List[CloudResource] = [
        r for r in resources if r.provider == CloudProviderKind.RACKSPACE
    ]

    # Rule scope: only resources whose inventory returned a valid
    # service entry.  A resource with a FAILED service (e.g. service
    # status='error') is still fed into the rules — the canonical
    # layer is what failed to inventory, not the rule engine.
    findings: List[RackspaceFinding] = []
    findings.extend(rule_rs_vol_001_detached(rackspace_resources))
    findings.extend(rule_rs_srv_001_non_active(rackspace_resources))
    findings.extend(rule_rs_lb_001_review(rackspace_resources))

    findings.sort(key=_sort_key)

    findings_by_rule: Dict[str, int] = {}
    findings_by_resource_type: Dict[str, int] = {}
    findings_by_severity: Dict[str, int] = {}
    savings_known = 0
    savings_unknown = 0
    savings_total: Optional[Decimal] = None
    for f in findings:
        findings_by_rule[f.rule_id] = findings_by_rule.get(f.rule_id, 0) + 1
        findings_by_resource_type[f.resource_type] = (
            findings_by_resource_type.get(f.resource_type, 0) + 1
        )
        sev_key = f.severity.value
        findings_by_severity[sev_key] = findings_by_severity.get(sev_key, 0) + 1
        if f.estimated_monthly_savings is None:
            savings_unknown += 1
        else:
            savings_known += 1
            savings_total = (
                (savings_total or Decimal("0"))
                + f.estimated_monthly_savings
            )

    limitations: List[str] = [
        "deterministic-only: ACTIVE servers do not generate rightsizing findings "
        "without utilization metrics evidence",
        f"rightsizing_evidence_status={rightsizing_evidence_status}",
        "estimated_monthly_savings is unavailable for every finding in 9C.1 — "
        "Rackspace Billing V2 provides cycle-level totals only, with no "
        "reliable per-resource price mapping",
    ]
    if cost_context is not None:
        limitations.append(
            "account-level current billing cycle cost is exposed as context only; "
            "no proportional allocation across resources is performed"
        )

    if not rackspace_resources:
        status = RackspaceOptimizationStatus.EMPTY
    elif findings:
        status = RackspaceOptimizationStatus.SUCCESS
    else:
        status = RackspaceOptimizationStatus.SUCCESS

    account_id: Optional[str] = None
    region: Optional[str] = None
    if rackspace_resources:
        account_id = rackspace_resources[0].account_id
        region = rackspace_resources[0].region

    return RackspaceOptimizationResult(
        status=status,
        provider=_provider_label(),
        account_id=account_id,
        region=region,
        resources_evaluated=len(rackspace_resources),
        findings=tuple(findings),
        findings_total=len(findings),
        findings_by_rule=dict(sorted(findings_by_rule.items())),
        findings_by_resource_type=dict(sorted(findings_by_resource_type.items())),
        findings_by_severity=dict(sorted(findings_by_severity.items())),
        estimated_savings_total=savings_total if savings_known > 0 else None,
        estimated_savings_known_count=savings_known,
        estimated_savings_unknown_count=savings_unknown,
        limitations=tuple(limitations),
        evaluated_at_iso=evaluated_at,
    )


__all__ = [
    "CATEGORY_LOAD_BALANCER",
    "CATEGORY_SERVER",
    "CATEGORY_VOLUME",
    "NON_ACTIVE_STATES",
    "RULE_LB_REVIEW",
    "RULE_SRV_STATE",
    "RULE_VOL_DETACHED",
    "RULE_VOL_TITLE",
    "RULE_SRV_TITLE",
    "RackspaceFinding",
    "RackspaceFindingConfidence",
    "RackspaceFindingSeverity",
    "RackspaceOptimizationResult",
    "RackspaceOptimizationStatus",
    "SEVERITY_RANK",
    "deterministic_finding_id",
    "evaluate_rackspace_optimization",
    "rule_rs_lb_001_review",
    "rule_rs_srv_001_non_active",
    "rule_rs_vol_001_detached",
]
