"""Deterministic optimization rule engine — Phase 3.

Pure / deterministic where reasonably possible.  Each rule takes
inputs derived from Phase 1 (resource metadata) and Phase 2
(CloudWatch utilization evidence) and produces zero or more
:class:`DeterministicCandidate` rows.  No AWS call happens in this
module — the orchestrator wires the candidates into the public
recommendation model after applying deduplication.

The rules here are deliberately conservative:

* Missing CloudWatch datapoints are NEVER treated as "zero
  utilization".  A resource without coverage is reported as
  ``LOW`` confidence and stays out of the savings aggregate.
* EBS savings are NEVER computed locally — AWS does not expose
  per-volume pricing reliably enough to make a Phase 3 estimate
  trustworthy.  We emit a REVIEW candidate and let downstream
  tooling look up the price.
* EIP / NAT / LB review candidates are also unpriced.  The savings
  field stays ``None`` and ``SavingsSource.UNKNOWN`` is set
  explicitly so the frontend never inflates the total.
* EC2 / RDS rule output is REVIEW-only.  We never pick a replacement
  instance type from heuristics — Compute Optimizer or Cost
  Optimization Hub own that decision when they are available.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from app.schemas.optimization import (
    Confidence,
    RecommendationAction,
    ResourceType,
    SavingsSource,
)
from app.services.aws.cloudwatch_metrics import DataQuality, MetricSeries

logger = logging.getLogger("cost-detective-backend.optimization_rules")


# ---------------------------------------------------------------------------
# Thresholds — pinned from the Phase 3 spec.
# ---------------------------------------------------------------------------

# Rule 3 — Low-utilization EC2:
#   CPU average < 10% AND CPU maximum < 40% AND lookback >= 7 days AND
#   data quality acceptable.  These are conservative defaults; the
#   values are centralized here so future tuning touches one place.
EC2_CPU_AVG_MAX_THRESHOLD = Decimal("10")
EC2_CPU_MAX_THRESHOLD = Decimal("40")
EC2_RULE_MIN_LOOKBACK_DAYS = 7
EC2_ACCEPTABLE_DATA_QUALITIES = {DataQuality.HIGH, DataQuality.MEDIUM}

# Rule 6 — RDS underutilization:
#   CPU average < 10% AND CPU maximum < 40% AND DatabaseConnections
#   average < 5 AND data quality acceptable.
RDS_CPU_AVG_MAX_THRESHOLD = Decimal("10")
RDS_CPU_MAX_THRESHOLD = Decimal("40")
RDS_DB_CONNECTIONS_AVG_THRESHOLD = Decimal("5")
RDS_ACCEPTABLE_DATA_QUALITIES = {DataQuality.HIGH, DataQuality.MEDIUM}

# Rule 4 — Idle NAT Gateway:
#   NAT BytesIn/Out / PacketsIn/Out data is sparse in our evidence
#   layer; we require a single HIGH-quality metric with all
#   datapoints equal to zero.  If we have ANY datapoint with
#   coverage < HIGH we skip the rule (no-data = no claim).
NAT_IDLE_REQUIRED_DATA_QUALITY = DataQuality.HIGH

# Rule 5 — Idle Load Balancer:
#   Require HIGH data quality and ALL datapoints equal to zero.
#   ALB RequestCount OR ALB ProcessedBytes -> idle when all zero.
#   NLB ProcessedBytes -> idle when all zero.
LB_IDLE_REQUIRED_DATA_QUALITY = DataQuality.HIGH


# ---------------------------------------------------------------------------
# Internal dataclass — orchestrator consumes this and converts to Pydantic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicCandidate:
    """A rule-emitted recommendation candidate.

    The orchestrator is responsible for assigning the public
    ``recommendation_id`` (deterministic, derived from inputs) and
    wrapping it in the public Pydantic schema.
    """

    resource_id: str
    resource_arn: Optional[str]
    resource_type: ResourceType
    region: str
    action: RecommendationAction
    title: str
    finding: str
    current_configuration: Dict[str, Any]
    recommended_configuration: Dict[str, Any]
    confidence: Confidence
    data_quality: str
    reason_codes: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ID generation — deterministic, stable, not a hash of arbitrary inputs.
# ---------------------------------------------------------------------------


def deterministic_recommendation_id(
    *,
    account_id: Optional[str],
    region: str,
    resource_id: str,
    action: RecommendationAction,
    suffix: Optional[str] = None,
) -> str:
    """Return a stable id of the form ``det-<action>-<hash>``.

    The hash is SHA-256 truncated to 16 hex chars, computed over
    ``account|region|resource_id|action`` so the same resource always
    yields the same id across requests.  ``suffix`` lets the
    orchestrator distinguish rule rows that share inputs (e.g. two
    LB idle rules on the same ARN, one per metric set).
    """
    payload = "|".join(
        [
            account_id or "unknown-account",
            region or "unknown-region",
            resource_id or "unknown-resource",
            action.value,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    if suffix:
        return f"det-{action.value.lower()}-{digest}-{suffix}"
    return f"det-{action.value.lower()}-{digest}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensive
        return None


def _average_cpu(series: List[MetricSeries]) -> Optional[Decimal]:
    """Return the average of all ``CPUUtilization|Average`` datapoints.

    Returns ``None`` when no datapoints are present so the rule
    short-circuits cleanly without inventing a value.
    """
    samples: List[Decimal] = []
    for s in series:
        if s.metric_name == "CPUUtilization" and s.statistic == "Average":
            samples.extend(d.value for d in s.datapoints)
    if not samples:
        return None
    total = sum(samples)
    return (total / Decimal(len(samples))).quantize(Decimal("0.01"))


def _max_cpu(series: List[MetricSeries]) -> Optional[Decimal]:
    samples: List[Decimal] = []
    for s in series:
        if s.metric_name == "CPUUtilization" and s.statistic == "Maximum":
            samples.extend(d.value for d in s.datapoints)
    if not samples:
        return None
    return max(samples).quantize(Decimal("0.01"))


def _avg_connections(series: List[MetricSeries]) -> Optional[Decimal]:
    samples: List[Decimal] = []
    for s in series:
        if s.metric_name == "DatabaseConnections" and s.statistic == "Average":
            samples.extend(d.value for d in s.datapoints)
    if not samples:
        return None
    return (sum(samples) / Decimal(len(samples))).quantize(Decimal("0.01"))


def _worst_quality(series: List[MetricSeries]) -> DataQuality:
    """Return the worst (lowest) data quality across all series."""
    if not series:
        return DataQuality.NO_DATA
    order = {
        DataQuality.HIGH: 0,
        DataQuality.MEDIUM: 1,
        DataQuality.LOW: 2,
        DataQuality.NO_DATA: 3,
    }
    return min(series, key=lambda s: order.get(s.data_quality, 4)).data_quality


def _is_all_zero(series: List[MetricSeries]) -> bool:
    """Return ``True`` only when every datapoint across every series is zero.

    Requires at least one datapoint — empty series is treated as
    "no evidence" and skipped by the caller.
    """
    if not series:
        return False
    total = sum((len(s.datapoints) for s in series))
    if total == 0:
        return False
    zeros = sum(
        1 for s in series for d in s.datapoints if d.value == Decimal("0")
    )
    return zeros == total


# ---------------------------------------------------------------------------
# Rule 1 — Unattached EBS
# ---------------------------------------------------------------------------


def rule_unattached_ebs(
    *,
    account_id: Optional[str],
    region: str,
    ebs_volumes: Iterable[Dict[str, Any]],
) -> List[DeterministicCandidate]:
    """Emit REVIEW_DELETE_UNATTACHED_EBS for each available+unattached volume.

    Phase 1 enumerates ``state`` from ``describe_volumes``; we treat
    ``state == "available"`` as "unattached".  ``size_gb`` and
    ``region`` are echoed into the evidence block so a UI can show
    the volume size without re-querying AWS.
    """
    out: List[DeterministicCandidate] = []
    for vol in ebs_volumes:
        if vol.get("state") != "available":
            continue
        volume_id = vol.get("volume_id")
        if not volume_id:
            continue
        size_gb = vol.get("size_gb")
        attachments = vol.get("attachments") or []
        out.append(
            DeterministicCandidate(
                resource_id=volume_id,
                resource_arn=None,
                resource_type=ResourceType.EBS_VOLUME,
                region=region,
                action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
                title=f"Unattached EBS volume {volume_id}",
                finding="EBS volume is in the 'available' state with zero attachments.",
                current_configuration={
                    "volume_id": volume_id,
                    "size_gb": size_gb,
                    "state": vol.get("state"),
                    "availability_zone": vol.get("availability_zone"),
                    "attachment_count": len(attachments),
                },
                recommended_configuration={
                    "recommendation": "REVIEW_DELETE_UNATTACHED_EBS",
                },
                confidence=Confidence.HIGH,
                data_quality=DataQuality.HIGH.value,
                reason_codes=["EBS_AVAILABLE_STATE"],
                evidence={
                    "size_gb": size_gb,
                    "state": vol.get("state"),
                    "availability_zone": vol.get("availability_zone"),
                    "attachment_count": len(attachments),
                    "tags": vol.get("tags") or {},
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule 2 — Unused Elastic IP
# ---------------------------------------------------------------------------


def rule_unused_eip(
    *,
    account_id: Optional[str],
    region: str,
    elastic_ips: Iterable[Dict[str, Any]],
) -> List[DeterministicCandidate]:
    """Emit REVIEW_RELEASE_UNUSED_EIP for each allocated-but-unassociated EIP.

    ``describe_addresses`` returns rows with no ``AssociationId`` when
    the address is not attached to any instance or network interface.
    Some accounts also surface ``private_ip`` only; either way, the
    absence of ``AssociationId`` is the canonical signal.
    """
    out: List[DeterministicCandidate] = []
    for eip in elastic_ips:
        if eip.get("association_id"):
            continue
        if eip.get("instance_id") or eip.get("network_interface_id"):
            continue
        public_ip = eip.get("public_ip")
        if not public_ip:
            continue
        out.append(
            DeterministicCandidate(
                resource_id=str(public_ip),
                resource_arn=None,
                resource_type=ResourceType.ELASTIC_IP,
                region=region,
                action=RecommendationAction.REVIEW_RELEASE_UNUSED_EIP,
                title=f"Unused Elastic IP {public_ip}",
                finding="Elastic IP is allocated but not associated with any instance or ENI.",
                current_configuration={
                    "public_ip": public_ip,
                    "allocation_id": eip.get("allocation_id"),
                    "association_id": eip.get("association_id"),
                    "instance_id": eip.get("instance_id"),
                    "network_interface_id": eip.get("network_interface_id"),
                },
                recommended_configuration={
                    "recommendation": "REVIEW_RELEASE_UNUSED_EIP",
                },
                confidence=Confidence.HIGH,
                data_quality=DataQuality.HIGH.value,
                reason_codes=["EIP_NOT_ASSOCIATED"],
                evidence={
                    "allocation_id": eip.get("allocation_id"),
                    "public_ip": public_ip,
                    "tags": eip.get("tags") or {},
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule 3 — Low-utilization EC2
# ---------------------------------------------------------------------------


def rule_low_utilization_ec2(
    *,
    account_id: Optional[str],
    region: str,
    ec2_instances: Iterable[Dict[str, Any]],
    utilization_by_resource: Dict[str, List[MetricSeries]],
    lookback_days: int,
) -> List[DeterministicCandidate]:
    """Emit REVIEW_LOW_UTILIZATION_EC2 for instances with sustained low CPU.

    Conservative criteria:

    * Average CPU < ``EC2_CPU_AVG_MAX_THRESHOLD``.
    * Maximum CPU < ``EC2_CPU_MAX_THRESHOLD``.
    * Lookback >= ``EC2_RULE_MIN_LOOKBACK_DAYS`` (the spec is explicit).
    * Data quality is ``HIGH`` or ``MEDIUM`` (not ``LOW`` / ``NO_DATA``).
    """
    if lookback_days < EC2_RULE_MIN_LOOKBACK_DAYS:
        return []
    out: List[DeterministicCandidate] = []
    for inst in ec2_instances:
        instance_id = inst.get("instance_id")
        if not instance_id:
            continue
        series = utilization_by_resource.get(instance_id, [])
        if not series:
            continue
        worst = _worst_quality(series)
        if worst not in EC2_ACCEPTABLE_DATA_QUALITIES:
            continue
        cpu_avg = _average_cpu(series)
        cpu_max = _max_cpu(series)
        if cpu_avg is None or cpu_max is None:
            continue
        if cpu_avg >= EC2_CPU_AVG_MAX_THRESHOLD:
            continue
        if cpu_max >= EC2_CPU_MAX_THRESHOLD:
            continue
        out.append(
            DeterministicCandidate(
                resource_id=instance_id,
                resource_arn=None,
                resource_type=ResourceType.EC2,
                region=region,
                action=RecommendationAction.REVIEW_LOW_UTILIZATION_EC2,
                title=f"Low-utilization EC2 {instance_id}",
                finding=(
                    f"Average CPU {cpu_avg}% and max CPU {cpu_max}% over the last "
                    f"{lookback_days}d are below conservative thresholds."
                ),
                current_configuration={
                    "instance_id": instance_id,
                    "instance_type": inst.get("instance_type"),
                    "state": inst.get("state"),
                },
                recommended_configuration={
                    "recommendation": "REVIEW_LOW_UTILIZATION_EC2",
                    "note": "Replacement instance type intentionally not chosen; defer to Compute Optimizer / Cost Optimization Hub.",
                },
                confidence=Confidence.MEDIUM,
                data_quality=worst.value,
                reason_codes=["CPU_AVG_LOW", "CPU_MAX_LOW"],
                evidence={
                    "cpu_avg": str(cpu_avg),
                    "cpu_max": str(cpu_max),
                    "lookback_days": lookback_days,
                    "data_quality": worst.value,
                    "instance_type": inst.get("instance_type"),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule 4 — Idle NAT Gateway
# ---------------------------------------------------------------------------


def rule_idle_nat_gateway(
    *,
    account_id: Optional[str],
    region: str,
    nat_gateways: Iterable[Dict[str, Any]],
    utilization_by_resource: Dict[str, List[MetricSeries]],
) -> List[DeterministicCandidate]:
    """Emit REVIEW_IDLE_NAT_GATEWAY for NAT gateways with all-zero metrics.

    The metric set we have today for NAT gateways is sparse; we only
    flag the resource when CloudWatch returned HIGH-quality data and
    every datapoint is exactly zero.  Anything less is treated as
    "no evidence".
    """
    out: List[DeterministicCandidate] = []
    for ng in nat_gateways:
        ng_id = ng.get("nat_gateway_id")
        if not ng_id:
            continue
        # Phase 1 surfaces ``state`` but not connectivity state.  Skip
        # the rule when the gateway is still ``pending`` — the AWS
        # state machine can report zero traffic until deletion completes.
        state = (ng.get("state") or "").lower()
        if state == "pending":
            continue
        series = utilization_by_resource.get(ng_id, [])
        if not series:
            continue
        worst = _worst_quality(series)
        if worst != NAT_IDLE_REQUIRED_DATA_QUALITY:
            continue
        if not _is_all_zero(series):
            continue
        out.append(
            DeterministicCandidate(
                resource_id=ng_id,
                resource_arn=None,
                resource_type=ResourceType.NAT_GATEWAY,
                region=region,
                action=RecommendationAction.REVIEW_IDLE_NAT_GATEWAY,
                title=f"Idle NAT Gateway {ng_id}",
                finding="CloudWatch metrics indicate zero traffic on every datapoint.",
                current_configuration={
                    "nat_gateway_id": ng_id,
                    "state": ng.get("state"),
                },
                recommended_configuration={
                    "recommendation": "REVIEW_IDLE_NAT_GATEWAY",
                },
                confidence=Confidence.MEDIUM,
                data_quality=worst.value,
                reason_codes=["ALL_METRICS_ZERO"],
                evidence={
                    "nat_gateway_id": ng_id,
                    "state": ng.get("state"),
                    "metric_series_count": len(series),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule 5 — Idle ALB / NLB
# ---------------------------------------------------------------------------


def rule_idle_load_balancer(
    *,
    account_id: Optional[str],
    region: str,
    load_balancers: Iterable[Dict[str, Any]],
    utilization_by_resource: Dict[str, List[MetricSeries]],
) -> List[DeterministicCandidate]:
    """Emit REVIEW_IDLE_LOAD_BALANCER for ALB / NLB with zero activity.

    Requires HIGH-quality metrics for either ALB ``RequestCount`` /
    ``ProcessedBytes`` or NLB ``ProcessedBytes``.  Missing metrics
    are treated as ``no evidence`` and skipped.
    """
    out: List[DeterministicCandidate] = []
    for lb in load_balancers:
        arn = lb.get("arn")
        lb_type = (lb.get("type") or "application").lower()
        if lb_type not in ("application", "network"):
            continue
        if not arn:
            continue
        series = utilization_by_resource.get(arn, [])
        if not series:
            continue
        worst = _worst_quality(series)
        if worst != LB_IDLE_REQUIRED_DATA_QUALITY:
            continue
        if not _is_all_zero(series):
            continue
        out.append(
            DeterministicCandidate(
                resource_id=arn,
                resource_arn=arn,
                resource_type=ResourceType.LOAD_BALANCER,
                region=region,
                action=RecommendationAction.REVIEW_IDLE_LOAD_BALANCER,
                title=f"Idle load balancer {lb.get('name') or arn.split('/')[-1]}",
                finding=(
                    "CloudWatch RequestCount / ProcessedBytes metrics are all "
                    "exactly zero over the lookback window."
                ),
                current_configuration={
                    "arn": arn,
                    "name": lb.get("name"),
                    "type": lb_type,
                },
                recommended_configuration={
                    "recommendation": "REVIEW_IDLE_LOAD_BALANCER",
                },
                confidence=Confidence.MEDIUM,
                data_quality=worst.value,
                reason_codes=["ALL_METRICS_ZERO"],
                evidence={
                    "arn": arn,
                    "type": lb_type,
                    "metric_series_count": len(series),
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Rule 6 — RDS Underutilization
# ---------------------------------------------------------------------------


def rule_rds_underutilization(
    *,
    account_id: Optional[str],
    region: str,
    rds_instances: Iterable[Dict[str, Any]],
    utilization_by_resource: Dict[str, List[MetricSeries]],
    lookback_days: int,
) -> List[DeterministicCandidate]:
    """Emit REVIEW_LOW_UTILIZATION_RDS for underutilized DB instances.

    Conservative criteria:

    * Average CPU < ``RDS_CPU_AVG_MAX_THRESHOLD``.
    * Maximum CPU < ``RDS_CPU_MAX_THRESHOLD``.
    * Average ``DatabaseConnections`` < ``RDS_DB_CONNECTIONS_AVG_THRESHOLD``.
    * Data quality is HIGH or MEDIUM.
    """
    if lookback_days < EC2_RULE_MIN_LOOKBACK_DAYS:
        return []
    out: List[DeterministicCandidate] = []
    for db in rds_instances:
        db_id = db.get("db_instance_identifier")
        if not db_id:
            continue
        series = utilization_by_resource.get(db_id, [])
        if not series:
            continue
        worst = _worst_quality(series)
        if worst not in RDS_ACCEPTABLE_DATA_QUALITIES:
            continue
        cpu_avg = _average_cpu(series)
        cpu_max = _max_cpu(series)
        connections_avg = _avg_connections(series)
        if cpu_avg is None or cpu_max is None or connections_avg is None:
            continue
        if cpu_avg >= RDS_CPU_AVG_MAX_THRESHOLD:
            continue
        if cpu_max >= RDS_CPU_MAX_THRESHOLD:
            continue
        if connections_avg >= RDS_DB_CONNECTIONS_AVG_THRESHOLD:
            continue
        out.append(
            DeterministicCandidate(
                resource_id=db_id,
                resource_arn=None,
                resource_type=ResourceType.RDS_DB_INSTANCE,
                region=region,
                action=RecommendationAction.REVIEW_LOW_UTILIZATION_RDS,
                title=f"Low-utilization RDS instance {db_id}",
                finding=(
                    f"Average CPU {cpu_avg}%, max CPU {cpu_max}%, and avg "
                    f"DatabaseConnections {connections_avg} are below conservative thresholds."
                ),
                current_configuration={
                    "db_instance_identifier": db_id,
                    "db_instance_class": db.get("db_instance_class"),
                    "engine": db.get("engine"),
                },
                recommended_configuration={
                    "recommendation": "REVIEW_LOW_UTILIZATION_RDS",
                    "note": "Replacement DB class intentionally not chosen; defer to Compute Optimizer / Cost Optimization Hub.",
                },
                confidence=Confidence.MEDIUM,
                data_quality=worst.value,
                reason_codes=["CPU_AVG_LOW", "CPU_MAX_LOW", "DB_CONNECTIONS_LOW"],
                evidence={
                    "cpu_avg": str(cpu_avg),
                    "cpu_max": str(cpu_max),
                    "connections_avg": str(connections_avg),
                    "lookback_days": lookback_days,
                    "data_quality": worst.value,
                    "db_instance_class": db.get("db_instance_class"),
                },
            )
        )
    return out


__all__ = [
    "EC2_ACCEPTABLE_DATA_QUALITIES",
    "EC2_CPU_AVG_MAX_THRESHOLD",
    "EC2_CPU_MAX_THRESHOLD",
    "EC2_RULE_MIN_LOOKBACK_DAYS",
    "LB_IDLE_REQUIRED_DATA_QUALITY",
    "NAT_IDLE_REQUIRED_DATA_QUALITY",
    "RDS_ACCEPTABLE_DATA_QUALITIES",
    "RDS_CPU_AVG_MAX_THRESHOLD",
    "RDS_CPU_MAX_THRESHOLD",
    "RDS_DB_CONNECTIONS_AVG_THRESHOLD",
    "DeterministicCandidate",
    "deterministic_recommendation_id",
    "rule_idle_load_balancer",
    "rule_idle_nat_gateway",
    "rule_low_utilization_ec2",
    "rule_rds_underutilization",
    "rule_unattached_ebs",
    "rule_unused_eip",
]
