"""Pydantic schemas for the provider-neutral cloud recommendations API — Phase 7C.2D.

These shapes mirror the dataclasses in
:mod:`app.services.cloud.models` (``CloudRecommendation`` /
``CloudRecommendationSummary``) so the canonical
``/api/cloud/recommendations`` route can serialise either AWS
or Azure adapter output into a single provider-neutral envelope.

Provider-neutral contract
-------------------------

* ``provider`` — ``"AWS"`` or ``"AZURE"`` (Rackspace reserved).
* ``scope`` — ``"all"``, ``"acct:<id>"`` (AWS), or ``"sub:<id>"``
  (Azure).
* ``status`` — ``SUCCESS`` / ``PARTIAL_SUCCESS`` / ``NO_DATA`` /
  ``ERROR``.
* ``savings_status`` — ``OK`` / ``MIXED_CURRENCY`` / ``EMPTY`` —
  distinguishes "every recommendation reports one currency we
  summed" from "currencies disagreed — we did not invent an FX
  rate" from "no recommendation carried a savings value".
* ``source`` — provider-native identifier
  (``AWS_COST_OPTIMIZATION_HUB``, ``AZURE_ADVISOR``, ...).

Savings field rules
-------------------

* ``potential_annual_savings`` is a single ``Decimal`` only when
  ``savings_status == "OK"``.
* ``savings_by_currency`` is non-empty only when
  ``savings_status == "OK"`` AND matches ``potential_annual_savings``.
* When ``savings_status == "MIXED_CURRENCY"``,
  ``potential_annual_savings`` is ``None`` and the per-currency
  totals live in ``savings_by_currency``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field


class CloudRecommendationItem(BaseModel):
    """A single normalised cloud recommendation.

    Mirrors :class:`app.services.cloud.models.CloudRecommendation`
    so the front-end can consume either provider without
    provider-specific branching.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    account_id: Optional[str] = None
    region: Optional[str] = None
    recommendation_id: str
    resource_id: str
    resource_type: str
    service: str
    action: str
    title: str
    # Phase 7D.1 — generic fields promoted from extensions so both
    # providers expose one shape (AWS + Azure).  Optional + defaulted
    # so older payloads stay valid.
    category: Optional[str] = None
    impact: Optional[str] = None
    description: Optional[str] = None
    estimated_savings: Optional[Decimal] = None
    currency: Optional[str] = None
    source: str
    extensions: Mapping[str, Any] = Field(default_factory=dict)


class CloudRecommendationAccountError(BaseModel):
    """A sanitised per-account / per-subscription failure.

    Provider-neutral rename of the Azure-only
    ``CloudRecommendationSubscriptionError``.  ``account_id`` carries
    the AWS account id or the Azure subscription id depending on the
    provider.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    display_name: str
    code: str
    message: str


class CloudRecommendationGroupCount(BaseModel):
    """A single bucket of a recommendation-group breakdown
    (by impact / resource_type / subscription / etc.)."""

    model_config = ConfigDict(frozen=True)

    key: str
    count: int


class CloudRecommendationCurrencyTotal(BaseModel):
    """One bucket of ``savings_by_currency``."""

    model_config = ConfigDict(frozen=True)

    currency: str
    amount: Decimal


class CloudRecommendationSubscriptionError(BaseModel):
    """A sanitised per-subscription failure surfaced in the aggregate."""

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    display_name: str
    code: str
    message: str


class CloudRecommendationSummaryEnvelope(BaseModel):
    """The provider-neutral recommendation-summary envelope."""

    model_config = ConfigDict(frozen=True)

    provider: str
    scope: str
    scope_kind: str  # "all" | "single"
    status: str  # SUCCESS | PARTIAL_SUCCESS | NO_DATA | ERROR
    savings_status: str  # OK | MIXED_CURRENCY | EMPTY
    currency: Optional[str] = None
    total_recommendations: int
    recommendations_with_savings: int
    recommendations_without_savings: int
    # ``potential_annual_savings`` is the Azure Advisor figure (kept
    # for backward compatibility).  ``potential_savings`` + the
    # generic ``savings_period`` are the provider-neutral pair: AWS
    # populates them with a MONTHLY figure, Azure likewise alongside
    # the legacy annual field.  Never both meaningfully populated for
    # AWS (its annual field stays None).
    potential_annual_savings: Optional[Decimal] = None
    potential_savings: Optional[Decimal] = None
    savings_period: Optional[str] = None  # MONTHLY | ANNUAL | None
    savings_by_currency: List[CloudRecommendationCurrencyTotal] = Field(default_factory=list)
    by_impact: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_resource_type: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_subscription: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_resource_group: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_type: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_subcategory: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    subscription_errors: List[CloudRecommendationSubscriptionError] = Field(default_factory=list)
    # Phase 7D.1 — provider-neutral attribution (AWS accounts / Azure
    # subscriptions) and a provider-neutral failure list.
    by_account: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_source: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    by_action: List[CloudRecommendationGroupCount] = Field(default_factory=list)
    account_errors: List[CloudRecommendationAccountError] = Field(default_factory=list)
    source: str = "CLOUD_RECOMMENDATIONS"


class CloudRecommendationListResponse(BaseModel):
    """The wire envelope for ``/api/cloud/recommendations``."""

    model_config = ConfigDict(frozen=True)

    summary: CloudRecommendationSummaryEnvelope
    recommendations: List[CloudRecommendationItem] = Field(default_factory=list)


__all__ = [
    "CloudRecommendationAccountError",
    "CloudRecommendationCurrencyTotal",
    "CloudRecommendationGroupCount",
    "CloudRecommendationItem",
    "CloudRecommendationListResponse",
    "CloudRecommendationSubscriptionError",
    "CloudRecommendationSummaryEnvelope",
]
