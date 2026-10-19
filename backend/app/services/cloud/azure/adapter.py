"""Azure adapter for the cloud-provider abstraction — Phase 7C.2A.

:class:`AzureCloudProvider` is the concrete implementation of
:class:`~app.services.cloud.base.CloudProvider` for Microsoft Azure.
It implements **only** the capabilities Phase 7C.2A delivers:

* provider identity (name, kind, credential strategies)
* authentication validation (Entra token exchange works)
* subscription validation (ARM subscription GET succeeds)
* subscription registration / selectability / enable / disable
* capability reporting (which capabilities are SUPPORTED today
  and which are NOT_IMPLEMENTED for future phases)

The full Cost Management / Resource Graph / Advisor surfaces are
explicitly OUT OF SCOPE for 7C.2A.  They will ship in
7C.2B / 7C.2C / 7C.2D.  The adapter's ``get_capabilities()``
returns ``NOT_IMPLEMENTED`` (via ``UNSUPPORTED``) for every
non-Azure-bootstrap capability so the UI can show the
"available in a future release" hint instead of attempting a
live call.

Credential strategy:

* ``AZURE_WORKLOAD_FEDERATION`` is the operational strategy.  The
  adapter delegates to
  :mod:`app.services.cloud.azure.credential` for token acquisition.
* ``AZURE_CLIENT_SECRET`` is reserved (Phase 7C.1 enum) but
  intentionally not implemented at this layer.

Subscription registry:

* Re-uses the existing ``cloud_accounts`` table from
  :mod:`app.db.models`.  Azure rows are simply
  ``provider='AZURE', account_id=<subscription-id>`` rows.
* ``tenant_id`` carries the Entra tenant GUID (shared across
  every subscription — single Entra application).
* ``is_management`` is intentionally **false** for every Azure
  row in this phase: Azure has no analogue of the AWS
  "management account" inside the registry.  The Entra tenant /
  client id are process-level configuration, not registry data.

Importing this module registers the adapter with the
:class:`ProviderRegistry` (see the bottom of the file).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount as _CloudAccountRow

from app.services.cloud.azure.arm_client import (
    AzureArmClient,
    AzureSubscriptionDescriptor,
)
from app.services.cloud.azure import credential as _credential_mod
from app.services.cloud.azure.credential import (
    AzureAuthError,
    AzureNotConfiguredError,
    AzureRuntimeConfig,
    acquire_access_token,
    get_credential,
    is_configured,
)


def _is_configured() -> bool:
    """Delegate to the credential module's ``is_configured`` symbol.

    Routing through ``_credential_mod.is_configured`` (rather than the
    module-level reference we imported above) lets tests monkeypatch
    ``credential.is_configured`` without having to also patch the
    adapter's namespace — a common source of confusion when the
    adapter imports its helper directly.
    """
    return _credential_mod.is_configured()
from app.services.cloud.azure.federation import (
    AWSWorkloadFederationCredential,
)
from app.services.cloud.azure.redaction import (
    AZURE_STATUS_ACCESS_DENIED,
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_CONNECTED,
    AZURE_STATUS_DISABLED,
    AZURE_STATUS_ERROR,
    AZURE_STATUS_NOT_CONFIGURED,
    AZURE_STATUS_NOT_FOUND,
    safe_subscription_label,
    scrub_log_payload,
)
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
from app.services.cloud.registry import ProviderRegistry


logger = logging.getLogger("cost-detective.cloud.azure")


# ---------------------------------------------------------------------------
# Region metadata
# ---------------------------------------------------------------------------


# A small, deliberately conservative list of Azure regions.  We
# return only the regions we can confidently address today; the
# region list will grow in a later phase once Cost Management /
# Resource Graph are wired in.
_AZURE_REGIONS: Tuple[CloudRegion, ...] = (
    CloudRegion(CloudProviderKind.AZURE, "eastus", "East US"),
    CloudRegion(CloudProviderKind.AZURE, "eastus2", "East US 2"),
    CloudRegion(CloudProviderKind.AZURE, "westus2", "West US 2"),
    CloudRegion(CloudProviderKind.AZURE, "westus3", "West US 3"),
    CloudRegion(CloudProviderKind.AZURE, "centralus", "Central US"),
    CloudRegion(CloudProviderKind.AZURE, "northeurope", "North Europe"),
    CloudRegion(CloudProviderKind.AZURE, "westeurope", "West Europe"),
    CloudRegion(CloudProviderKind.AZURE, "uksouth", "UK South"),
    CloudRegion(CloudProviderKind.AZURE, "japaneast", "Japan East"),
    CloudRegion(CloudProviderKind.AZURE, "southeastasia", "Southeast Asia"),
)


# ---------------------------------------------------------------------------
# Capability status mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CapabilityReport:
    """Convenience wrapper used to render the capability response."""

    capability: CloudCapability
    state: CloudCapabilityState
    detail: Mapping[str, Any]


def _not_implemented(cap: CloudCapability, phase: str) -> _CapabilityReport:
    """Render a NOT_IMPLEMENTED capability row.

    ``UNSUPPORTED`` is the right Phase 7C.1 state for a capability
    the adapter has not implemented (as opposed to ``UNAVAILABLE``
    which means the implementation exists but the upstream API is
    unreachable).  The detail string carries the phase the
    capability will land in so an operator reading the response
    knows where to look next.
    """
    return _CapabilityReport(
        capability=cap,
        state=CloudCapabilityState.UNSUPPORTED,
        detail={"phase": phase, "status": "NOT_IMPLEMENTED"},
    )


def _supported(cap: CloudCapability, detail: Mapping[str, Any]) -> _CapabilityReport:
    return _CapabilityReport(
        capability=cap,
        state=CloudCapabilityState.SUPPORTED,
        detail=dict(detail),
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AzureCloudProvider(CloudProvider):
    """The Azure implementation of the :class:`CloudProvider` contract."""

    provider_kind: CloudProviderKind = CloudProviderKind.AZURE
    credential_strategies: Sequence[CloudCredentialStrategy] = (
        CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION,
    )

    # ---- Account / region metadata ----------------------------------------

    def list_regions(self) -> List[CloudRegion]:
        return list(_AZURE_REGIONS)

    def get_provider_account(self, account: _CloudAccountRow) -> CloudAccount:
        """Translate an Azure registry row into a provider-neutral
        :class:`CloudAccount`.

        ``account_id`` for Azure rows carries the subscription GUID.
        ``external_ref`` carries the ARM resource id of the
        subscription (``/subscriptions/<id>``).  ``is_management``
        is always False in this phase — the Entra tenant is a
        process-level concept, not a per-row concept.
        """
        return CloudAccount(
            provider=CloudProviderKind.AZURE,
            account_id=account.account_id,
            display_name=account.display_name,
            is_management=False,
            enabled=bool(account.enabled),
            external_ref=f"/subscriptions/{account.account_id}",
            metadata={
                "tenant_id": account.tenant_id,
                "subscription_id": account.subscription_id or account.account_id,
                "credential_strategy": (
                    CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION.value
                ),
            },
        )

    # ---- Capabilities ------------------------------------------------------

    def get_capabilities(
        self, account: Optional[_CloudAccountRow] = None
    ) -> CloudProviderStatus:
        """Return the Azure capability snapshot.

        Phase 7C.2A: ``AUTHENTICATION`` + ``SUBSCRIPTIONS``.
        Phase 7C.2B: ``COSTS`` (Microsoft Cost Management).
        Phase 7C.2C: ``RESOURCES`` (Resource Graph inventory).
        Phase 7C.2D: ``OPTIMIZATION`` (Azure Advisor Cost
        recommendations).  ``ADVISOR`` is reported as ``UNSUPPORTED``
        until a dedicated Advisor surface ships; ``AI_EVIDENCE`` /
        ``METRICS`` / ``BILLING`` remain ``UNSUPPORTED``.
        """
        runtime_configured = _credential_mod.is_configured()
        auth_state = (
            CloudCapabilityState.SUPPORTED
            if runtime_configured
            else CloudCapabilityState.NOT_CONFIGURED
        )
        subs_state = (
            CloudCapabilityState.SUPPORTED
            if runtime_configured
            else CloudCapabilityState.NOT_CONFIGURED
        )
        costs_state = (
            CloudCapabilityState.SUPPORTED
            if runtime_configured
            else CloudCapabilityState.NOT_CONFIGURED
        )
        resources_state = (
            CloudCapabilityState.SUPPORTED
            if runtime_configured
            else CloudCapabilityState.NOT_CONFIGURED
        )
        optimization_state = (
            CloudCapabilityState.SUPPORTED
            if runtime_configured
            else CloudCapabilityState.NOT_CONFIGURED
        )
        capabilities: Dict[CloudCapability, CloudCapabilityState] = {
            CloudCapability.AUTHENTICATION: auth_state,
            CloudCapability.SUBSCRIPTIONS: subs_state,
            CloudCapability.COSTS: costs_state,
            CloudCapability.RESOURCES: resources_state,
            CloudCapability.OPTIMIZATION: optimization_state,
            # Future phases:
            CloudCapability.METRICS: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.BILLING: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.ADVISOR: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.AI_EVIDENCE: CloudCapabilityState.UNSUPPORTED,
        }
        details: Dict[CloudCapability, Mapping[str, Any]] = {
            CloudCapability.AUTHENTICATION: {
                "strategy": CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION.value,
                "source": "AWS STS GetWebIdentityToken → Microsoft Entra federated credential",
                "configured": runtime_configured,
                "status": "CONNECTED" if runtime_configured else "NOT_CONFIGURED",
            },
            CloudCapability.SUBSCRIPTIONS: {
                "source": "Azure Resource Manager subscriptions API",
                "validation_endpoint": "GET /subscriptions/{id}",
                "configured": runtime_configured,
            },
            CloudCapability.COSTS: {
                "phase": "7C.2B",
                "status": "SUPPORTED" if runtime_configured else "NOT_CONFIGURED",
                "source": "Microsoft Cost Management Query API",
                "api_version": "2026-06-01",
                "cost_type": "ActualCost",
                "scope": "subscription",
                "dimensions": ["ServiceName", "ResourceLocation"],
                "granularity": ["Daily", "None"],
            },
            CloudCapability.RESOURCES: {
                "phase": "7C.2C",
                "status": "SUPPORTED" if runtime_configured else "NOT_CONFIGURED",
                "source": "Microsoft Resource Graph Query API",
                "api_version": "2024-04-01",
                "scope": "subscription",
                "default_columns": [
                    "id",
                    "name",
                    "type",
                    "location",
                    "resourceGroup",
                    "subscriptionId",
                    "tags",
                ],
            },
            CloudCapability.OPTIMIZATION: {
                "phase": "7C.2D",
                "status": "SUPPORTED" if runtime_configured else "NOT_CONFIGURED",
                "source": "Microsoft Advisor (via Resource Graph advisorresources table)",
                "resource_graph_api_version": "2024-04-01",
                "advisor_query": "advisorresources | where type == microsoft.advisor/recommendations | where properties.category == Cost | where properties.recommendationStatus == Active",
                "scope": "subscription",
                "savings_field": "extendedProperties.annualSavingsAmount",
                "savings_currency_field": "extendedProperties.savingsCurrency",
                "read_only": True,
            },
            CloudCapability.METRICS: {"phase": "future", "status": "NOT_IMPLEMENTED"},
            CloudCapability.BILLING: {"phase": "future", "status": "NOT_IMPLEMENTED"},
            CloudCapability.ADVISOR: {"phase": "future", "status": "NOT_IMPLEMENTED"},
            CloudCapability.AI_EVIDENCE: {"phase": "future", "status": "NOT_IMPLEMENTED"},
        }
        return CloudProviderStatus(
            provider=CloudProviderKind.AZURE,
            capabilities=capabilities,
            details=details,
            supported_regions=_AZURE_REGIONS,
            supported_lookback_days=(7, 30, 60, 90),
        )

    # ---- Validation --------------------------------------------------------

    def validate_account(
        self, account: _CloudAccountRow
    ) -> AccountValidationResult:
        """Resolve the connectivity status for an Azure subscription.

        Maps the ARM response onto the Phase 7C.1 status enum.
        ``is_management`` is not special-cased for Azure rows — the
        Entra tenant is process-level configuration; per-row
        enable/disable is the only mutable state.

        Disabled accounts short-circuit to ``DISABLED`` so the
        adapter never makes a network call for a row the operator
        has turned off.
        """
        strategy = CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION
        if not account.enabled:
            return AccountValidationResult(
                provider=CloudProviderKind.AZURE,
                account_id=account.account_id,
                display_name=account.display_name,
                status=AZURE_STATUS_DISABLED,
                reason="subscription is disabled",
                credential_strategy=strategy,
            )

        if not _credential_mod.is_configured():
            return AccountValidationResult(
                provider=CloudProviderKind.AZURE,
                account_id=account.account_id,
                display_name=account.display_name,
                status=AZURE_STATUS_NOT_CONFIGURED,
                reason="Azure runtime configuration is missing (AZURE_TENANT_ID / AZURE_CLIENT_ID)",
                credential_strategy=strategy,
            )

        descriptor = self._validate_subscription_unsafe(account.account_id)
        # Translate ARM descriptor → AccountValidationResult.  We
        # deliberately collapse every non-CONNECTED state into a
        # sanitised message so the API surface never echoes the
        # raw ARM body.
        arm_status = descriptor.status
        status = arm_status  # The arm_client already produces the
        # Phase 7C.1 status enum.
        # Map arm-client status → Phase 7C.1 AccountValidationResult.
        # The arm client already returns CONNECTED / NOT_FOUND /
        # ACCESS_DENIED / AUTH_FAILED; we just route those values
        # straight through.  DISABLED was handled above.
        if status == AZURE_STATUS_CONNECTED:
            reason: Optional[str] = (
                f"subscription state={descriptor.state}" if descriptor.state else None
            )
            arn = descriptor.subscription_id
        elif status == AZURE_STATUS_NOT_FOUND:
            reason = "subscription id is not visible to the service principal"
            arn = None
        elif status == AZURE_STATUS_ACCESS_DENIED:
            reason = "service principal lacks Reader/Cost Management Reader RBAC"
            arn = None
        elif status == AZURE_STATUS_AUTH_FAILED:
            reason = "Entra rejected the federated assertion"
            arn = None
        elif status == AZURE_STATUS_NOT_CONFIGURED:
            reason = "Azure runtime configuration is missing"
            arn = None
        else:
            reason = "ARM returned an unexpected response"
            arn = None

        return AccountValidationResult(
            provider=CloudProviderKind.AZURE,
            account_id=account.account_id,
            display_name=account.display_name,
            status=status,
            reason=reason,
            arn=arn,
            region=None,
            credential_strategy=strategy,
        )

    # ---- Internals ---------------------------------------------------------

    def _validate_subscription_unsafe(
        self, subscription_id: str
    ) -> AzureSubscriptionDescriptor:
        """Call ARM and return the descriptor.

        This helper never raises — every error is folded into the
        descriptor's ``status`` field by ``AzureArmClient``.
        """
        client = AzureArmClient()
        try:
            return client.validate_subscription(subscription_id)
        finally:
            client.close()

    # ---- Cost (Phase 7C.2B) ---------------------------------------------

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
        """Aggregate Cost Management data for ``accounts`` over the window.

        ``accounts`` is the set of registry rows the caller wants
        included; the adapter fans out across subscriptions with
        bounded concurrency and folds the per-subscription snapshots
        into a single provider-neutral
        :class:`~app.services.cloud.models.CloudCostSummary`.
        """
        from app.services.cloud.azure.cost_aggregator import (
            aggregate_cost_summary_for_subscriptions,
        )

        rows = list(accounts) if accounts else []
        summary, failures = (
            await aggregate_cost_summary_for_subscriptions(
                rows=rows,
                period_start=period_start,
                period_end=period_end,
                previous_period_start=previous_period_start,
                previous_period_end=previous_period_end,
                today=date.today(),
                region=region,
            )
        )
        # Phase 7D.3.3 — the daily points are already stashed on
        # ``extensions`` by the aggregator (``_daily_points`` and
        # ``_daily_currency_status``); we don't need to do anything
        # additional here.  Return the summary as-is.
        return summary

    async def get_cost_trend(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostTrend:
        """Return the daily cost trend.

        Phase 7D.3.3 — the trend is derived from the same snapshot
        pass that produced the summary so this call issues ZERO
        additional Azure POSTs.  This adapter calls
        :meth:`get_cost_summary` once and reuses the rolled-up
        daily points stashed on the summary's ``extensions``.
        """
        from app.services.cloud.azure.cost_aggregator import (
            build_trend_from_points,
        )
        from decimal import Decimal as _Decimal

        rows = list(accounts) if accounts else []
        summary = await self.get_cost_summary(
            accounts=accounts,
            period_start=period_start,
            period_end=period_end,
            previous_period_start=period_start,
            previous_period_end=period_end,
            region=region,
        )
        extensions = dict(summary.extensions or {})
        cached_points = extensions.get("_daily_points") or []
        daily = [
            (date.fromisoformat(p["date"]), _Decimal(str(p["amount"])), p["currency"])
            for p in cached_points
        ]
        return build_trend_from_points(
            rows=rows,
            period_start=period_start,
            period_end=period_end,
            daily_points=daily,
        )

    async def get_cost_by_service(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> List[CloudServiceCost]:
        from app.services.cloud.azure.cost_aggregator import (
            aggregate_cost_by_service_for_subscriptions,
        )

        rows = list(accounts) if accounts else []
        items, status, failures = await aggregate_cost_by_service_for_subscriptions(
            rows=rows,
            period_start=period_start,
            period_end=period_end,
            today=date.today(),
        )
        return list(items)

    # ---- Resources (Phase 7C.2C) -----------------------------------------

    async def get_resources(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> List[CloudResource]:
        """Enumerate resources via Azure Resource Graph.

        ``accounts`` is the set of registry rows the caller wants
        included; the adapter fans out across subscriptions with
        bounded concurrency and folds the per-subscription snapshots
        into a single flat list of normalised
        :class:`~app.services.cloud.models.CloudResource` shapes.
        The ``region`` argument is intentionally ignored: Azure
        Resource Graph returns location as a column, not a query
        parameter; future region filtering belongs at the route
        layer.
        """
        from app.services.cloud.azure.resource_aggregator import (
            aggregate_resources_for_subscriptions,
        )

        rows = list(accounts) if accounts else []
        _summary, flat, _failures = await aggregate_resources_for_subscriptions(
            rows=rows
        )
        return list(flat)

    async def get_resource_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> CloudResourceSummary:
        """Aggregate a resource summary via Azure Resource Graph."""
        from app.services.cloud.azure.resource_aggregator import (
            aggregate_resources_for_subscriptions,
        )

        rows = list(accounts) if accounts else []
        summary, _flat, _failures = await aggregate_resources_for_subscriptions(
            rows=rows
        )
        return summary

    # ---- Stubs (out of scope for 7C.2C) ------------------------------------

    # Phase 7C.2D — recommendations / Advisor — is the only remaining
    # abstract method on ``CloudProvider`` that the Azure adapter has
    # not implemented.  Keep it as a NotImplementedError so any
    # regression that calls into it is loud.

    async def get_recommendations(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        days: int = 90,
        region: Optional[str] = None,
    ) -> List[CloudRecommendation]:
        """Enumerate active Azure Advisor Cost recommendations.

        ``accounts`` is the set of registry rows the caller wants
        included; the adapter delegates to the Advisor aggregator
        which fans out across subscriptions with bounded
        concurrency.  The ``days`` argument is intentionally
        ignored: Advisor recommendations carry their own
        ``lastUpdated`` timestamp; we do not synthesise a time
        window.  ``region`` is also ignored: Advisor
        recommendations are subscription-scoped, not
        region-scoped.
        """
        from app.services.cloud.azure.advisor_aggregator import (
            aggregate_advisor_recommendations_for_subscriptions,
        )

        rows = list(accounts) if accounts else []
        flat, _rollup, _failures = await aggregate_advisor_recommendations_for_subscriptions(
            rows=rows
        )
        return list(flat)


# ---------------------------------------------------------------------------
# Subscription bootstrap
# ---------------------------------------------------------------------------


# Subscription ids / display names are *configuration data*, not
# secrets.  They are NOT hardcoded into the provider module —
# they are read from runtime configuration (see
# :mod:`app.services.cloud.azure.registrations`).  The provider
# exposes a single ``bootstrap`` entry point that the application
# startup wires through that configuration.


def _bootstrap_azure_subscriptions(
    db: Any,
    *,
    registrations: Sequence[Mapping[str, str]] = (),
) -> int:
    """Idempotently register the two real Azure subscriptions.

    Returns the number of NEW rows inserted (existing rows are
    silently kept).  Refuses to insert rows when the runtime is
    not configured — ``tenant_id`` would have no meaningful value
    without ``AZURE_TENANT_ID``.
    """
    if not _credential_mod.is_configured():
        logger.info(
            "azure.bootstrap.skipped reason=not_configured registrations=%d",
            len(registrations),
        )
        return 0

    runtime = AzureRuntimeConfig.from_settings()
    if runtime is None:
        return 0

    from sqlalchemy import select
    from app.db.models import CloudAccount

    inserted = 0
    for entry in registrations:
        sub_id = (entry.get("subscription_id") or "").strip()
        display_name = (entry.get("display_name") or sub_id).strip()
        if not sub_id:
            continue
        try:
            existing = db.execute(
                select(CloudAccount).where(
                    CloudAccount.provider == "AZURE",
                    CloudAccount.account_id == sub_id,
                )
            ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "azure.bootstrap.lookup_failed code=%s",
                type(exc).__name__,
            )
            continue

        if existing is not None:
            # Idempotent path — keep the existing row untouched
            # except for tenant_id if it was empty for any reason.
            if not existing.tenant_id:
                existing.tenant_id = runtime.tenant_id
                existing.subscription_id = sub_id
                try:
                    db.commit()
                except Exception:  # noqa: BLE001 — defensive
                    db.rollback()
            continue

        row = CloudAccount(
            provider="AZURE",
            account_id=sub_id,
            display_name=display_name,
            role_arn=None,
            external_id=None,
            tenant_id=runtime.tenant_id,
            subscription_id=sub_id,
            provider_metadata={
                "credential_strategy": (
                    CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION.value
                ),
                "bootstrap_source": "phase7c2a",
            },
            enabled=True,
            is_management=False,
            preferred_region=None,
        )
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
            inserted += 1
            logger.info(
                "azure.bootstrap.inserted sub=%s display=%s",
                safe_subscription_label(sub_id),
                scrub_log_payload(display_name)[:40],
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            db.rollback()
            logger.warning(
                "azure.bootstrap.insert_failed sub=%s code=%s",
                safe_subscription_label(sub_id),
                type(exc).__name__,
            )
    return inserted


def bootstrap_azure_subscriptions_once(*, force: bool = False) -> int:
    """Process-wide idempotent subscription bootstrap.

    Called from ``app.main`` at startup; safe to invoke multiple
    times.  Returns the count of newly-inserted rows.

    The actual subscription list comes from runtime configuration
    (``app.services.cloud.azure.registrations.get_bootstrap_registrations``),
    not from the provider module.  This keeps the provider
    implementation free of operator-specific identifiers.
    """
    if not force and getattr(bootstrap_azure_subscriptions_once, "_done", False):
        return 0
    from app.services.cloud.azure.registrations import get_bootstrap_registrations
    from app.db.session import SessionLocal

    registrations = get_bootstrap_registrations()
    own_session = False
    db = None
    try:
        db = SessionLocal()
        own_session = True
        inserted = _bootstrap_azure_subscriptions(db, registrations=list(registrations))
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.warning(
            "azure.bootstrap.failed code=%s",
            type(exc).__name__,
        )
        inserted = 0
    finally:
        if own_session and db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 — defensive
                pass
    bootstrap_azure_subscriptions_once._done = True
    return inserted


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register this adapter with the singleton :class:`ProviderRegistry`.

    When the runtime is configured we register the Azure adapter as
    enabled; otherwise we register as DISABLED so the registry
    surface still lists Azure (admin UIs can see it exists) but
    ``require(AZURE)`` raises ``ProviderNotFound`` with
    ``disabled=True``.
    """
    registry = ProviderRegistry.current()
    registry.register(AzureCloudProvider())
    # NOTE: we deliberately do NOT auto-disable the Azure adapter at
    # import time.  Tests pin a ``disable`` / ``enable`` lifecycle on
    # the registry; in production the API layer / ``ProviderRegistry``
    # already routes ``require(kind)`` through the disabled-set and
    # the runtime-config check inside the adapter's
    # ``validate_account`` / ``get_capabilities`` short-circuits to
    # ``NOT_CONFIGURED``.  An operator who wants Azure-disabled
    # behaviour in production can call ``registry.disable(AZURE)``
    # from their deployment bootstrap.


_register()


__all__ = [
    "AzureCloudProvider",
    "bootstrap_azure_subscriptions_once",
]
