"""Provider-neutral domain models — Phase 7C.

These dataclasses are the canonical shape every concrete provider
adapter produces.  AWS today, Azure/Rackspace tomorrow.

Design rules:

* Every model carries enough attribution to reconstruct which
  provider / account / region / source produced the data.  This is
  the foundation for future ``All Clouds`` aggregation and for the
  AI Cost Analyst to distinguish AWS from Azure evidence.
* Provider-specific extras live under ``extensions`` (a free-form
  ``dict[str, Any]``) so adapters do not have to invent extra
  fields on the base model to surface a single provider quirk.
* Currency is a required field on every monetary model — we never
  assume USD.  Aggregating across providers is the responsibility
  of the caller; this layer refuses to silently combine different
  currencies.
* ``Decimal`` is used for every monetary amount so JSON
  serialization round-trips without binary-float corruption.  The
  conversion to ``str`` happens at the JSON boundary, never inside
  the model.
* Dataclasses are frozen by default so callers cannot mutate
  provider output by accident.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------


class CloudProviderKind(str, Enum):
    """The set of cloud providers the platform knows about.

    Today only ``AWS`` has a live adapter; ``AZURE`` and
    ``RACKSPACE`` are reserved for Phase 7C.2 and 7C.3
    respectively.  Adding a new provider is a two-step process:
    register the enum value here, then implement a
    :class:`~app.services.cloud.base.CloudProvider` subclass and
    register it with the :class:`ProviderRegistry`.
    """

    AWS = "AWS"
    AZURE = "AZURE"
    RACKSPACE = "RACKSPACE"


# ---------------------------------------------------------------------------
# Account / region
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudAccount:
    """A registered cloud account in the platform registry.

    ``account_id`` is the provider-native identifier (12-digit AWS
    account ID for AWS, GUID/hex for Azure subscription ID, numeric
    Rackspace tenant ID).  ``display_name`` is the operator-supplied
    label.  ``is_management`` marks the account the application
    itself runs in (EC2 instance profile for AWS, equivalent for
    other providers).

    ``external_ref`` is the opaque reference the provider adapter
    needs to reach the account (ARN for AWS, ARM resource ID for
    Azure, etc.).  Treat it as write-only — adapters must never
    log it in plaintext.

    ``metadata`` carries provider-specific key/value pairs (e.g.
    ``{"role_arn": "...", "external_id": "..."}`` for AWS or
    ``{"tenant_id": "...", "subscription_id": "..."}`` for Azure).
    """

    provider: CloudProviderKind
    account_id: str
    display_name: str
    is_management: bool
    enabled: bool
    external_ref: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudRegion:
    """A cloud region the adapter knows how to address.

    ``region_id`` is the provider-native code (``us-east-1``,
    ``eastus``, ``DFW``).  ``display_name`` is the human label.
    """

    provider: CloudProviderKind
    region_id: str
    display_name: str


# ---------------------------------------------------------------------------
# Cost models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudServiceCost:
    """Spend attributed to a single service within a cost report."""

    service: str
    amount: Decimal
    currency: str
    provider: CloudProviderKind
    account_id: Optional[str] = None
    region: Optional[str] = None
    extensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudCostSummary:
    """The summary view of cost for a single provider + account scope.

    ``daily`` is the time series, ``by_service`` and ``by_region``
    are the breakdowns.  ``currency`` is the currency every figure
    is denominated in; aggregating across providers with different
    currencies is the caller's responsibility.
    """

    provider: CloudProviderKind
    account_id: Optional[str]  # None when aggregating across accounts.
    scope: str  # "acct:<id>" or "all"
    period_start: date
    period_end: date
    currency: str
    current_total: Decimal
    previous_total: Decimal
    change_amount: Decimal
    change_percent: Optional[Decimal]
    estimated: bool
    by_service: List[CloudServiceCost]
    by_region: List[CloudServiceCost]
    extensions: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudCostTrend:
    """A single point on the cost time series."""

    provider: CloudProviderKind
    account_id: Optional[str]
    period_start: date
    period_end: date
    currency: str
    daily: List[CloudServiceCost]  # one entry per day
    source: str


# ---------------------------------------------------------------------------
# Resource models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudResource:
    """A single cloud resource as reported by a provider.

    ``resource_type`` is a provider-native or normalized slug
    (``ec2``, ``rds``, ``lambda`` for AWS; equivalent slugs for
    other providers).  ``resource_id`` is the provider-native
    primary key (ARN when available, otherwise the local id).
    ``composite_id`` is a globally-unique identifier built from
    ``provider|account|region|resource_type|resource_id`` so the
    same i-0123 in two different accounts never collides.

    ``metadata`` is a free-form dict that adapters use to surface
    fields specific to their provider (instance_type, engine,
    size_gb, ...).  Common fields like ``state`` and ``region`` are
    promoted onto the model.
    """

    provider: CloudProviderKind
    account_id: str
    region: Optional[str]
    resource_type: str
    resource_id: str
    service: str
    name: Optional[str]
    state: Optional[str]
    composite_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudResourceSummary:
    """A grouped resource inventory suitable for the UI."""

    provider: CloudProviderKind
    account_id: Optional[str]
    scope: str
    region: Optional[str]
    services: Dict[str, Dict[str, Any]]  # service -> {items, status, error_code?}
    totals_by_account: List[Dict[str, Any]]
    extensions: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloudRecommendation:
    """A provider-emitted cost optimization recommendation.

    ``source`` is the provider-native origin (AWS_COST_OPTIMIZATION_HUB,
    AWS_COMPUTE_OPTIMIZER, AZURE_ADVISOR, ...) so the UI can badge
    it correctly.  ``estimated_savings`` is the provider-reported
    estimate; ``None`` means the provider did not return one (we
    NEVER fabricate one).

    Phase 7D.1 promotes three previously extension-only fields onto
    the shared contract so both providers expose the same generic
    columns without provider-specific branching:

    * ``category`` — a coarse grouping (AWS action slug such as
      ``RIGHTSIZE``; Azure Advisor category such as ``Cost``).
    * ``impact`` — the provider-native severity/confidence bucket
      (AWS ``HIGH``/``MEDIUM``/``LOW``; Azure ``High``/``Medium``/
      ``Low``).
    * ``description`` — the long-form explanation
      (AWS ``finding``; Azure Advisor ``problem``/description).

    These are additive and default to ``None`` so existing AWS and
    Azure call sites that do not populate them keep working.
    Provider metadata that cannot be normalised cleanly still lives
    under ``extensions``.
    """

    provider: CloudProviderKind
    account_id: Optional[str]
    region: Optional[str]
    recommendation_id: str
    resource_id: str
    resource_type: str
    service: str
    action: str
    title: str
    estimated_savings: Optional[Decimal]
    currency: str
    source: str
    category: Optional[str] = None
    impact: Optional[str] = None
    description: Optional[str] = None
    extensions: Mapping[str, Any] = field(default_factory=dict)


__all__ = [
    "CloudAccount",
    "CloudCostSummary",
    "CloudCostTrend",
    "CloudProviderKind",
    "CloudRecommendation",
    "CloudRegion",
    "CloudResource",
    "CloudResourceSummary",
    "CloudServiceCost",
]
