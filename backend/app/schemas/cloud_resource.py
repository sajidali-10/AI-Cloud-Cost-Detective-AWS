"""Pydantic schemas for the provider-neutral cloud resource API — Phase 7C.2C.

These shapes mirror the dataclasses in
:mod:`app.services.cloud.models` (``CloudResource`` /
``CloudResourceSummary``) so the canonical ``/api/cloud/resources``
route can serialise either AWS or Azure adapter output into a single
provider-neutral envelope.

Provider-neutral contract
-------------------------

* ``provider`` — ``"AWS"`` or ``"AZURE"`` (Rackspace reserved for a
  future phase).
* ``scope`` — ``"all"``, ``"acct:<id>"`` (AWS), or ``"sub:<id>"``
  (Azure).
* ``status`` — ``SUCCESS`` / ``PARTIAL_SUCCESS`` / ``NO_DATA`` /
  ``ERROR`` — the same vocabulary Phase 7C.2B introduced for the
  cost surface.
* ``resource_type`` carries the canonical provider identifier
  (``EC2`` for AWS, ``Microsoft.Compute/virtualMachines`` for
  Azure).  The shared UI does NOT translate this; the AWS / Azure
  response shapes carry the original identifier so a single set of
  components can render both.
* ``account_id`` / ``account_display_name`` carry the same
  attribution as the cost envelope — for AWS this is a 12-digit
  account id, for Azure it is the subscription GUID.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field


class CloudResourceItem(BaseModel):
    """A single normalised cloud resource.

    Mirrors :class:`app.services.cloud.models.CloudResource` so the
    front-end can consume either provider without provider-specific
    branching.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    account_id: str
    region: Optional[str] = None
    resource_type: str
    resource_id: str
    service: str
    name: Optional[str] = None
    state: Optional[str] = None
    composite_id: str
    metadata: Mapping[str, Any] = Field(default_factory=dict)


class CloudResourceGroupCount(BaseModel):
    """A single bucket of a resource-group breakdown (by type /
    location / resource-group / subscription)."""

    model_config = ConfigDict(frozen=True)

    key: str
    count: int


class CloudResourceSubscriptionShare(BaseModel):
    """Per-subscription attribution for the resource rollup.

    The Azure adapter carries the same shape for subscription
    attribution; the AWS adapter may omit it when the underlying
    fan-out is per-account rather than per-subscription.
    """

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    display_name: str
    count: int


class CloudResourceAccountTotal(BaseModel):
    """Per-account / per-subscription resource count."""

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    display_name: str
    count: int


class CloudResourceSubscriptionError(BaseModel):
    """A sanitised per-subscription failure surfaced in the aggregate."""

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    display_name: str
    code: str
    message: str


class CloudResourceSummaryEnvelope(BaseModel):
    """The provider-neutral resource-summary envelope.

    Distinct from :class:`app.services.cloud.models.CloudResourceSummary`
    so the canonical route can serialise counts / breakdowns in a
    shape the front-end already understands, without widening the
    internal dataclass model.  The route adapter translates the
    adapter-specific ``extensions`` dict into the typed fields below.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    scope: str
    scope_kind: str  # "all" | "single"
    status: str
    total_resources: int
    by_type: List[CloudResourceGroupCount] = Field(default_factory=list)
    by_location: List[CloudResourceGroupCount] = Field(default_factory=list)
    by_resource_group: List[CloudResourceGroupCount] = Field(default_factory=list)
    by_subscription: List[CloudResourceSubscriptionShare] = Field(default_factory=list)
    subscription_errors: List[CloudResourceSubscriptionError] = Field(default_factory=list)
    source: str = "CLOUD_RESOURCE_GRAPH"


class CloudResourceListResponse(BaseModel):
    """The wire envelope for ``/api/cloud/resources``."""

    model_config = ConfigDict(frozen=True)

    summary: CloudResourceSummaryEnvelope
    resources: List[CloudResourceItem] = Field(default_factory=list)


__all__ = [
    "CloudResourceAccountTotal",
    "CloudResourceGroupCount",
    "CloudResourceItem",
    "CloudResourceListResponse",
    "CloudResourceSubscriptionError",
    "CloudResourceSubscriptionShare",
    "CloudResourceSummaryEnvelope",
]
