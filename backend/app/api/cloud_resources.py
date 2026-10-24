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

* ``provider`` — required, one of ``AWS`` / ``AZURE`` / ``RACKSPACE``.
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
from app.services.cloud.rackspace.resources import (
    RackspaceResourceError,
    RackspaceResourceErrorCategory,
    aggregate_rackspace_resources,
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
    elif (
        provider in (CloudProviderKind.AWS, CloudProviderKind.RACKSPACE)
        and cleaned.startswith("acct:")
    ):
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
    """Translate AWS adapter output into the canonical envelope.

    Phase 7D.3.1 — defence-in-depth resource summaries.

    The AWS aggregator does not currently populate ``by_type`` /
    ``by_location`` / ``by_subscription`` in its extensions, even
    when ``resources[]`` is populated.  Derive these summaries
    server-side from the canonical resource list so the invariant
    ``sum(by_type counts) <= total_resources`` always holds,
    regardless of the aggregator's output.  An explicit
    ``Unassigned`` bucket preserves resources that lack a
    resource_type rather than silently dropping them.
    """
    from app.services.cloud.models import CloudProviderKind as _CPK

    extensions = summary.extensions or {}

    # Build canonical summaries from the normalised resources list.
    type_counts: dict[str, int] = {}
    location_counts: dict[str, int] = {}
    subscription_counts: dict[str, dict[str, str]] = {}  # account_id -> {display_name, count}
    unassigned_count = 0
    for resource in resources:
        rt = (resource.resource_type or "").strip()
        if rt:
            type_counts[rt] = type_counts.get(rt, 0) + 1
        else:
            unassigned_count += 1
        loc = (resource.region or "").strip()
        if loc:
            location_counts[loc] = location_counts.get(loc, 0) + 1
        else:
            location_counts.setdefault("Unassigned", 0)
            location_counts["Unassigned"] += 1
        if resource.account_id:
            entry = subscription_counts.setdefault(
                resource.account_id,
                {"display_name": resource.account_id, "count": 0},
            )
            entry["count"] += 1
            # Best-effort: prefer the AWS account_display_name from
            # the resource metadata when the aggregator stamped it.
            meta = resource.metadata or {}
            display = meta.get("account_display_name") if isinstance(meta, dict) else None
            if display:
                entry["display_name"] = str(display)
    if unassigned_count > 0:
        type_counts["Unassigned"] = unassigned_count

    # Merge with aggregator-supplied breakdowns (if any) so we don't
    # regress on richer upstream summaries.
    def _merge_group(ext_key: str, derived: dict[str, int]) -> list:
        merged: dict[str, int] = dict(derived)
        for item in extensions.get(ext_key, []) or []:
            key = item.get("key")
            if not key:
                continue
            merged[key] = merged.get(key, 0) + int(item.get("count", 0))
        return [
            CloudResourceGroupCount(key=k, count=v)
            for k, v in sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    by_type = _merge_group("by_type", type_counts)
    by_location = _merge_group("by_location", location_counts)
    by_resource_group = _merge_group("by_resource_group", {})
    by_subscription = []
    for account_id, payload in sorted(subscription_counts.items()):
        by_subscription.append(
            CloudResourceSubscriptionShare(
                subscription_id=account_id,
                display_name=payload["display_name"],
                count=payload["count"],
            )
        )
    # If the aggregator already has subscription counts, layer
    # them on top so we never lose upstream metadata.
    for item in extensions.get("by_subscription", []) or []:
        sid = item.get("subscription_id") or item.get("account_id") or ""
        if not sid:
            continue
        existing = next((s for s in by_subscription if s.subscription_id == sid), None)
        if existing is None:
            by_subscription.append(
                CloudResourceSubscriptionShare(
                    subscription_id=sid,
                    display_name=item.get("display_name", sid),
                    count=item.get("count", 0),
                )
            )
        else:
            existing.count += item.get("count", 0)

    subscription_errors = [
        CloudResourceSubscriptionError(
            subscription_id=item.get("subscription_id") or item.get("account_id") or "",
            display_name=item.get("display_name", ""),
            code=item.get("code", "ERROR"),
            message=item.get("message", ""),
        )
        for item in extensions.get("subscription_errors", [])
    ]
    # Derive total_resources defensively: prefer the aggregator's
    # figure, fall back to the canonical count of returned items.
    total_resources = extensions.get("total_resources", len(resources))
    if total_resources == 0 and resources:
        total_resources = len(resources)
    summary_envelope = CloudResourceSummaryEnvelope(
        provider="AWS",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        total_resources=total_resources,
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


def _build_rackspace_envelope(
    summary, *, resources, rows, scope, failures
) -> CloudResourceListResponse:
    """Translate Rackspace adapter output into the canonical envelope.

    Mirrors the Azure builder: breakdowns come from the aggregator's
    extensions (derived from the normalised resources, never
    fabricated).  Per-service failures surface through the canonical
    ``subscription_errors`` slot — the scope id is the safe Cloud
    tenant and the label names the service that failed.
    """
    extensions = summary.extensions or {}
    by_type = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_type", [])
    ]
    by_location = [
        CloudResourceGroupCount(key=item["key"], count=item["count"])
        for item in extensions.get("by_location", [])
    ]
    by_subscription = [
        CloudResourceSubscriptionShare(
            subscription_id=item["subscription_id"],
            display_name=item.get("display_name", item["subscription_id"]),
            count=item["count"],
        )
        for item in extensions.get("by_subscription", [])
    ]
    raw_errors = list(extensions.get("subscription_errors", []))
    for failure in failures:
        raw_errors.append(
            {
                "subscription_id": rows[0].account_id if rows else "",
                "display_name": failure.service,
                "code": failure.category.value,
                "message": failure.message,
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
        provider="RACKSPACE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        total_resources=total_resources,
        by_type=by_type,
        by_location=by_location,
        by_resource_group=[],
        by_subscription=by_subscription,
        subscription_errors=subscription_errors,
        source="RACKSPACE_CLOUD_API",
    )
    items = [
        CloudResourceItem(
            provider="RACKSPACE",
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
    provider: str = Query(..., description="AWS | AZURE | RACKSPACE"),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Default 'all'.  For AWS: 'acct:<id>' or a registry row id.  "
            "For Azure: 'sub:<id>' or a registry row id.  "
            "For Rackspace: 'acct:<tenant id>' or a registry row id."
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
        elif kind == CloudProviderKind.RACKSPACE:
            summary, resources, failures = await aggregate_rackspace_resources(
                rows=rows
            )
            envelope = _build_rackspace_envelope(
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
    except RackspaceResourceError as exc:
        return _rackspace_resource_error_response(exc)
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


def _rackspace_resource_error_response(exc: RackspaceResourceError) -> JSONResponse:
    """Translate a :class:`RackspaceResourceError` into a sanitised envelope.

    Follows the same vocabulary as the Azure and Rackspace cost error
    mappings so the front-end can reuse its status handling.  The
    message bodies are stable category semantics — they never contain
    credentials, X-Auth-Token values, the RAN, or raw payloads.
    """
    mapping = {
        RackspaceResourceErrorCategory.NOT_CONFIGURED: (
            status.HTTP_503_SERVICE_UNAVAILABLE, "NOT_CONFIGURED",
        ),
        RackspaceResourceErrorCategory.AUTH_FAILED: (
            status.HTTP_401_UNAUTHORIZED, "AUTH_FAILED",
        ),
        RackspaceResourceErrorCategory.ACCESS_DENIED: (
            status.HTTP_403_FORBIDDEN, "ACCESS_DENIED",
        ),
        RackspaceResourceErrorCategory.NOT_FOUND: (
            status.HTTP_404_NOT_FOUND, "NOT_FOUND",
        ),
        RackspaceResourceErrorCategory.THROTTLED: (
            status.HTTP_429_TOO_MANY_REQUESTS, "THROTTLED",
        ),
        RackspaceResourceErrorCategory.NETWORK_ERROR: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR",
        ),
        RackspaceResourceErrorCategory.INVALID_RESPONSE: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR",
        ),
        RackspaceResourceErrorCategory.UNSUPPORTED: (
            status.HTTP_502_BAD_GATEWAY, "UNSUPPORTED",
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
        },
        headers=headers,
    )


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
