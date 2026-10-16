"""Cloud provider abstraction — Phase 7C.

This package hosts the provider-neutral interfaces and dataclasses
that let the application talk to AWS today and (in later phases)
Azure and Rackspace through a single, uniform contract.

Phase 7C.1 creates the abstraction without implementing any new
provider.  AWS is wired in through :mod:`app.services.cloud.aws`
as the first concrete adapter; the registry knows how to look up
``CloudProviderKind.AWS`` and dispatch calls to the existing
Phase 7B code paths unchanged.

Layering:

    FastAPI routes
        |
    CloudProvider ABC  <-- the only contract the routes know about
        |
    AWSCloudProvider   <-- wraps Phase 7B code (no behaviour change)
        |
    AWS service modules (cost_explorer, resource_aggregator, ...)

Two principles drive the design:

1. **Do not duplicate AWS service logic.**  The AWS adapter is a
   thin wrapper that re-uses the existing
   :mod:`app.services.aws.*` modules.  No data shapes are
   re-derived; the adapter maps existing dataclasses into the
   normalized models below and back.

2. **Provider-neutral where it matters, provider-specific where it
   doesn't.**  Cost / resource / recommendation shapes carry
   provider + account + region + source on every record so a
   future ``All Clouds`` aggregate can attribute every figure.
   Provider-specific metadata lives under a free-form
   ``extensions`` dict so adapters do not have to invent
   provider-specific fields on the base model.
"""
from __future__ import annotations

from app.services.cloud.base import (
    AccountValidationResult,
    CloudCapability,
    CloudCapabilityState,
    CloudCredentialStrategy,
    CloudProvider,
    CloudProviderStatus,
)
from app.services.cloud.models import (
    CloudAccount,
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudRecommendation,
    CloudRegion,
    CloudResource,
    CloudResourceSummary,
    CloudServiceCost,
)
from app.services.cloud.registry import (
    ProviderAlreadyRegistered,
    ProviderNotFound,
    ProviderRegistry,
)

__all__ = [
    "AccountValidationResult",
    "CloudAccount",
    "CloudCapability",
    "CloudCapabilityState",
    "CloudCostSummary",
    "CloudCostTrend",
    "CloudCredentialStrategy",
    "CloudProvider",
    "CloudProviderKind",
    "CloudProviderStatus",
    "CloudRecommendation",
    "CloudRegion",
    "CloudResource",
    "CloudResourceSummary",
    "CloudServiceCost",
    "ProviderAlreadyRegistered",
    "ProviderNotFound",
    "ProviderRegistry",
]
