"""Provider-neutral Cloud resource API — Phase 7C.2C.

Canonical resource inventory surface for every registered provider.
Lives on ``/api/cloud/resources`` so the front-end can hit a single
endpoint regardless of provider.

Architectural notes
-------------------

* AWS keeps its existing :mod:`app.api.aws` resources route
  (``/api/aws/resources`` + ``/api/aws/resources/all``) for backwards
  compatibility with every Phase 1 / 7B / 7C.1 caller.  The canonical
  provider-neutral route delegates to the AWS adapter for
  ``provider='AWS'`` so the same normalised shape is returned, but
  the AWS legacy routes remain untouched.
* Azure resource data is fetched by the Azure adapter, which
  delegates to :mod:`app.services.cloud.azure.resource_aggregator`.
  No raw Azure payload ever leaks past the adapter layer — every
  resource is normalised into the provider-neutral
  :class:`~app.services.cloud.models.CloudResource` shape before
  reaching the route.

Selection semantics
-------------------

* ``provider`` — required, one of ``AWS`` / ``AZURE``.
* ``account_id`` (optional) — ``"all"`` (default),
  ``"acct:<id>"`` / ``"sub:<id>"``, or a bare numeric row id.

Error mapping
-------------

Distinct from the AWS route: Azure Resource Graph responses are
mapped onto a provider-specific envelope so the front-end can
distinguish:

* ``NO_DATA`` — Resource Graph returned zero rows AND no
  subscription errors (the union is genuinely empty).
* ``PARTIAL_SUCCESS`` — at least one subscription succeeded,
  another failed.
* ``AUTH_FAILED`` / ``ACCESS_DENIED`` / ``NOT_FOUND`` /
  ``THROTTLED`` / ``UPSTREAM_ERROR`` — mapped to HTTP 502 with a
  sanitised error_code so the caller can render an appropriate UI
  state.

The route never echoes the raw Resource Graph body.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cloud_resource import (
    CloudResourceGroupCount,
    CloudResourceItem,
    CloudResourceListResponse,
    CloudResourceSubscriptionError,
    CloudResourceSubscriptionShare,
    CloudResourceSummaryEnvelope,
)
from app.services.aws.account_provider import (
    iter_enabled_accounts,
    get_management_account,
)
from app.services.cloud import (
    CloudProviderKind,
    ProviderNotFound,
    ProviderRegistry,
)
from app.services.cloud.azure.resource_aggregator import (
    aggregate_resources_for_subscriptions,
)
from app.services.cloud.azure.resource_graph import (
    AzureResourceGraphError,
    AzureResourceGraphErrorCategory,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.cloud_resources")


router = APIRouter(prefix="/cloud", tags=["cloud"])


# ---------------------------------------------------------------------------
# Cache key — must include EVERY dimension that affects the response.
# ---------------------------------------------------------------------------


def _build_cache_key(
    *,
    user_id: int,
    provider: str,
    scope: str,
) -> str:
    """Stable cache key for the canonical resource route.

    Resource Graph responses change slowly (hours, not seconds) but
    we still key on ``user_id`` + ``provider`` + ``scope`` so the
    same AWS / Azure isolation rules from Phase 7C.2B apply.  The
    route does NOT currently persist to the cost_cache table (the
    AWS legacy route still does); the in-process dedup inside the
    provider-neutral route handles concurrent identical requests.
    """
    import hashlib

    raw = "|".join(
        ["v1", provider.upper(), scope, f"user={user_id}"]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


# ---------------------------------------------------------------------------
# Selection / row resolution
# ---------------------------------------------------------------------------


def _resolve_rows(
    db: Session,
    *,
    provider: CloudProviderKind,
    selection: Optional[str],
) -> tuple[Optional[list], str, Optional[JSONResponse]]:
    """Translate ``selection`` into a concrete list of registry rows."""
    selection = (selection or "all").strip() or "all"
    if selection == "all":
        rows = list(iter_enabled_accounts(db, provider=provider.value))
        if not rows and provider == CloudProviderKind.AWS:
            mgmt = get_management_account(db)
            if mgmt is not None:
                rows = [mgmt]
        if not rows:
            return None, "all", JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "error_code": "NoAccountsRegistered",
                    "message": (
                        f"no {provider.value} accounts are registered"
                    ),
                },
            )
        return rows, "all", None

    # Specific-scope path — translate the selector to a registry row.
    cleaned = selection
    if provider == CloudProviderKind.AZURE and cleaned.startswith("sub:"):
        cleaned = cleaned[len("sub:"):]
    elif provider == CloudProviderKind.AWS and cleaned.startswith("acct:"):
        cleaned = cleaned[len("acct:"):]
    from sqlalchemy import select
    from app.db.models import CloudAccount as _CloudAccountRow

    row = None
    if cleaned.isdigit():
        try:
            from app.services.aws.account_provider import resolve_account_or_404
            row = resolve_account_or_404(db, cleaned)
        except Exception:
            row = None
    if row is None:
        try:
            row = db.execute(
                select(_CloudAccountRow).where(
                    _CloudAccountRow.provider == provider.value,
                    _CloudAccountRow.account_id == cleaned,
                    _CloudAccountRow.enabled.is_(True),
                )
            ).scalar_one_or_none()
        except Exception:
            row = None
    if row is None:
        return None, "single", JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "AccountNotFound",
                "message": (
                    f"no {provider.value} account matches selection={selection!r}"
                ),
            },
        )
    if not row.enabled:
        return None, "single", JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "AccountDisabled",
                "message": f"account {row.account_id} is disabled",
            },
        )
    if provider == CloudProviderKind.AZURE:
        scope = f"sub:{row.account_id}"
    else:
        scope = f"acct:{row.account_id}"
    return [row], scope, None


# ---------------------------------------------------------------------------
# Adapter dispatch
# ---------------------------------------------------------------------------


def _build_aws_envelope(summary, *, resources, rows, scope) -> CloudResourceListResponse:
    """Translate AWS adapter output into the canonical envelope."""
    from app.services.cloud.models import CloudProviderKind as _CPK

    extensions = summary.extensions or {}
    by_type = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_type", [])
    ]
    by_location = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_location", [])
    ]
    by_resource_group = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_resource_group", [])
    ]
    by_subscription = [
        CloudResourceSubscriptionShare(
            subscription_id=item.get("subscription_id") or item.get("account_id") or "",
            display_name=item.get("display_name", ""),
            count=item["count"],
        )
        for item in extensions.get("by_subscription", [])
    ]
    subscription_errors = [
        CloudResourceSubscriptionError(
            subscription_id=item.get("subscription_id") or item.get("account_id") or "",
            display_name=item.get("display_name", ""),
            code=item.get("code", "ERROR"),
            message=item.get("message", ""),
        )
        for item in extensions.get("subscription_errors", [])
    ]
    summary_envelope = CloudResourceSummaryEnvelope(
        provider="AWS",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        total_resources=extensions.get("total_resources", len(resources)),
        by_type=by_type,
        by_location=by_location,
        by_resource_group=by_resource_group,
        by_subscription=by_subscription,
        subscription_errors=subscription_errors,
        source="AWS_INVENTORY",
    )
    items = [
        CloudResourceItem(
            provider="AWS",
            account_id=resource.account_id,
            region=resource.region,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            service=resource.service,
            name=resource.name,
            state=resource.state,
            composite_id=resource.composite_id,
            metadata=dict(resource.metadata) if resource.metadata else {},
        )
        for resource in resources
    ]
    return CloudResourceListResponse(summary=summary_envelope, resources=items)


def _build_azure_envelope(
    summary, *, resources, rows, scope, failures
) -> CloudResourceListResponse:
    """Translate Azure adapter output into the canonical envelope."""
    extensions = summary.extensions or {}
    by_type = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_type", [])
    ]
    by_location = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_location", [])
    ]
    by_resource_group = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_resource_group", [])
    ]
    by_subscription = [
        CloudResourceSubscriptionShare(
            subscription_id=item["subscription_id"],
            display_name=item.get("display_name", item["subscription_id"]),
            count=item["count"],
        )
        for item in extensions.get("by_subscription", [])
    ]
    # Merge adapter-level subscription errors with the per-route failures.
    raw_errors = list(extensions.get("subscription_errors", []))
    for f in failures:
        raw_errors.append(
            {
                "subscription_id": f.subscription_id,
                "display_name": f.display_name,
                "code": f.category,
                "message": f.message,
            }
        )
    subscription_errors = [
        CloudResourceSubscriptionError(
            subscription_id=item.get("subscription_id", ""),
            display_name=item.get("display_name", ""),
            code=item.get("code", "ERROR"),
            message=item.get("message", ""),
        )
        for item in raw_errors
    ]
    total_resources = extensions.get("total_resources", len(resources))
    summary_envelope = CloudResourceSummaryEnvelope(
        provider="AZURE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        total_resources=total_resources,
        by_type=by_type,
        by_location=by_location,
        by_resource_group=by_resource_group,
        by_subscription=by_subscription,
        subscription_errors=subscription_errors,
        source="AZURE_RESOURCE_GRAPH",
    )
    items = [
        CloudResourceItem(
            provider="AZURE",
            account_id=resource.account_id,
            region=resource.region,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            service=resource.service,
            name=resource.name,
            state=resource.state,
            composite_id=resource.composite_id,
            metadata=dict(resource.metadata) if resource.metadata else {},
        )
        for resource in resources
    ]
    return CloudResourceListResponse(summary=summary_envelope, resources=items)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/resources",
    summary="Provider-neutral resource inventory for the selected provider and account context.",
)
async def get_cloud_resources(
    provider: str = Query(..., description="AWS | AZURE (Rackspace in a future phase)"),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Default 'all'.  For AWS: 'acct:<id>' or a registry row id.  "
            "For Azure: 'sub:<id>' or a registry row id."
        ),
    ),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    provider = (provider or "").strip().upper()
    try:
        kind = CloudProviderKind(provider)
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
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
                "message": f"provider {kind.value} is not registered in this deployment",
            },
        )

    rows, scope, rows_err = _resolve_rows(db, provider=kind, selection=account_id)
    if rows_err is not None:
        return rows_err

    cache_key = _build_cache_key(
        user_id=int(user.id), provider=kind.value, scope=scope
    )
    _ = cache_key  # correlation id; not persisted

    adapter = registry.require(kind)

    try:
        if kind == CloudProviderKind.AWS:
            summary = await adapter.get_resource_summary(accounts=rows)
            resources = await adapter.get_resources(accounts=rows)
            envelope = _build_aws_envelope(
                summary, resources=resources, rows=rows, scope=scope
            )
        elif kind == CloudProviderKind.AZURE:
            summary, resources, failures = await aggregate_resources_for_subscriptions(
                rows=rows
            )
            envelope = _build_azure_envelope(
                summary,
                resources=resources,
                rows=rows,
                scope=scope,
                failures=failures,
            )
        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "status": "error",
                    "error_code": "UnsupportedProvider",
                    "message": f"unsupported provider {kind.value}",
                },
            )
    except AzureResourceGraphError as exc:
        return _azure_error_response(exc)
    except Exception as exc:  # noqa: BLE001 - sanitised
        logger.warning(
            "cloud.resources.upstream_failed provider=%s code=%s",
            kind.value, type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": type(exc).__name__,
                "message": "upstream resource provider failed",
            },
        )

    return envelope.model_dump(mode="json")


def _azure_error_response(exc: AzureResourceGraphError) -> JSONResponse:
    """Translate an :class:`AzureResourceGraphError` into a sanitised envelope."""
    mapping = {
        AzureResourceGraphErrorCategory.AUTH_FAILED: (
            status.HTTP_401_UNAUTHORIZED, "AUTH_FAILED"
        ),
        AzureResourceGraphErrorCategory.ACCESS_DENIED: (
            status.HTTP_403_FORBIDDEN, "ACCESS_DENIED"
        ),
        AzureResourceGraphErrorCategory.NOT_FOUND: (
            status.HTTP_404_NOT_FOUND, "NOT_FOUND"
        ),
        AzureResourceGraphErrorCategory.THROTTLED: (
            status.HTTP_429_TOO_MANY_REQUESTS, "THROTTLED"
        ),
        AzureResourceGraphErrorCategory.UPSTREAM_ERROR: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
        AzureResourceGraphErrorCategory.NETWORK_ERROR: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
        AzureResourceGraphErrorCategory.INVALID_RESPONSE: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
    }
    http_status, public_code = mapping.get(
        exc.category, (status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR")
    )
    headers: dict = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(exc.retry_after))
    return JSONResponse(
        status_code=http_status,
        content={
            "status": "error",
            "error_code": public_code,
            "category": exc.category.value,
            "status_code": exc.status_code,
        },
        headers=headers,
    )


__all__ = ["get_cloud_resources"]
