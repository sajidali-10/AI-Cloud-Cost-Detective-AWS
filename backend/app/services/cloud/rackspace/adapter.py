"""Rackspace adapter for the cloud-provider abstraction — Phase 9B.1.

:class:`RackspaceCloudProvider` is the concrete implementation of
:class:`~app.services.cloud.base.CloudProvider` for Rackspace.  It
implements **only** the canonical COSTS surface (Phase 9B.1):

* provider identity (name, kind, credential strategy)
* authentication validation (Rackspace Identity v2.0 token exchange)
* capability reporting (COSTS / BILLING supported when configured;
  RESOURCES / OPTIMIZATION explicitly unsupported — Phase 9B.2+)
* canonical cost summary backed by Rackspace Billing V2
  ``estimated_charges``

Everything transport-related is delegated to the Phase 9A modules
in :mod:`app.services.rackspace` — there is exactly ONE Rackspace
authentication implementation in this codebase (in-memory token
cache, no persistence, no logging).

Account identity model
---------------------

Rackspace has two distinct identifiers that are NEVER conflated:

* **Cloud tenant / account id** (``RACKSPACE_ACCOUNT_ID``) — the
  Identity-scoped tenant.  This is the safe, non-secret identifier
  stored in the ``cloud_accounts`` registry and surfaced in API
  responses.
* **Billing Account Number / RAN** (``RACKSPACE_BILLING_ACCOUNT``) —
  the path scope for Billing V2 calls.  It is treated as protected
  configuration: never persisted in the registry, never logged,
  never echoed in API responses or error messages.

Credential strategy: ``RACKSPACE_API_KEY`` (username + API key in
the host ``.env`` — the application never stores Rackspace
credentials in PostgreSQL).

Read-only guarantee
-------------------

The adapter issues only ``GET`` requests against product APIs.
The single write-like verb in the entire Rackspace integration is
the Identity authentication ``POST``.  No Rackspace write actions
exist in this codebase.

Importing this module registers the adapter with the
:class:`~app.services.cloud.registry.ProviderRegistry` (see the
bottom of the file).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount as _CloudAccountRow

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
from app.services.cloud.rackspace.costs import (
    RACKSPACE_COST_SOURCE,
    RackspaceCostError,
    RackspaceCostErrorCategory,
    assert_period_overlap,
    fetch_cost_snapshot,
)
from app.services.cloud.rackspace.recommendations import (
    RACKSPACE_RECOMMENDATION_SOURCE,
    aggregate_rackspace_recommendations_for_account,
)
from app.services.cloud.rackspace.resources import (
    RACKSPACE_RESOURCE_SOURCE,
    RackspaceResourceError,
    aggregate_rackspace_resources,
)
from app.services.rackspace import get_billing_client
from app.services.rackspace.config import (
    billing_account_configured,
    get_config_status,
)
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceElevatedRoleError,
    RackspaceError,
    RackspacePermissionError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import ELEVATED_ROLES, RackspaceIdentity


logger = logging.getLogger("cost-detective.cloud.rackspace")


# ---------------------------------------------------------------------------
# Sanitized status vocabulary (mirrors the AWS / Azure adapters)
# ---------------------------------------------------------------------------

RACKSPACE_STATUS_CONNECTED = "CONNECTED"
RACKSPACE_STATUS_ACCESS_DENIED = "ACCESS_DENIED"
RACKSPACE_STATUS_AUTH_FAILED = "AUTH_FAILED"
RACKSPACE_STATUS_DISABLED = "DISABLED"
RACKSPACE_STATUS_ERROR = "ERROR"
RACKSPACE_STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# Region metadata
# ---------------------------------------------------------------------------


# Rackspace operates a small set of regional datacenters.  The cost
# surface is billing-account scoped (not regional), so this list is
# informational only — no region filtering is applied to Billing V2
# calls.  Kept deliberately conservative: only the datacenters the
# Identity catalog is known to publish.
_RACKSPACE_REGIONS: Tuple[CloudRegion, ...] = (
    CloudRegion(CloudProviderKind.RACKSPACE, "DFW", "Dallas"),
    CloudRegion(CloudProviderKind.RACKSPACE, "ORD", "Chicago"),
    CloudRegion(CloudProviderKind.RACKSPACE, "IAD", "Northern Virginia"),
    CloudRegion(CloudProviderKind.RACKSPACE, "LON", "London"),
    CloudRegion(CloudProviderKind.RACKSPACE, "SYD", "Sydney"),
    CloudRegion(CloudProviderKind.RACKSPACE, "HKG", "Hong Kong"),
)


def _costs_configured() -> bool:
    """True when BOTH Identity credentials and the RAN are configured."""

    return get_config_status().configured and billing_account_configured()


def _identity_configured() -> bool:
    return get_config_status().configured


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RackspaceCloudProvider(CloudProvider):
    """The Rackspace implementation of the :class:`CloudProvider` contract."""

    provider_kind: CloudProviderKind = CloudProviderKind.RACKSPACE
    credential_strategies: Sequence[CloudCredentialStrategy] = (
        CloudCredentialStrategy.RACKSPACE_API_KEY,
    )

    # ---- Account / region metadata ----------------------------------------

    def list_regions(self) -> List[CloudRegion]:
        return list(_RACKSPACE_REGIONS)

    def get_provider_account(self, account: _CloudAccountRow) -> CloudAccount:
        """Translate a Rackspace registry row into a provider-neutral
        :class:`CloudAccount`.

        ``account_id`` carries the Cloud tenant id (non-secret).
        ``external_ref`` is intentionally ``None`` — the Billing
        Account Number (RAN) is protected configuration and is
        NEVER persisted in the registry or echoed on API surfaces.
        """
        return CloudAccount(
            provider=CloudProviderKind.RACKSPACE,
            account_id=account.account_id,
            display_name=account.display_name,
            is_management=False,
            enabled=bool(account.enabled),
            external_ref=None,
            metadata={
                "credential_strategy": (
                    CloudCredentialStrategy.RACKSPACE_API_KEY.value
                ),
            },
        )

    # ---- Capabilities ------------------------------------------------------

    def get_capabilities(
        self, account: Optional[_CloudAccountRow] = None
    ) -> CloudProviderStatus:
        """Return the Rackspace capability snapshot.

        Phase 9B.1: ``AUTHENTICATION`` + ``COSTS`` + ``BILLING``.
        Phase 9B.2: ``RESOURCES`` (catalog-driven read-only inventory
        of Cloud Servers / Block Storage / Load Balancers).
        Phase 9C.2: ``OPTIMIZATION`` — the Phase 9C.1 deterministic
        engine exposed through the canonical recommendation surface.
        METRICS / ADVISOR / AI_EVIDENCE remain UNSUPPORTED.
        """
        identity_ok = _identity_configured()
        costs_ok = _costs_configured()

        def _state(configured: bool) -> CloudCapabilityState:
            return (
                CloudCapabilityState.SUPPORTED
                if configured
                else CloudCapabilityState.NOT_CONFIGURED
            )

        capabilities: Dict[CloudCapability, CloudCapabilityState] = {
            CloudCapability.AUTHENTICATION: _state(identity_ok),
            CloudCapability.COSTS: _state(costs_ok),
            CloudCapability.BILLING: _state(costs_ok),
            CloudCapability.RESOURCES: _state(identity_ok),
            CloudCapability.OPTIMIZATION: _state(identity_ok),
            CloudCapability.METRICS: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.ADVISOR: CloudCapabilityState.UNSUPPORTED,
            CloudCapability.AI_EVIDENCE: CloudCapabilityState.UNSUPPORTED,
        }
        details: Dict[CloudCapability, Mapping[str, Any]] = {
            CloudCapability.AUTHENTICATION: {
                "strategy": CloudCredentialStrategy.RACKSPACE_API_KEY.value,
                "source": "Rackspace Identity v2.0 apiKeyCredentials",
                "configured": identity_ok,
                "status": (
                    RACKSPACE_STATUS_CONNECTED if identity_ok
                    else RACKSPACE_STATUS_NOT_CONFIGURED
                ),
            },
            CloudCapability.COSTS: {
                "phase": "9B.1",
                "status": (
                    "SUPPORTED" if costs_ok else "NOT_CONFIGURED"
                ),
                "source": RACKSPACE_COST_SOURCE,
                "endpoint": "GET /v2/accounts/{ran}/estimated_charges",
                "periods": "current billing cycle only",
                "dimensions": ["service"],
                "unsupported_dimensions": [
                    "daily_trend",
                    "region_breakdown",
                    "previous_period",
                ],
                "read_only": True,
                "configured": costs_ok,
            },
            CloudCapability.BILLING: {
                "phase": "9B.1",
                "status": (
                    "SUPPORTED" if costs_ok else "NOT_CONFIGURED"
                ),
                "source": "Rackspace Billing V2",
                "role": "billing:observer",
                "read_only": True,
                "configured": costs_ok,
            },
            CloudCapability.RESOURCES: {
                "phase": "9B.2",
                "status": (
                    "SUPPORTED" if identity_ok else "NOT_CONFIGURED"
                ),
                "source": RACKSPACE_RESOURCE_SOURCE,
                "resource_types": [
                    "SERVER",
                    "VOLUME",
                    "LOAD_BALANCER",
                ],
                "services": [
                    "Cloud Servers",
                    "Cloud Block Storage",
                    "Cloud Load Balancers",
                ],
                "catalog_resolved": True,
                "multi_region": True,
                "read_only": True,
                "configured": identity_ok,
            },
            CloudCapability.OPTIMIZATION: {
                "phase": "9C.2",
                "status": (
                    "SUPPORTED" if identity_ok else "NOT_CONFIGURED"
                ),
                "source": RACKSPACE_RECOMMENDATION_SOURCE,
                "engine": "deterministic (Phase 9C.1)",
                "rules": [
                    "RS-VOL-001",
                    "RS-SRV-001",
                    "RS-LB-001",
                ],
                "rightsizing_evidence_status": "DEFERRED",
                "savings_semantics": (
                    "unknown savings are null, never zero"
                ),
                "read_only": True,
                "configured": identity_ok,
            },
            CloudCapability.METRICS: {"phase": "future", "status": "NOT_IMPLEMENTED"},
            CloudCapability.ADVISOR: {"phase": "future", "status": "NOT_IMPLEMENTED"},
            CloudCapability.AI_EVIDENCE: {"phase": "future", "status": "NOT_IMPLEMENTED"},
        }
        return CloudProviderStatus(
            provider=CloudProviderKind.RACKSPACE,
            capabilities=capabilities,
            details=details,
            supported_regions=_RACKSPACE_REGIONS,
            supported_lookback_days=(7, 30, 60, 90),
        )

    # ---- Validation --------------------------------------------------------

    def validate_account(
        self, account: _CloudAccountRow
    ) -> AccountValidationResult:
        """Resolve the connectivity status for the Rackspace account.

        Performs a real Rackspace Identity authentication (the only
        write-like verb in the integration).  Fails closed when an
        elevated role is observed on the principal — the read-only
        contract is non-negotiable.  Never raises; every failure is
        encoded into the sanitized result.
        """
        strategy = CloudCredentialStrategy.RACKSPACE_API_KEY

        def _result(status: str, reason: Optional[str]) -> AccountValidationResult:
            return AccountValidationResult(
                provider=CloudProviderKind.RACKSPACE,
                account_id=account.account_id,
                display_name=account.display_name,
                status=status,
                reason=reason,
                arn=None,
                region=None,
                credential_strategy=strategy,
            )

        if not account.enabled:
            return _result(
                RACKSPACE_STATUS_DISABLED, "account is disabled"
            )
        if not _identity_configured():
            return _result(
                RACKSPACE_STATUS_NOT_CONFIGURED,
                "Rackspace runtime configuration is missing "
                "(RACKSPACE_USERNAME / RACKSPACE_API_KEY / RACKSPACE_ACCOUNT_ID)",
            )

        # Real connectivity check via Identity.  A synchronous event
        # loop is required; ``validate_account`` is a sync contract, so
        # we drive the coroutine explicitly.
        import asyncio

        async def _auth() -> Any:
            from app.services.rackspace import get_identity_client

            return await get_identity_client().current_session()

        try:
            session = asyncio.run(_auth())
        except RackspaceConfigError:
            return _result(
                RACKSPACE_STATUS_NOT_CONFIGURED,
                "Rackspace runtime configuration is missing",
            )
        except RackspaceAuthError:
            return _result(
                RACKSPACE_STATUS_AUTH_FAILED,
                "Rackspace Identity rejected the credentials",
            )
        except RackspacePermissionError:
            return _result(
                RACKSPACE_STATUS_ACCESS_DENIED,
                "Rackspace Identity denied access",
            )
        except RackspaceElevatedRoleError:
            return _result(
                RACKSPACE_STATUS_ACCESS_DENIED,
                "elevated role detected; read-only contract violated",
            )
        except (RackspaceTransportError, RackspaceError):
            return _result(
                RACKSPACE_STATUS_ERROR,
                "Rackspace Identity is unreachable",
            )
        except Exception:  # noqa: BLE001 — defensive
            return _result(
                RACKSPACE_STATUS_ERROR,
                "Rackspace validation failed unexpectedly",
            )

        elevated = sorted(set(session.role_names) & set(ELEVATED_ROLES))
        if elevated:
            # Fail closed: the product contract is READ ONLY.
            logger.warning(
                "rackspace.validate.elevated_roles_present count=%d",
                len(elevated),
            )
            return _result(
                RACKSPACE_STATUS_ACCESS_DENIED,
                "elevated role detected; read-only contract requires "
                "observer roles (billing:observer)",
            )
        return _result(RACKSPACE_STATUS_CONNECTED, "Rackspace Identity authenticated")

    # ---- Cost (Phase 9B.1) -------------------------------------------------

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
        """Aggregate Billing V2 estimated charges for the current cycle.

        The Rackspace Billing API answers ONLY for the current
        billing cycle.  The requested window must overlap the actual
        source period (otherwise :class:`RackspaceCostError` with
        category ``PERIOD_UNSUPPORTED`` propagates to the route).

        The returned summary exposes the ACTUAL source period (as
        returned by Rackspace) — never the requested window — so the
        canonical envelope cannot misrepresent coverage.  The
        previous-period comparison is deliberately unavailable
        (``previous_total=None``): historical totals are a Phase 9B
        follow-up and are never fabricated.

        The ``region`` argument is ignored: Billing V2 is scoped to
        the billing account, not to a datacenter.
        """
        rows = [row for row in (accounts or []) if row is not None]
        if not rows:
            raise RackspaceCostError(
                RackspaceCostErrorCategory.NOT_CONFIGURED,
                "no Rackspace accounts are registered",
            )

        snapshot = await fetch_cost_snapshot(get_billing_client())
        assert_period_overlap(
            requested_start=period_start,
            requested_end=period_end,
            snapshot=snapshot,
        )

        # Account attribution.  The billing scope is a single RAN;
        # the registry row carries the safe Cloud tenant identity.
        primary = rows[0]
        account_id_value: Optional[str] = (
            primary.account_id if len(rows) == 1 else None
        )
        warnings: List[str] = list(snapshot.warnings)
        if len(rows) > 1:
            warnings.append(
                f"multiple_account_rows_requested={len(rows)}"
            )

        # True-zero semantics: a valid snapshot with total == 0 is a
        # SUCCESS — indistinguishable in status from any other valid
        # response, never an unavailable state.
        status = "SUCCESS"

        by_service: List[CloudServiceCost] = [
            CloudServiceCost(
                service=item.service,
                amount=item.amount,
                currency=snapshot.currency,
                provider=CloudProviderKind.RACKSPACE,
                account_id=account_id_value,
                region=None,
            )
            for item in snapshot.by_service
        ]

        extensions: Dict[str, Any] = {
            "status": status,
            "source": RACKSPACE_COST_SOURCE,
            "warnings": warnings,
            "by_account": [
                {
                    "account_id": primary.account_id,
                    "display_name": primary.display_name,
                    "amount": str(snapshot.total),
                }
            ],
            "account_errors": [],
            # The requested window may differ from the source period
            # (e.g. a 30-day lookback spanning two cycles).  Flag it
            # so the envelope consumer can see the adjustment.
            "period_adjusted": not (
                period_start == snapshot.period_start
                and period_end == snapshot.period_end
            ),
            "unsupported_dimensions": [
                "daily_trend",
                "region_breakdown",
                "previous_period",
            ],
            "record_count": snapshot.record_count,
            "currency_status": "OK",
            "previous_period_unavailable": True,
        }

        return CloudCostSummary(
            provider=CloudProviderKind.RACKSPACE,
            account_id=account_id_value,
            scope=(
                f"acct:{primary.account_id}"
                if len(rows) == 1
                else "all"
            ),
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            currency=snapshot.currency,
            current_total=snapshot.total,
            estimated=snapshot.estimated,
            previous_total=None,
            change_amount=None,
            change_percent=None,
            by_service=by_service,
            by_region=[],
            extensions=extensions,
        )

    async def get_cost_trend(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> CloudCostTrend:
        """Return the daily cost trend — deliberately EMPTY.

        ``estimated_charges`` provides no reliable per-day
        granularity.  Returning an empty series (instead of
        fabricating daily points from the cycle total) is the
        documented Phase 9B.1 behavior; the canonical envelope
        renders its empty-trend state.
        """
        rows = [row for row in (accounts or []) if row is not None]
        account_id_value = rows[0].account_id if len(rows) == 1 else None
        return CloudCostTrend(
            provider=CloudProviderKind.RACKSPACE,
            account_id=account_id_value,
            period_start=period_start,
            period_end=period_end,
            currency="",  # no monetary data is emitted
            daily=[],
            source=RACKSPACE_COST_SOURCE,
        )

    async def get_cost_by_service(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        period_start: date,
        period_end: date,
        region: Optional[str] = None,
    ) -> List[CloudServiceCost]:
        """Return the per-service cost breakdown for the window."""
        summary = await self.get_cost_summary(
            accounts=accounts,
            period_start=period_start,
            period_end=period_end,
            previous_period_start=period_start,
            previous_period_end=period_end,
            region=region,
        )
        return list(summary.by_service)

    # ---- Resources (Phase 9B.2) --------------------------------------------

    async def get_resources(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> List[CloudResource]:
        """Return the canonical Rackspace resource inventory.

        Delegates to :func:`aggregate_rackspace_resources` — the same
        catalog-driven, multi-region, failure-isolated aggregation the
        canonical route uses — and returns only the normalised
        resources.  Raises :class:`RackspaceResourceError` on total
        failure so callers can distinguish a failed inventory from a
        legitimately empty one.  The ``region`` argument is accepted
        for contract parity but not filtered on: Rackspace discovery
        enumerates every authorized region from the Identity catalog.
        """
        _summary, resources, _failures = await aggregate_rackspace_resources(
            rows=accounts,
        )
        return resources

    async def get_resource_summary(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        region: Optional[str] = None,
    ) -> CloudResourceSummary:
        """Return the grouped Rackspace inventory suitable for the UI.

        Empty-success semantics: a tenant with zero servers, volumes
        and load balancers yields ``status=SUCCESS`` with an empty
        resource list — never an error.
        """
        summary, _resources, _failures = await aggregate_rackspace_resources(
            rows=accounts,
        )
        return summary

    async def get_recommendations(
        self,
        *,
        accounts: Sequence[_CloudAccountRow],
        days: int = 90,
        region: Optional[str] = None,
    ) -> List[CloudRecommendation]:
        """Return the canonical Rackspace recommendation list (Phase 9C.2).

        Delegates to
        :func:`app.services.cloud.rackspace.recommendations.aggregate_rackspace_recommendations_for_account`
        — the same canonical aggregation the
        ``/api/cloud/recommendations?provider=RACKSPACE`` route serves —
        and returns only the normalised recommendations (the rollup /
        per-service failures stay internal to the route envelope).

        Read-only and deterministic: the Phase 9C.1 engine never issues
        a Rackspace product write, and unknown savings stay ``None``.
        """
        flat, _rollup, _failures = await aggregate_rackspace_recommendations_for_account(
            rows=accounts, region=region, days=days,
        )
        return flat


# ---------------------------------------------------------------------------
# Account bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_rackspace_account(db: Any) -> int:
    """Idempotently register the single configured Rackspace account.

    The registry row stores ONLY non-secret account metadata:

    * ``account_id`` — the Cloud tenant id (``RACKSPACE_ACCOUNT_ID``)
    * ``display_name`` — ``RACKSPACE_ACCOUNT_DISPLAY_NAME`` or a safe
      default label
    * ``provider_metadata`` — credential strategy + bootstrap source

    The Billing Account Number (RAN) and every credential remain in
    the protected host ``.env`` — NEVER in PostgreSQL.

    Returns the number of NEW rows inserted.  Skips silently (0)
    when the runtime is not fully configured for costs (Identity
    credentials + RAN) so a deployment without Rackspace keeps
    working unchanged.
    """
    if not _costs_configured():
        logger.info(
            "rackspace.bootstrap.skipped reason=not_configured"
        )
        return 0

    from sqlalchemy import select
    from app.db.models import CloudAccount

    account_id = (get_config_status_and_account_id() or "").strip()
    if not account_id:
        return 0
    display_name = (
        os.getenv("RACKSPACE_ACCOUNT_DISPLAY_NAME", "").strip()
        or "Rackspace Cloud"
    )

    try:
        existing = db.execute(
            select(CloudAccount).where(
                CloudAccount.provider == "RACKSPACE",
                CloudAccount.account_id == account_id,
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "rackspace.bootstrap.lookup_failed code=%s",
            type(exc).__name__,
        )
        return 0

    if existing is not None:
        # Idempotent path — keep the existing row untouched.
        return 0

    row = CloudAccount(
        provider="RACKSPACE",
        account_id=account_id,
        display_name=display_name,
        role_arn=None,
        external_id=None,
        tenant_id=account_id,
        subscription_id=None,
        provider_metadata={
            "credential_strategy": (
                CloudCredentialStrategy.RACKSPACE_API_KEY.value
            ),
            "bootstrap_source": "phase9b1",
        },
        enabled=True,
        is_management=False,
        preferred_region=None,
    )
    db.add(row)
    try:
        db.commit()
        logger.info(
            "rackspace.bootstrap.inserted display=%s",
            display_name[:40],
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — defensive
        db.rollback()
        logger.warning(
            "rackspace.bootstrap.insert_failed code=%s",
            type(exc).__name__,
        )
        return 0


def get_config_status_and_account_id() -> Optional[str]:
    """Return the configured Cloud tenant id, or ``None``.

    Reads ``RACKSPACE_ACCOUNT_ID`` from settings without ever logging
    the value.  The tenant id is non-secret (it appears in the
    registry and API responses); the RAN is never read here.
    """

    from app.core.config import get_settings

    try:
        settings = get_settings()
    except Exception:  # pragma: no cover — defensive
        return None
    value = (getattr(settings, "rackspace_account_id", "") or "").strip()
    return value or None


def bootstrap_rackspace_account_once(*, force: bool = False) -> int:
    """Process-wide idempotent account bootstrap.

    Called from ``app.main`` at startup; safe to invoke multiple
    times.  Returns the count of newly-inserted rows.  Derives the
    single configured Rackspace account from runtime configuration —
    operators never insert database rows by hand.
    """
    if not force and getattr(bootstrap_rackspace_account_once, "_done", False):
        return 0
    from app.db.session import SessionLocal

    inserted = 0
    db = None
    try:
        db = SessionLocal()
        inserted = _bootstrap_rackspace_account(db)
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.warning(
            "rackspace.bootstrap.failed code=%s",
            type(exc).__name__,
        )
        inserted = 0
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 — defensive
                pass
    bootstrap_rackspace_account_once._done = True
    return inserted


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register this adapter with the singleton :class:`ProviderRegistry`.

    The adapter registers unconditionally (mirroring the Azure
    pattern): the runtime-config checks inside ``validate_account``
    / ``get_capabilities`` short-circuit to ``NOT_CONFIGURED`` when
    Rackspace is absent, and the canonical route surfaces a precise
    error when the provider is requested without configuration.
    """

    registry = ProviderRegistry.current()
    registry.register(RackspaceCloudProvider())


_register()


__all__ = [
    "RackspaceCloudProvider",
    "bootstrap_rackspace_account_once",
]
