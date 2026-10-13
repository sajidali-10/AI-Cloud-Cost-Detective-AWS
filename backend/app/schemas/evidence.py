"""Pydantic schemas for the evidence route — Phase 2.

The evidence schema is the wire contract between Phase 2 and the
Phase 3 optimizer.  It is intentionally deterministic:

* ``cost_context.scope`` is one of ``ACCOUNT``, ``SERVICE``,
  ``REGION`` — NEVER ``RESOURCE`` with a dollar figure.  Cost
  Explorer does not give us per-resource spend, so we never
  fabricate one.
* ``data_quality`` mirrors the CloudWatch classifier
  (``high | medium | low | no_data``).
* ``sources`` is the explicit list of AWS APIs that contributed to
  the item so Phase 3 can attribute every number.

Phase 7C.1 adds ``provider`` and ``account_id`` attribution fields
so the AI Cost Analyst can distinguish AWS evidence from future
Azure / Rackspace evidence.  The fields are optional and default
to AWS so existing callers continue to work unchanged.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.services.aws.cloudwatch_metrics import DataQuality


class CostScope(str, Enum):
    """The scope of a cost figure attached to evidence.

    ``RESOURCE`` is intentionally NOT in the enum — the Phase 2
    spec forbids per-resource dollar figures (Cost Explorer only
    gives us account / service / region aggregates).
    """

    ACCOUNT = "ACCOUNT"
    SERVICE = "SERVICE"
    REGION = "REGION"


class EvidenceStatus(str, Enum):
    """The overall status of an evidence response."""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class CostContext(BaseModel):
    """Where the cost number came from.

    ``amount`` is the spend for ``scope`` over the requested period.
    ``scope=SERVICE`` carries the canonical AWS service name in
    ``service``.  ``scope=REGION`` carries the AWS region in
    ``region``.  ``scope=ACCOUNT`` sets neither.
    """

    model_config = ConfigDict(frozen=True)

    scope: CostScope
    service: Optional[str] = None
    region: Optional[str] = None
    amount: Decimal
    unit: str = "USD"


class ResourceEvidence(BaseModel):
    """One evidence record per discovered resource.

    Phase 7C.1: ``provider`` and ``account_id`` are optional
    attribution fields.  When omitted they default to ``AWS`` and
    ``None`` respectively so existing Phase 2 callers continue to
    produce schemas that pass validation.  Future Azure / Rackspace
    adapters will always set ``provider`` explicitly so the AI can
    distinguish AWS evidence from Azure evidence.
    """

    model_config = ConfigDict(frozen=True)

    resource_id: str
    resource_type: str  # "ec2" | "rds" | "lambda" | "alb" | "nlb" | ...
    service: str
    region: str
    provider: str = Field(
        default="AWS",
        description=(
            "Provider that produced this evidence record.  "
            "Phase 7C.1 always emits 'AWS'; future adapters will "
            "set 'AZURE' or 'RACKSPACE'."
        ),
    )
    account_id: Optional[str] = Field(
        default=None,
        description=(
            "Provider-native account identifier (12-digit AWS id "
            "today, subscription GUID for Azure, tenant id for "
            "Rackspace).  None for the aggregate 'All' view."
        ),
    )
    utilization: dict = Field(default_factory=dict)
    cost_context: CostContext
    data_quality: DataQuality = DataQuality.NO_DATA
    sources: List[str] = Field(default_factory=list)


class Warning(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str
    service: str
    resource_id: Optional[str] = None
    code: str
    message: str


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    region: str
    days: int
    status: EvidenceStatus
    cost_summary: dict
    # Phase 7C.1: provider attribution at the top level so the AI
    # can disambiguate AWS / Azure / Rackspace evidence without
    # scanning every resource record.  Defaults to AWS.
    provider: str = Field(default="AWS")
    # Optional account/subscription attribution at the top level.
    account_id: Optional[str] = Field(default=None)
    resources: List[ResourceEvidence]
    warnings: List[Warning] = Field(default_factory=list)


__all__ = [
    "CostContext",
    "CostScope",
    "EvidenceResponse",
    "EvidenceStatus",
    "ResourceEvidence",
    "Warning",
]
