"""Cloud provider base contract — Phase 7C.

This module defines the abstract base class every concrete provider
adapter must subclass.  It also defines the capability model and the
credential-strategy types — neither of which is implemented yet for
non-AWS providers, but both of which are part of the type system
Phase 7C.1 commits to.

Threading & async conventions
-----------------------------

Phase 7B's existing aggregators are async (``aggregate_cost_report_for_context``,
``aggregate_inventory_for_context``, ``aggregate_recommendations_for_context``);
identity / cost explorer / resource enumerators are sync and called via
``asyncio.to_thread``.  The interface mirrors this so adapters can pass
sync work into ``to_thread`` and call async fan-outs directly.

Sync methods
~~~~~~~~~~~~

* :meth:`CloudProvider.provider_name`
* :meth:`CloudProvider.validate_account`
* :meth:`CloudProvider.list_regions`
* :meth:`CloudProvider.get_capabilities`
* :meth:`CloudProvider.get_status`
* :meth:`CloudProvider.get_provider_account`

Async methods
~~~~~~~~~~~~~

* :meth:`CloudProvider.get_cost_summary`
* :meth:`CloudProvider.get_cost_trend`
* :meth:`CloudProvider.get_cost_by_service`
* :meth:`CloudProvider.get_resources`
* :meth:`CloudProvider.get_resource_summary`
* :meth:`CloudProvider.get_recommendations`

The ``account_id`` argument on every method is the **registry row id**
(a numeric PK), not the provider-native account id.  Routes are
expected to resolve the selector (``"all"``, ``"acct:<id>"``, or a
bare numeric id) to a registry row before calling the adapter; the
adapter then translates the row to provider-native credentials.
This keeps the contract uniform across providers.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence

from app.db.models import CloudAccount as _CloudAccountRow

from app.services.cloud.models import (
    CloudAccount,
    CloudProviderKind,
    CloudRegion,
)


# ---------------------------------------------------------------------------
# Capability model
# ---------------------------------------------------------------------------


class CloudCapability(str, Enum):
    """The set of capabilities a provider may expose.

    Not every provider implements every capability; the capability
    table is the source of truth for what the UI/API can safely
    call.  Adding a capability here is a deliberate API decision:
    every adapter must declare its support.

    Phase 7C.2A adds ``AUTHENTICATION`` and ``SUBSCRIPTIONS`` so
    the Azure adapter can report connectivity and tenant
    enumeration without having to wait for Cost Management /
    Resource Graph / Advisor to ship.
    """

    COSTS = "COSTS"
    RESOURCES = "RESOURCES"
    OPTIMIZATION = "OPTIMIZATION"
    METRICS = "METRICS"
    BILLING = "BILLING"
    ADVISOR = "ADVISOR"
    AI_EVIDENCE = "AI_EVIDENCE"
    # Phase 7C.2A — Azure bootstrap capabilities.
    AUTHENTICATION = "AUTHENTICATION"
    SUBSCRIPTIONS = "SUBSCRIPTIONS"


class CloudCapabilityState(str, Enum):
    """The runtime state of a single capability.

    Distinct from "does this provider support the capability" —
    a capability can be SUPPORTED at compile time but
    PERMISSION_DENIED at runtime, or NOT_CONFIGURED because the
    operator has not supplied credentials.

    The UI must distinguish these four "negative" states because
    the remediation path differs:

    * ``UNSUPPORTED``        — provider does not implement; show "n/a".
    * ``UNAVAILABLE``        — provider does but the API is unreachable.
    * ``PERMISSION_DENIED``  — credentials missing the required IAM perm.
    * ``NOT_CONFIGURED``     — operator has not registered an account yet.
    """

    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass(frozen=True)
class CloudProviderStatus:
    """A capability snapshot for a single provider.

    Returned by :meth:`CloudProvider.get_capabilities` and exposed
    to the UI via the ``/api/cloud/providers`` endpoint.  ``state``
    is per-capability; ``details`` is a free-form dict (sanitized —
    no raw exception payloads) used to surface operator hints such
    as "AWS Compute Optimizer is not enrolled in this region".
    """

    provider: CloudProviderKind
    capabilities: Mapping[CloudCapability, CloudCapabilityState]
    details: Mapping[CloudCapability, Mapping[str, Any]] = field(default_factory=dict)
    supported_regions: Sequence[CloudRegion] = field(default_factory=tuple)
    supported_lookback_days: Sequence[int] = (7, 30, 60, 90)

    def has(self, capability: CloudCapability) -> bool:
        """Return True iff the capability is currently SUPPORTED."""
        return self.capabilities.get(capability) == CloudCapabilityState.SUPPORTED

    def describe(self) -> List[dict]:
        """Render a JSON-safe summary suitable for the API surface."""
        return [
            {
                "capability": cap.value,
                "state": state.value,
                "detail": dict(self.details.get(cap, {})),
            }
            for cap, state in sorted(
                self.capabilities.items(), key=lambda kv: kv[0].value
            )
        ]


# ---------------------------------------------------------------------------
# Credential-strategy types (Phase 7C.1: shapes only; no live impl).
# ---------------------------------------------------------------------------


class CloudCredentialStrategy(str, Enum):
    """How a provider obtains credentials for a given account.

    Phase 7C.1 defines the type so future Azure/Rackspace adapters
    can declare their strategy up-front.  No Azure or Rackspace
    credential is ever stored at this point.

    The strings are deliberately stable identifiers, not free-form
    labels, so they round-trip cleanly through the database and the
    API surface.
    """

    # AWS — EC2 instance profile (management account only).
    AWS_INSTANCE_PROFILE = "AWS_INSTANCE_PROFILE"
    # AWS — STS AssumeRole with optional external_id (target accounts).
    AWS_ASSUME_ROLE = "AWS_ASSUME_ROLE"
    # Azure — workload identity federation (production target).
    AZURE_WORKLOAD_FEDERATION = "AZURE_WORKLOAD_FEDERATION"
    # Azure — client secret (bootstrap / fallback only).
    AZURE_CLIENT_SECRET = "AZURE_CLIENT_SECRET"
    # Rackspace — API key in header.
    RACKSPACE_API_KEY = "RACKSPACE_API_KEY"

    @property
    def provider(self) -> CloudProviderKind:
        if self.value.startswith("AWS_"):
            return CloudProviderKind.AWS
        if self.value.startswith("AZURE_"):
            return CloudProviderKind.AZURE
        return CloudProviderKind.RACKSPACE


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class CloudProvider(abc.ABC):
    """The provider-neutral contract every adapter implements.

    Adapters are stateless apart from their immutable ``provider_kind``
    and any process-local caches they own.  Routes acquire adapters
    via :class:`ProviderRegistry`; routes MUST NOT instantiate
    adapters directly so the registry remains the single source of
    truth for what providers are active.
    """

    #: The kind this adapter serves.  Set as a class attribute.
    provider_kind: CloudProviderKind = None  # type: ignore[assignment]

    #: The credential strategies this adapter supports.  Set as a
    #: class attribute.  Used by the registry to refuse adapters
    #: that are not yet wired with any production strategy.
    credential_strategies: Sequence[CloudCredentialStrategy] = ()

    @property
    def provider_name(self) -> str:
        """Human-readable provider name; defaults to the enum value."""
        return self.provider_kind.value

    # ---- Account / region metadata ----------------------------------------

    @abc.abstractmethod
    def list_regions(self) -> List[CloudRegion]:
        """Return the regions this provider exposes.

        For AWS this is a small static list; for Azure it can come
        from the Resource Manager subscriptions API; for Rackspace
        it is a fixed list of datacenters.  Adapters should return
        only regions they can actually address — a region that is
        advertised but not enabled in the account is misleading.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def validate_account(self, account: _CloudAccountRow) -> "AccountValidationResult":
        """Return the connectivity status for a registry row.

        Never raises for permission / connectivity errors — those
        are encoded into :class:`AccountValidationResult`.  Only
        programming bugs propagate as exceptions.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_provider_account(self, account: _CloudAccountRow) -> CloudAccount:
        """Translate a registry row into a :class:`CloudAccount`.

        The mapping is provider-specific: AWS translates ``role_arn``
        into ``external_ref``; Azure will translate ``tenant_id +
        subscription_id`` into a subscription resource id; etc.
        """
        raise NotImplementedError

    # ---- Capabilities ------------------------------------------------------

    @abc.abstractmethod
    def get_capabilities(self, account: Optional[_CloudAccountRow] = None) -> CloudProviderStatus:
        """Return the capability snapshot for ``account`` (or the
        management account if ``account`` is ``None``).
        """
        raise NotImplementedError

    # ---- Cost --------------------------------------------------------------

    @abc.abstractmethod
    async def get_cost_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        previous_period_start: date,
        previous_period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostSummary:
        """Aggregate cost for ``accounts`` over the requested window.

        ``accounts`` is the set of registry rows the caller wants
        included.  The adapter decides how to interpret that set:
        the AWS adapter fans out across each row and sums; a future
        Azure adapter will do the same against subscriptions.

        The returned summary is attributed back to the provider and
        to each contributing account — never blended.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_cost_trend(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostTrend:
        """Return the daily cost trend for the requested window."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_cost_by_service(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> List[CloudServiceCost]:
        """Return the per-service cost breakdown for the window."""
        raise NotImplementedError

    # ---- Resources ---------------------------------------------------------

    @abc.abstractmethod
    async def get_resources(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> List[CloudResource]:
        """Return every resource discovered under ``accounts``."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_resource_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> CloudResourceSummary:
        """Return the grouped inventory suitable for the UI."""
        raise NotImplementedError

    # ---- Recommendations ---------------------------------------------------

    @abc.abstractmethod
    async def get_recommendations(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        days: int,
        region: Optional[str] = None,
    ) -> List[CloudRecommendation]:
        """Return the deduplicated recommendation list."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Validation result (provider-agnostic status envelope)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountValidationResult:
    """Connectivity result for a registry row.

    Stable, sanitized status string so the UI can map it without
    parsing provider-specific messages.
    """

    provider: CloudProviderKind
    account_id: str
    display_name: str
    status: str  # CONNECTED / ACCESS_DENIED / ROLE_NOT_FOUND / DISABLED / ERROR / NOT_CONFIGURED
    reason: Optional[str] = None
    arn: Optional[str] = None
    region: Optional[str] = None
    credential_strategy: Optional[CloudCredentialStrategy] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider.value,
            "account_id": self.account_id,
            "display_name": self.display_name,
            "status": self.status,
            "reason": self.reason,
            "arn": self.arn,
            "region": self.region,
            "credential_strategy": (
                self.credential_strategy.value
                if self.credential_strategy is not None
                else None
            ),
        }


__all__ = [
    "AccountValidationResult",
    "CloudCapability",
    "CloudCapabilityState",
    "CloudCredentialStrategy",
    "CloudProvider",
    "CloudProviderStatus",
]
