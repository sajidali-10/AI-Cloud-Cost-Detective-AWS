"""Provider-neutral cloud routes — Phase 7C.

Mounted on the existing :data:`router` from :mod:`app.api.aws` so
the public URL is ``/api/cloud/*`` after Nginx strips ``/api/``.

Endpoints:

* ``GET    /api/cloud/providers``                    — list registered providers + capabilities
* ``GET    /api/cloud/accounts/selectable``          — selector payload across every provider
* ``GET    /api/cloud/accounts/selectable/{provider}`` — selector payload scoped to a provider

Phase 7C.1: AWS is the only provider with rows in the registry
today, but the routes fan out across every registered adapter so
a future Azure / Rackspace adapter will appear here automatically
once it is wired into :class:`ProviderRegistry`.

The existing Phase 7B routes (``/api/cloud-accounts/...``,
``/api/aws/...``) remain untouched and are still the canonical
AWS API surface.  The new ``/api/cloud/...`` routes are the
canonical provider-neutral surface and will be the only ones the
frontend uses once the migration is complete.
"""
from __future__ import annotations

import logging
from typing import Any, List

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from app.api.deps import require_role
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cloud_account import (
    CloudAccountSelectable,
    CloudAccountSelectableListResponse,
)
from app.services.cloud import (
    CloudProviderKind,
    ProviderNotFound,
    ProviderRegistry,
)
from app.services.aws.account_provider import list_accounts

logger = logging.getLogger("cost-detective-backend.cloud_providers")

# A dedicated router with the ``/cloud`` prefix so the public URL
# is ``/api/cloud/*`` (after Nginx strips ``/api/``).  Sharing
# ``aws_router`` would force the routes under ``/api/aws/cloud/*``
# which is not the spec.
router = APIRouter(prefix="/cloud", tags=["cloud"])


# ---------------------------------------------------------------------------
# GET /api/cloud/providers
# ---------------------------------------------------------------------------


@router.get(
    "/providers",
    summary="List every registered cloud provider and its capability surface.",
)
def list_cloud_providers(
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    """Return every registered provider with its capability snapshot.

    Phase 7C.1: AWS is the only provider in the registry.  Future
    adapters (Azure, Rackspace) will appear here automatically once
    they self-register.  The response distinguishes ``is_registered``
    (the adapter is wired) from ``is_enabled`` (the adapter is
    active for this deployment) so an admin can see when an adapter
    has been disabled.
    """
    registry = ProviderRegistry.current()
    out: List[dict] = []
    for kind in sorted(
        registry.known_kinds(), key=lambda k: k.value
    ):
        adapter = registry.try_get(kind)
        enabled = registry.is_registered(kind)
        entry: dict = {
            "provider": kind.value,
            "is_registered": adapter is not None,
            "is_enabled": enabled,
            "credential_strategies": [
                s.value for s in (adapter.credential_strategies if adapter else [])
            ],
        }
        if adapter is None:
            entry["capabilities"] = []
            entry["supported_regions"] = []
            entry["supported_lookback_days"] = []
            entry["status"] = "NOT_REGISTERED"
            entry["details"] = {"reason": "adapter is not registered"}
        else:
            try:
                snapshot = adapter.get_capabilities()
                entry["capabilities"] = snapshot.describe()
                entry["supported_regions"] = [
                    {
                        "region_id": r.region_id,
                        "display_name": r.display_name,
                    }
                    for r in snapshot.supported_regions
                ]
                entry["supported_lookback_days"] = list(
                    snapshot.supported_lookback_days
                )
                # Provider is up if at least one core capability
                # is SUPPORTED; UNSUPPORTED / UNAVAILABLE /
                # NOT_CONFIGURED are all summarised as "available
                # but no live data".  Phase 7C.1 never returns
                # UNAVAILABLE — the AWS adapter is always at
                # least SUPPORTED for the core capabilities.
                has_supported = any(
                    v == "SUPPORTED" for v in [
                        item["state"] for item in entry["capabilities"]
                    ]
                )
                entry["status"] = "OK" if has_supported else "NO_CAPABILITIES"
            except Exception as exc:  # noqa: BLE001 - sanitized
                logger.warning(
                    "cloud.providers.capabilities_failed provider=%s code=%s",
                    kind.value, type(exc).__name__,
                )
                entry["capabilities"] = []
                entry["supported_regions"] = []
                entry["supported_lookback_days"] = []
                entry["status"] = "ERROR"
                entry["details"] = {"error_code": type(exc).__name__}
        out.append(entry)

    # Always surface reserved providers (AZURE / RACKSPACE) as
    # not-yet-implemented so the frontend can show an
    # "available in a future release" hint without polling a
    # second endpoint.
    registered = {entry["provider"] for entry in out}
    for kind in CloudProviderKind:
        if kind.value not in registered:
            out.append(
                {
                    "provider": kind.value,
                    "is_registered": False,
                    "is_enabled": False,
                    "credential_strategies": [],
                    "capabilities": [],
                    "supported_regions": [],
                    "supported_lookback_days": [],
                    "status": "NOT_REGISTERED",
                    "details": {
                        "reason": "provider adapter is not yet wired; "
                        "the platform plans to support it in a future release",
                    },
                }
            )
    out.sort(key=lambda entry: entry["provider"])
    return {"providers": out, "count": len(out)}


# ---------------------------------------------------------------------------
# GET /api/cloud/accounts/selectable
# ---------------------------------------------------------------------------


@router.get(
    "/accounts/selectable",
    response_model=CloudAccountSelectableListResponse,
    summary="Minimal selectable account list across every registered provider.",
)
def list_cloud_accounts_selectable(
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db=Depends(get_db),
) -> Any:
    """Return the selector payload across every registered provider.

    Phase 7C.1: only AWS has rows in the registry, so the
    response is structurally identical to
    ``/api/cloud-accounts/selectable``.  When Azure / Rackspace
    rows appear in future phases they will be appended here
    automatically.

    The ``provider`` field on every item lets the frontend route
    requests through the correct adapter without a second
    lookup.
    """
    registry = ProviderRegistry.current()
    out: List[CloudAccountSelectable] = []
    for kind in sorted(
        registry.available_kinds(), key=lambda k: k.value
    ):
        try:
            rows = list_accounts(db, provider=kind.value, enabled_only=True)
        except Exception as exc:  # noqa: BLE001 - sanitized
            logger.warning(
                "cloud.accounts.list_failed provider=%s code=%s",
                kind.value, type(exc).__name__,
            )
            continue
        for row in rows:
            out.append(
                CloudAccountSelectable(
                    id=row.id,
                    account_id=row.account_id,
                    display_name=row.display_name,
                    provider=row.provider,
                    enabled=row.enabled,
                    is_management=row.is_management,
                )
            )
    # Stable ordering — management first, then by provider + id.
    out.sort(
        key=lambda item: (
            not item.is_management,
            item.provider,
            item.account_id,
        )
    )
    return CloudAccountSelectableListResponse(items=out, count=len(out))


# ---------------------------------------------------------------------------
# GET /api/cloud/accounts/selectable/{provider}
# ---------------------------------------------------------------------------


@router.get(
    "/accounts/selectable/{provider}",
    response_model=CloudAccountSelectableListResponse,
    summary="Minimal selectable account list for a single provider.",
)
def list_cloud_accounts_selectable_for_provider(
    provider: str,
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db=Depends(get_db),
) -> Any:
    """Return the selector payload scoped to a single provider.

    Returns 404 when the provider is not registered (or is
    disabled).  The frontend uses this for the per-provider
    selector surface; the ``/api/cloud/accounts/selectable``
    route above is the canonical "all providers" view.
    """
    try:
        kind = CloudProviderKind(provider.strip().upper())
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "UnknownProvider",
                "message": f"unknown provider {provider!r}",
            },
        )
    registry = ProviderRegistry.current()
    try:
        registry.require(kind)
    except ProviderNotFound:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "ProviderNotRegistered",
                "message": f"provider {kind.value} is not registered",
            },
        )
    rows = list_accounts(db, provider=kind.value, enabled_only=True)
    items = [
        CloudAccountSelectable(
            id=row.id,
            account_id=row.account_id,
            display_name=row.display_name,
            provider=row.provider,
            enabled=row.enabled,
            is_management=row.is_management,
        )
        for row in rows
    ]
    items.sort(key=lambda item: (not item.is_management, item.account_id))
    return CloudAccountSelectableListResponse(items=items, count=len(items))


__all__ = [
    "list_cloud_accounts_selectable",
    "list_cloud_accounts_selectable_for_provider",
    "list_cloud_providers",
]
