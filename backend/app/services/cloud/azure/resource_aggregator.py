"""Azure resource aggregator — Phase 7C.2C.

Aggregates Microsoft Resource Graph responses for one or more Azure
subscriptions into the provider-neutral
:class:`~app.services.cloud.models.CloudResource` /
:class:`~app.services.cloud.models.CloudResourceSummary` shapes.

Public surface
--------------

* :func:`aggregate_resources_for_subscriptions` — produces a
  normalised resource list + summary across one or more subscriptions.
* :func:`build_resource_summary` — pure rollup helper that folds a
  list of normalised resources into the summary shape.

Failure isolation
-----------------

Each subscription is queried independently.  An error from one
subscription is captured as a sanitised
:class:`AzureResourceGraphSubscriptionFailure` and **does not** fail
the overall response.  Per-subscription attribution is preserved.

Status semantics
----------------

* ``SUCCESS`` — at least one subscription returned resources or a
  zero-resource empty result (zero resources is NOT an error).
* ``NO_DATA`` — every subscription returned no rows AND there were
  no failures (the union is genuinely empty).
* ``PARTIAL_SUCCESS`` — at least one subscription succeeded, at
  least one failed.

NO_DATA / EMPTY handling
------------------------

A subscription with zero resources is **not** a NO_DATA outcome —
NO_DATA is reserved for "we couldn't reach the upstream" or "the
upstream returned 204 / no rows anywhere".  A genuine zero-resource
result is reported as ``status="SUCCESS"`` with an empty resource
list, so the UI can render an empty-state gracefully without
showing an error banner.

Currency safety
---------------

Resources do not carry monetary fields, so the cost-related
currency guard from Phase 7C.2B does not apply.  We still defensively
aggregate `location` / `type` / `resourceGroup` keys without any
implicit normalisation.

Bounded concurrency
-------------------

The aggregator uses an :class:`asyncio.Semaphore` of size
``AZURE_RESOURCE_MAX_CONCURRENCY`` (env-driven, default ``4``) so a
slow subscription cannot serialise the rest of the fan-out.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount
from app.services.cloud.azure.resource_graph import (
    AzureResourceGraphClient,
    AzureResourceGraphError,
)
from app.services.cloud.models import (
    CloudProviderKind,
    CloudRegion,
    CloudResource,
    CloudResourceSummary,
)

logger = logging.getLogger("cost-detective.azure.resource_aggregator")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


def _max_concurrency() -> int:
    return int(os.getenv("AZURE_RESOURCE_MAX_CONCURRENCY", "4"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureResourceGraphSubscriptionFailure:
    """Sanitised per-subscription failure surfaced to the API layer."""

    subscription_id: str
    display_name: str
    category: str
    status_code: Optional[int]
    message: str


@dataclass(frozen=True)
class AzureSubscriptionResourceSnapshot:
    """A single subscription's contribution to the resource rollup."""

    subscription_id: str
    display_name: str
    status: str  # SUCCESS | NO_DATA
    resources: Tuple[CloudResource, ...]
    error: Optional[AzureResourceGraphSubscriptionFailure] = None


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def _coerce_tags(value: Any) -> Mapping[str, str]:
    """Coerce a Resource Graph ``tags`` value to a flat ``str → str`` map.

    Resource Graph returns tags as a JSON object whose values may be
    strings, numbers, booleans, or nested structures (e.g.
    ``{"CreatedBy": "alice", "CostCenter": 42}``).  The provider-
    neutral model accepts only ``str → str``; we stringify non-string
    values defensively rather than dropping the tag.
    """
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, str] = {}
    for key, raw in value.items():
        if raw is None:
            continue
        if isinstance(raw, str):
            out[str(key)] = raw
        else:
            try:
                out[str(key)] = str(raw)
            except Exception:  # noqa: BLE001 - defensive
                continue
    return out


def _coerce_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _subscription_label_from_rows(
    rows: Sequence[Mapping[str, Any]],
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Best-effort subscription display name.

    Resource Graph does not return a subscription display name; the
    aggregator already knows the display name via the registry row.
    This helper exists so the per-resource ``account_display_name``
    field can fall back to the registry display name when the
    upstream does not surface it.
    """
    return fallback


def normalize_row(
    row: Mapping[str, Any],
    *,
    subscription_display_name: Optional[str] = None,
) -> CloudResource:
    """Translate a single Resource Graph row into a CloudResource.

    The KQL query projects the columns the application needs:
    ``id, name, type, location, resourceGroup, subscriptionId, tags``.
    Anything else is surfaced via ``metadata`` (with the original
    Azure properties filtered out — we never echo raw Azure
    metadata into the shared model).
    """
    resource_id = _coerce_string(row.get("id")) or ""
    # The Azure ARM id format is
    # ``/subscriptions/<sub>/resourceGroups/<rg>/providers/<type>/<name>``.
    # Resource Graph may return ``id`` with a missing prefix when
    # the row is malformed; we keep the raw value so the composite
    # identity stays unique.
    composite_id = f"AZURE|{resource_id}"
    name = _coerce_string(row.get("name"))
    resource_type = _coerce_string(row.get("type"))
    location = _coerce_string(row.get("location"))
    resource_group = _coerce_string(row.get("resourceGroup"))
    subscription_id = _coerce_string(row.get("subscriptionId"))
    tags = _coerce_tags(row.get("tags"))

    metadata: Dict[str, Any] = {"resource_group": resource_group} if resource_group else {}
    if tags:
        metadata["tags"] = dict(tags)

    return CloudResource(
        provider=CloudProviderKind.AZURE,
        account_id=subscription_id or "",
        region=location,
        resource_type=resource_type or "unknown",
        resource_id=resource_id,
        service=resource_type or "unknown",
        name=name,
        state=None,
        composite_id=composite_id,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Per-subscription fetch
# ---------------------------------------------------------------------------


def _sanitize_error(
    exc: AzureResourceGraphError,
    *,
    subscription_id: str,
    display_name: str,
) -> AzureResourceGraphSubscriptionFailure:
    return AzureResourceGraphSubscriptionFailure(
        subscription_id=subscription_id,
        display_name=display_name,
        category=exc.category.value,
        status_code=exc.status_code,
        message=exc.category.value,  # sanitised category only
    )


async def _fetch_subscription_resources(
    row: CloudAccount,
) -> AzureSubscriptionResourceSnapshot:
    """Fetch + normalise resources for a single subscription.

    Wraps the REST call in a single try/except so the aggregator's
    fan-out is failure-isolated.
    """
    client = AzureResourceGraphClient()
    try:
        result = client.query(subscription_ids=[row.account_id])
    except AzureResourceGraphError as exc:
        return AzureSubscriptionResourceSnapshot(
            subscription_id=row.account_id,
            display_name=row.display_name,
            status="ERROR",
            resources=(),
            error=_sanitize_error(
                exc,
                subscription_id=row.account_id,
                display_name=row.display_name,
            ),
        )
    finally:
        client.close()

    resources = tuple(
        normalize_row(
            r,
            subscription_display_name=row.display_name,
        )
        for r in result.rows
        if isinstance(r, Mapping)
    )
    status = "NO_DATA" if not resources else "SUCCESS"
    return AzureSubscriptionResourceSnapshot(
        subscription_id=row.account_id,
        display_name=row.display_name,
        status=status,
        resources=resources,
    )


# ---------------------------------------------------------------------------
# Cross-subscription fetch (management-group / tenant scope)
# ---------------------------------------------------------------------------


async def _fetch_tenant_resources(
    rows: Sequence[CloudAccount],
) -> List[AzureSubscriptionResourceSnapshot]:
    """Fetch resources tenant-wide and partition them per subscription.

    Resource Graph's tenant-mode returns every row the configured
    identity is authorised to see, regardless of subscription.  We
    issue ONE query and then partition the rows by ``subscriptionId``
    so the response carries per-subscription attribution without
    paying the per-subscription round-trip cost.
    """
    client = AzureResourceGraphClient()
    try:
        result = client.query(subscription_ids=[])
    except AzureResourceGraphError as exc:
        # Surface a single error against the first row; the per-row
        # walk below stays empty so the caller sees the failure.
        label = ",".join(
            row.account_id for row in rows
        ) or "<tenant>"
        logger.warning(
            "azure.resource_aggregator.tenant_query_failed subs=%s code=%s",
            label, exc.category.value,
        )
        first = rows[0] if rows else None
        return [
            AzureSubscriptionResourceSnapshot(
                subscription_id=first.account_id if first else "<tenant>",
                display_name=first.display_name if first else "Azure",
                status="ERROR",
                resources=(),
                error=_sanitize_error(
                    exc,
                    subscription_id=first.account_id if first else "",
                    display_name=first.display_name if first else "Azure",
                ),
            )
        ]
    finally:
        client.close()

    by_subscription: Dict[str, List[CloudResource]] = OrderedDict()
    for r in result.rows:
        if not isinstance(r, Mapping):
            continue
        subscription_id = _coerce_string(r.get("subscriptionId")) or "<unknown>"
        by_subscription.setdefault(subscription_id, []).append(
            normalize_row(r)
        )
    snapshots: List[AzureSubscriptionResourceSnapshot] = []
    # Build snapshots in the order of the registry rows so the
    # caller sees a deterministic response.
    for row in rows:
        bucket = by_subscription.get(row.account_id, [])
        status = "NO_DATA" if not bucket else "SUCCESS"
        snapshots.append(
            AzureSubscriptionResourceSnapshot(
                subscription_id=row.account_id,
                display_name=row.display_name,
                status=status,
                resources=tuple(bucket),
            )
        )
    # Any subscription that Resource Graph returned but the registry
    # does not know about gets a "phantom" snapshot so the
    # attribution stays accurate.
    for subscription_id, bucket in by_subscription.items():
        if any(s.subscription_id == subscription_id for s in snapshots):
            continue
        snapshots.append(
            AzureSubscriptionResourceSnapshot(
                subscription_id=subscription_id,
                display_name=subscription_id,
                status="SUCCESS",
                resources=tuple(bucket),
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Per-subscription fan-out (explicit scope)
# ---------------------------------------------------------------------------


async def fetch_subscription_resource_snapshots(
    *,
    rows: Sequence[CloudAccount],
) -> List[AzureSubscriptionResourceSnapshot]:
    """Fan out Resource Graph across ``rows`` with bounded concurrency.

    When ``rows`` is empty the function returns ``[]`` without
    issuing any upstream call.  Each per-subscription call has its
    own client lifecycle so a slow subscription cannot starve
    concurrency.
    """
    if not rows:
        return []

    sem = asyncio.Semaphore(_max_concurrency())

    async def _bounded(row: CloudAccount) -> AzureSubscriptionResourceSnapshot:
        async with sem:
            return await _fetch_subscription_resources(row)

    return await asyncio.gather(*(_bounded(row) for row in rows))


# ---------------------------------------------------------------------------
# Rollup — summary
# ---------------------------------------------------------------------------


def _empty_summary(
    *,
    rows: Sequence[CloudAccount],
    status: str,
    failures: Sequence[AzureResourceGraphSubscriptionFailure] = (),
) -> CloudResourceSummary:
    return CloudResourceSummary(
        provider=CloudProviderKind.AZURE,
        account_id=rows[0].account_id if len(rows) == 1 else None,
        scope="all" if len(rows) != 1 else f"sub:{rows[0].account_id}",
        region=None,
        services={},
        totals_by_account=[],
        extensions={
            "status": status,
            "by_type": [],
            "by_location": [],
            "by_resource_group": [],
            "by_subscription": [],
            "subscription_errors": [
                {
                    "subscription_id": err.subscription_id,
                    "display_name": err.display_name,
                    "code": err.category,
                    "message": err.message,
                }
                for err in failures
            ],
        },
    )


def build_resource_summary(
    snapshots: Sequence[AzureSubscriptionResourceSnapshot],
    *,
    rows: Sequence[CloudAccount],
) -> CloudResourceSummary:
    """Combine per-subscription snapshots into a CloudResourceSummary.

    Counts and breakdowns are derived from the **normalised**
    resources, not from raw Azure rows, so the shape is provider-
    neutral and free of Azure-specific leakage.
    """
    failures = [snap.error for snap in snapshots if snap.error is not None]
    successful_snapshots = [snap for snap in snapshots if snap.error is None]
    # A snapshot counts as "empty" when the upstream call succeeded
    # but returned no rows (genuine zero-resource subscription).  We
    # distinguish this from a failure so the rollup can surface a
    # NO_DATA outcome when every accessible subscription is empty.
    successful_empty_snapshots = [
        snap for snap in successful_snapshots if not snap.resources
    ]
    successful_with_data_snapshots = [
        snap for snap in successful_snapshots if snap.resources
    ]

    # Status rollup — NO_DATA is reserved for the "every accessible
    # subscription is empty AND nothing failed" outcome.  A mixed
    # scenario (some data, some empty) is SUCCESS because the
    # upstream calls succeeded.
    if (
        not successful_with_data_snapshots
        and not failures
        and successful_empty_snapshots
    ):
        status = "NO_DATA"
    elif failures and successful_with_data_snapshots:
        status = "PARTIAL_SUCCESS"
    elif failures and not successful_with_data_snapshots:
        status = "PARTIAL_SUCCESS" if successful_empty_snapshots else "ERROR"
    elif successful_with_data_snapshots:
        status = "SUCCESS"
    else:
        status = "NO_DATA"

    # Aggregate resources — dedupe by ``composite_id`` so the
    # all-subscription tenant-mode query (which partitions every
    # row into a per-subscription snapshot) never produces
    # duplicates when the same row happens to be tagged with
    # multiple subscriptions in the upstream payload.
    seen_composite: set = set()
    all_resources: List[CloudResource] = []
    for snap in successful_snapshots:
        for resource in snap.resources:
            if resource.composite_id in seen_composite:
                continue
            seen_composite.add(resource.composite_id)
            all_resources.append(resource)

    # By type — stable order, sorted by count descending.
    by_type_counts: Dict[str, int] = {}
    for r in all_resources:
        by_type_counts[r.resource_type] = by_type_counts.get(r.resource_type, 0) + 1
    by_type = sorted(
        ({"key": k, "count": v} for k, v in by_type_counts.items()),
        key=lambda item: (-item["count"], item["key"]),
    )

    # By location
    by_location_counts: Dict[str, int] = {}
    for r in all_resources:
        if r.region:
            by_location_counts[r.region] = by_location_counts.get(r.region, 0) + 1
    by_location = sorted(
        ({"key": k, "count": v} for k, v in by_location_counts.items()),
        key=lambda item: (-item["count"], item["key"]),
    )

    # By resource group
    by_rg_counts: Dict[str, int] = {}
    for r in all_resources:
        rg = (r.metadata or {}).get("resource_group") if r.metadata else None
        if rg:
            by_rg_counts[rg] = by_rg_counts.get(rg, 0) + 1
    by_resource_group = sorted(
        ({"key": k, "count": v} for k, v in by_rg_counts.items()),
        key=lambda item: (-item["count"], item["key"]),
    )

    # By subscription
    by_sub_counts: Dict[str, int] = {}
    sub_display_names: Dict[str, str] = {}
    for snap in successful_snapshots:
        by_sub_counts[snap.subscription_id] = by_sub_counts.get(
            snap.subscription_id, 0
        ) + len(snap.resources)
        sub_display_names[snap.subscription_id] = snap.display_name
    by_subscription = sorted(
        (
            {
                "subscription_id": k,
                "display_name": sub_display_names.get(k, k),
                "count": v,
            }
            for k, v in by_sub_counts.items()
        ),
        key=lambda item: (-item["count"], item["subscription_id"]),
    )

    # Build the provider-neutral ``services`` map.  Azure doesn't
    # carry the same "ok/denied/error" status shape AWS does, so we
    # emit one bucket per resource type with status=ok when rows
    # exist and status=denied / error when the row-level failure
    # surfaced.
    services: Dict[str, Dict[str, Any]] = {}
    for entry in by_type:
        key = entry["key"]
        services[key] = {
            "service": key,
            "status": "ok" if entry["count"] > 0 else "empty",
            "items": [
                r for r in all_resources if r.resource_type == key
            ],
            "error_code": None,
        }

    totals_by_account = [
        {
            "subscription_id": snap.subscription_id,
            "display_name": snap.display_name,
            "count": len(snap.resources),
        }
        for snap in successful_snapshots
    ]

    # Single-subscription scope uses ``sub:<id>``; multi uses ``all``.
    if len(rows) == 1:
        scope = f"sub:{rows[0].account_id}"
        account_id = rows[0].account_id
    else:
        scope = "all"
        account_id = None

    return CloudResourceSummary(
        provider=CloudProviderKind.AZURE,
        account_id=account_id,
        scope=scope,
        region=None,
        services=services,
        totals_by_account=totals_by_account,
        extensions={
            "status": status,
            "total_resources": len(all_resources),
            "by_type": by_type,
            "by_location": by_location,
            "by_resource_group": by_resource_group,
            "by_subscription": by_subscription,
            "subscription_errors": [
                {
                    "subscription_id": err.subscription_id,
                    "display_name": err.display_name,
                    "code": err.category,
                    "message": err.message,
                }
                for err in failures
            ],
        },
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def aggregate_resources_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
) -> Tuple[
    CloudResourceSummary,
    List[CloudResource],
    List[AzureResourceGraphSubscriptionFailure],
]:
    """Aggregate Resource Graph data for ``rows``.

    When ``rows`` has more than one entry we issue **one** tenant-
    scoped query and partition the rows by ``subscriptionId``.  When
    ``rows`` has exactly one entry we issue a per-subscription query
    so the response is scoped to that subscription and the
    per-subscription cost matches the per-subscription cost for the
    other endpoints.

    Returns ``(summary, flat_resources, failures)`` where
    ``flat_resources`` is the deduplicated list of every resource
    across every successful subscription.
    """
    if not rows:
        return _empty_summary(rows=rows, status="NO_DATA"), [], []

    if len(rows) == 1:
        snapshots = await fetch_subscription_resource_snapshots(rows=rows)
    else:
        snapshots = await _fetch_tenant_resources(rows)
    failures = [snap.error for snap in snapshots if snap.error is not None]
    summary = build_resource_summary(snapshots, rows=rows)
    flat_resources: List[CloudResource] = []
    seen_composite: set = set()
    for snap in snapshots:
        if snap.error is not None:
            continue
        for resource in snap.resources:
            if resource.composite_id in seen_composite:
                continue
            seen_composite.add(resource.composite_id)
            flat_resources.append(resource)
    return summary, flat_resources, failures


__all__ = [
    "AzureResourceGraphSubscriptionFailure",
    "AzureSubscriptionResourceSnapshot",
    "aggregate_resources_for_subscriptions",
    "build_resource_summary",
    "fetch_subscription_resource_snapshots",
    "normalize_row",
]
