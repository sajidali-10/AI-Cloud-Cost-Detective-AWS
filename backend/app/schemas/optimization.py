"""Pydantic schemas for the optimization engine — Phase 3.

This module defines the normalized wire contract between the
optimization services and the FastAPI layer.  Every recommendation
that leaves the backend has the shape defined here — raw AWS
``GetEC2InstanceRecommendations`` payloads are intentionally NOT
leaked through the API.

Invariants (enforced in code AND in the Phase 3 verifier):

* **No AI savings.** ``SavingsSource`` carries only the four allowed
  members; ``AI_ESTIMATE`` does not exist.
* **No fabricated savings.** ``estimated_monthly_savings`` and
  ``savings_percentage`` are ``Optional[...]``.  AWS-native sources
  fill them in when present; deterministic rules leave them null and
  set ``savings_source = UNKNOWN`` so the frontend can render an
  explicit "no estimate" badge.
* **Per-recommendation dedup is preserved by ``sources``.**
  ``primary_source`` is the authoritative savings source;
  ``sources`` lists every source that contributed, in stable order.
* **Capabilities expose the enrollment state of every AWS-native
  source.**  The optimization endpoint must remain functional even
  when Compute Optimizer and Cost Optimization Hub are both inactive.

Note on naming: we use ``OptimizationNotice`` rather than ``Warning``
because Pydantic 2.12 confuses the local class with the builtin
:class:`warnings.Warning` during schema generation.  The wire shape
(list of ``{source, code, message, region}``) is unchanged.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CapabilityStatus(str, Enum):
    """The enrollment / availability state of an AWS-native source.

    The optimization endpoint is fully usable in every state — only
    the recommendation payload changes.  ``NOT_ENROLLED`` is mapped
    to ``INACTIVE`` on the wire but kept distinct internally so tests
    can assert the difference between "AWS rejected the call" and
    "the account has never enrolled".
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING = "PENDING"
    FAILED = "FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_ENROLLED = "NOT_ENROLLED"
    AVAILABLE = "AVAILABLE"


class ResourceType(str, Enum):
    """The resource type a recommendation targets.

    Slugs match the AWS Cost Optimization Hub ``resourceType`` enum
    where possible (uppercased to keep our wire format consistent).
    """

    EC2 = "EC2"
    EBS_VOLUME = "EBS_VOLUME"
    LAMBDA_FUNCTION = "LAMBDA_FUNCTION"
    RDS_DB_INSTANCE = "RDS_DB_INSTANCE"
    ELASTIC_IP = "ELASTIC_IP"
    NAT_GATEWAY = "NAT_GATEWAY"
    LOAD_BALANCER = "LOAD_BALANCER"


class RecommendationAction(str, Enum):
    """The optimization action a recommendation proposes.

    Values are deliberately grouped:

    * ``RIGHTSIZE`` / ``DELETE_UNUSED`` / ``RELEASE_UNUSED`` come
      from AWS-native sources (Compute Optimizer, Cost Optimization Hub).
    * ``REVIEW_*`` values come from the deterministic rule engine.
      They are advisory only — the engine never proposes an instance
      type unless AWS supplied one.
    """

    RIGHTSIZE = "RIGHTSIZE"
    DELETE_UNUSED = "DELETE_UNUSED"
    RELEASE_UNUSED = "RELEASE_UNUSED"
    STOP_IDLE = "STOP_IDLE"
    REVIEW_DELETE_UNATTACHED_EBS = "REVIEW_DELETE_UNATTACHED_EBS"
    REVIEW_RELEASE_UNUSED_EIP = "REVIEW_RELEASE_UNUSED_EIP"
    REVIEW_LOW_UTILIZATION_EC2 = "REVIEW_LOW_UTILIZATION_EC2"
    REVIEW_IDLE_NAT_GATEWAY = "REVIEW_IDLE_NAT_GATEWAY"
    REVIEW_IDLE_LOAD_BALANCER = "REVIEW_IDLE_LOAD_BALANCER"
    REVIEW_LOW_UTILIZATION_RDS = "REVIEW_LOW_UTILIZATION_RDS"


class SavingsSource(str, Enum):
    """Where the savings figure on a recommendation came from.

    ``CALCULATED`` is reserved for future deterministic cost
    calculations; Phase 3 emits it only when AWS itself supplies the
    figure via Cost Optimization Hub or Compute Optimizer
    (which it does as ``CALCULATED`` from the AWS console's POV).
    ``UNKNOWN`` is the default for deterministic rules that have no
    authoritative number.
    """

    AWS_COST_OPTIMIZATION_HUB = "AWS_COST_OPTIMIZATION_HUB"
    AWS_COMPUTE_OPTIMIZER = "AWS_COMPUTE_OPTIMIZER"
    CALCULATED = "CALCULATED"
    UNKNOWN = "UNKNOWN"


class Confidence(str, Enum):
    """The confidence / data-quality bucket for a recommendation.

    Heuristic:

    * ``HIGH`` — AWS-native source supplies a savings figure and a
      concrete resource-level recommendation (CO finding != Optimized,
      or COH actionType set).
    * ``MEDIUM`` — deterministic rule with strong CloudWatch coverage.
    * ``LOW`` — partial evidence or a deterministic rule on a
      resource with insufficient datapoints.
    """

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class OptimizationStatus(str, Enum):
    """The overall status of an optimization response.

    * ``SUCCESS`` — every source answered cleanly.
    * ``PARTIAL_SUCCESS`` — at least one source contributed valid
      recommendations and at least one source failed.
    * ``FAILED`` — every source failed (deterministic rules always
      run, so ``FAILED`` only fires when the deterministic engine
      itself errors AND no AWS-native source contributed).
    """

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Action slug map (used for deterministic ID generation)
# ---------------------------------------------------------------------------


_ACTION_SLUGS: Dict[RecommendationAction, str] = {
    RecommendationAction.RIGHTSIZE: "rightsize",
    RecommendationAction.DELETE_UNUSED: "delete-unused",
    RecommendationAction.RELEASE_UNUSED: "release-unused",
    RecommendationAction.STOP_IDLE: "stop-idle",
    RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS: "review-delete-unattached-ebs",
    RecommendationAction.REVIEW_RELEASE_UNUSED_EIP: "review-release-unused-eip",
    RecommendationAction.REVIEW_LOW_UTILIZATION_EC2: "review-low-utilization-ec2",
    RecommendationAction.REVIEW_IDLE_NAT_GATEWAY: "review-idle-nat-gateway",
    RecommendationAction.REVIEW_IDLE_LOAD_BALANCER: "review-idle-load-balancer",
    RecommendationAction.REVIEW_LOW_UTILIZATION_RDS: "review-low-utilization-rds",
}


def action_slug(action: RecommendationAction) -> str:
    """Return a URL-safe slug for a recommendation action."""
    return _ACTION_SLUGS.get(action, action.value.lower())


# ---------------------------------------------------------------------------
# Warning — declared early because CapabilitiesResponse references it.
# ---------------------------------------------------------------------------


class OptimizationNotice(BaseModel):
    """Structured warning emitted by the optimization engine.

    See the module docstring for why this class is named
    ``OptimizationNotice`` rather than ``Warning``.
    """

    model_config = ConfigDict(frozen=True)

    source: str  # "compute_optimizer" | "cost_optimization_hub" | "deterministic_rules" | "identity"
    code: str
    message: str
    region: Optional[str] = None


# ---------------------------------------------------------------------------
# Recommendation model
# ---------------------------------------------------------------------------


class RecommendationEvidence(BaseModel):
    """Per-source evidence block embedded in a recommendation.

    ``data`` is a free-form JSON object whose shape depends on the
    source.  Keys we currently emit:

    * For AWS Compute Optimizer: ``finding``, ``lookback_period_days``,
      ``current_instance_type``, ``performance_risk``,
      ``recommended_options``.
    * For Cost Optimization Hub: ``current_resource_summary``,
      ``recommended_resource_summary``, ``implementation_effort``,
      ``restart_needed``, ``rollback_possible``.
    * For deterministic rules: the rule-specific evidence (volume
      size, attachment count, CPU averages, etc.).
    """

    model_config = ConfigDict(frozen=True)

    source: SavingsSource
    confidence: Confidence
    data: Dict[str, Any] = Field(default_factory=dict)
    reason_codes: List[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    """A normalized optimization recommendation.

    Fields unavailable from AWS are ``None`` (never invented).
    Deterministic rules always set ``estimated_monthly_savings=None``
    and ``savings_source=UNKNOWN`` unless AWS supplies a number.
    """

    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    resource_id: str
    resource_arn: Optional[str] = None
    resource_type: ResourceType
    region: str
    account_id: Optional[str] = None
    action: RecommendationAction
    title: str
    finding: str
    current_configuration: Dict[str, Any] = Field(default_factory=dict)
    recommended_configuration: Dict[str, Any] = Field(default_factory=dict)
    estimated_monthly_savings: Optional[Decimal] = None
    currency: str = "USD"
    savings_percentage: Optional[Decimal] = None
    savings_source: SavingsSource
    primary_source: SavingsSource
    sources: List[SavingsSource]
    confidence: Confidence
    data_quality: str = "unknown"
    reason_codes: List[str] = Field(default_factory=list)
    restart_needed: Optional[bool] = None
    rollback_possible: Optional[bool] = None
    evidence: List[RecommendationEvidence] = Field(default_factory=list)
    aws_recommendation_ids: List[str] = Field(default_factory=list)
    detected_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Capabilities endpoint
# ---------------------------------------------------------------------------


class ServiceCapability(BaseModel):
    """Enrollment / availability state for a single source."""

    model_config = ConfigDict(frozen=True)

    status: CapabilityStatus
    detail: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    error_code: Optional[str] = None


class CapabilitiesResponse(BaseModel):
    """The ``GET /api/aws/optimization/capabilities`` payload."""

    model_config = ConfigDict(frozen=True)

    region: str
    account_id: Optional[str] = None
    compute_optimizer: ServiceCapability
    cost_optimization_hub: ServiceCapability
    deterministic_engine: ServiceCapability
    supported_resource_types: List[ResourceType]
    supported_lookback_days: List[int]
    warnings: List[OptimizationNotice] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Recommendations endpoint
# ---------------------------------------------------------------------------


class RecommendationsResponse(BaseModel):
    """The ``GET /api/aws/optimization/recommendations`` payload."""

    model_config = ConfigDict(frozen=True)

    region: str
    account_id: Optional[str] = None
    days: int
    status: OptimizationStatus
    count: int
    recommendations: List[Recommendation]
    warnings: List[OptimizationNotice] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------


class SummaryByCategory(BaseModel):
    """Count + savings breakdown by one categorical axis."""

    model_config = ConfigDict(frozen=True)

    key: str
    count: int
    estimated_monthly_savings: Optional[Decimal] = None
    currency: str = "USD"


class SummaryResponse(BaseModel):
    """The ``GET /api/aws/optimization/summary`` payload.

    All savings aggregates are computed AFTER dedup so two AWS-native
    sources that point at the same (resource, action) contribute
    exactly once.  Recommendations without a savings figure are
    reported in ``recommendations_without_savings`` and excluded
    from the aggregate — we never silently inflate the total.
    """

    model_config = ConfigDict(frozen=True)

    region: str
    account_id: Optional[str] = None
    days: int
    status: OptimizationStatus
    total_recommendations: int
    total_estimated_monthly_savings: Optional[Decimal] = None
    currency: str = "USD"
    by_resource_type: List[SummaryByCategory] = Field(default_factory=list)
    by_action: List[SummaryByCategory] = Field(default_factory=list)
    by_source: List[SummaryByCategory] = Field(default_factory=list)
    by_confidence: List[SummaryByCategory] = Field(default_factory=list)
    recommendations_without_savings: int = 0
    warnings: List[OptimizationNotice] = Field(default_factory=list)


# Backward-compatible alias removed — see the module docstring.  Use
# ``OptimizationNotice`` directly.

__all__ = [
    "CapabilitiesResponse",
    "CapabilityStatus",
    "Confidence",
    "OptimizationStatus",
    "OptimizationNotice",
    "Recommendation",
    "RecommendationAction",
    "RecommendationEvidence",
    "RecommendationsResponse",
    "ResourceType",
    "SavingsSource",
    "ServiceCapability",
    "SummaryByCategory",
    "SummaryResponse",
    "action_slug",
]
