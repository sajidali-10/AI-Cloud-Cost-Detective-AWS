"""Pydantic schemas for the utilization route — Phase 2."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.services.aws.cloudwatch_metrics import DataQuality


class UtilizationRequest(BaseModel):
    """Request body for ``POST /api/aws/utilization``.

    The request accepts region/lookback_days/optional resource_types
    only — NEVER an arbitrary AWS operation name.  The backend
    decides which metrics are valid (per the Phase 2 spec).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    region: str = Field(min_length=1)
    lookback_days: int = Field(default=30)
    resource_types: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional subset of resource types to fetch metrics for. "
            "Allowed values: ec2, rds, lambda, alb, nlb. When omitted, "
            "all supported types are scanned."
        ),
    )


class Datapoint(BaseModel):
    """One CloudWatch datapoint: timestamp + value."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    value: Decimal


class MetricSeries(BaseModel):
    """One CloudWatch metric series for a single resource."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    metric_name: str
    statistic: str
    unit: str = ""
    datapoints: List[Datapoint]
    data_quality: DataQuality = DataQuality.NO_DATA


class Warning(BaseModel):
    """Structured warning — surfaced per resource or per service.

    The route layer attaches these to the response so callers can
    show 'this resource returned AccessDenied, others are fine'
    instead of seeing a 500 that destroys valid data from other
    services.
    """

    model_config = ConfigDict(frozen=True)

    source: str  # "cloudwatch" | "cost_explorer" | "resources"
    service: str
    resource_id: Optional[str] = None
    code: str  # e.g. "ACCESS_DENIED"
    message: str


class ResourceUtilization(BaseModel):
    """Utilization result for a single resource across all its metrics."""

    model_config = ConfigDict(frozen=True)

    resource_id: str
    resource_type: str
    service: str  # EC2 | RDS | Lambda | ELBv2 (we group ALB+NLB as ELBv2 for display)
    region: str
    lookback_days: int
    metrics: List[MetricSeries]
    data_quality: DataQuality = DataQuality.NO_DATA
    warnings: List[Warning] = Field(default_factory=list)


class UtilizationResponse(BaseModel):
    """Wire envelope for the utilization route."""

    model_config = ConfigDict(frozen=True)

    region: str
    lookback_days: int
    resources: List[ResourceUtilization]
    warnings: List[Warning] = Field(default_factory=list)
