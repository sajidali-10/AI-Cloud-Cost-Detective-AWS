"""AWS Cost Optimization Hub service — Phase 3.

Wraps the Boto3 ``cost-optimization-hub`` client with:

* ``ListEnrollmentStatuses`` — returns the account-level enrollment
  state (ACTIVE / INACTIVE / PENDING / FAILED).
* ``GetPreferences`` — surfaces the account-level preferences (which
  resource types the hub includes in its scope).
* ``ListRecommendations`` — paginates the hub's recommendation list
  with ``NextToken``, returning every recommendation AWS has
  available for the account.
* ``ListRecommendationSummaries`` — high-level counters by resource
  type and action type for the capabilities endpoint.

We deliberately avoid ``GetRecommendation`` — the wire payload from
``ListRecommendations`` already contains the savings, restart, and
rollback flags we need, and pulling detail per-row would turn the
endpoint into an N+1 API storm.  ``GetRecommendation`` is wired in
for completeness but the optimization route does NOT use it.

**No paid features are activated.**  ``UpdatePreferences`` and
``UpdateEnrollmentStatus`` are never called.  The hub may report
itself inactive; in that case the optimization endpoint continues
to function on deterministic rules.

All Boto3 call sites are wrapped by ``assert_read_only``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, List, Optional, Sequence

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.services.aws.guard import assert_read_only

logger = logging.getLogger("cost-detective-backend.cost_optimization_hub")


# ---------------------------------------------------------------------------
# Constants — pinned from the Phase 3 spec.
# ---------------------------------------------------------------------------

ALLOWED_LOOKBACK_DAYS: tuple[int, ...] = (7, 30, 60, 90)

# The hub is account-scoped; we accept an explicit ``account_id`` only
# when the caller has cross-account visibility.  In the Phase 3
# single-account scope, AWS infers the caller's account from the
# credentials, so ``account_id`` is purely a documentation convenience.
HUB_PAGE_SIZE: int = 100  # within AWS' MaxResults envelope


class HubEnrollmentStatus(str, Enum):
    """Maps the hub's ``status`` field to our ``CapabilityStatus`` enum."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING = "PENDING"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class CostOptimizationHubError(RuntimeError):
    """Sanitized wrapper around any Cost Optimization Hub failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _sanitize_boto_error(exc: Exception) -> CostOptimizationHubError:
    """Map a Boto3 exception to a sanitized ``CostOptimizationHubError``."""
    code = "CostOptimizationHubError"
    try:
        response = getattr(exc, "response", None) or {}
        err = response.get("Error") if isinstance(response, dict) else None
        if isinstance(err, dict) and err.get("Code"):
            code = str(err["Code"])
    except Exception:  # pragma: no cover - defensive
        pass
    return CostOptimizationHubError(code=code, message="Cost Optimization Hub request failed")


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_cost_optimization_hub_client(region: Optional[str] = None) -> BaseClient:
    """Return a Cost Optimization Hub client.

    The hub is account-scoped, but accepts a region for parity with
    the other AWS services.  Conservative retries mirror the rest of
    the codebase.
    """
    config = BotoConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )
    kwargs: dict[str, Any] = {"config": config}
    if region:
        kwargs["region_name"] = region
    return boto3.client("cost-optimization-hub", **kwargs)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def _map_status(raw_status: Optional[str]) -> str:
    """Map a hub enrollment status string to our ``CapabilityStatus``."""
    if not raw_status:
        return "INACTIVE"
    normalized = str(raw_status).strip().upper()
    mapping = {
        "ACTIVE": "ACTIVE",
        "INACTIVE": "INACTIVE",
        "PENDING": "PENDING",
        "FAILED": "FAILED",
    }
    return mapping.get(normalized, "UNAVAILABLE")


def list_enrollment_statuses(
    client: BaseClient,
    *,
    account_id: Optional[str] = None,
    include_organization: bool = False,
) -> str:
    """Return the account-level enrollment status.

    When ``account_id`` is provided we pass it through; otherwise AWS
    uses the caller's account.  ``include_organization`` is wired in
    for completeness but defaults to ``False`` to match the single-
    account scope.
    """
    assert_read_only(client, "list_enrollment_statuses")
    params: dict[str, Any] = {}
    if account_id:
        params["accountId"] = account_id
    if include_organization:
        params["includeOrganizationMembers"] = True
    try:
        response = client.list_enrollment_statuses(**params)
    except (ClientError, BotoCoreError) as exc:
        raise _sanitize_boto_error(exc) from None
    items = response.get("items") or response.get("EnrollmentStatuses") or []
    if not items:
        # The hub returns an empty list when the account has never
        # been enrolled.  Surface that as ``INACTIVE`` rather than
        # ``UNAVAILABLE`` so the caller can render the right message.
        return "INACTIVE"
    # Pick the requested account's row, else the first.
    for row in items:
        if account_id is None or row.get("accountId") == account_id:
            return _map_status(row.get("status"))
    return _map_status(items[0].get("status"))


def get_preferences(client: BaseClient, *, account_id: Optional[str] = None) -> dict[str, Any]:
    """Return the hub preferences (member- account scoping rules).

    A failure here is non-fatal — the caller treats an exception as
    "preferences unknown" and continues.
    """
    assert_read_only(client, "get_preferences")
    params: dict[str, Any] = {}
    if account_id:
        params["accountId"] = account_id
    try:
        return dict(client.get_preferences(**params))
    except (ClientError, BotoCoreError) as exc:
        raise _sanitize_boto_error(exc) from None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _paginate(
    *,
    client: BaseClient,
    method_name: str,
    base_kwargs: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    """Yield pages until ``nextToken`` is empty.

    Same idiom as the Cost Explorer and CloudWatch wrappers.  ``MaxResults``
    is intentionally NOT sent when the operation rejects it (the hub
    paginates without a per-page cap).
    """
    next_token: Optional[str] = None
    while True:
        kwargs = {k: v for k, v in base_kwargs.items() if v is not None}
        if next_token:
            kwargs["nextToken"] = next_token
        try:
            response = getattr(client, method_name)(**kwargs)
        except (ClientError, BotoCoreError) as exc:
            raise _sanitize_boto_error(exc) from None
        yield response
        next_token = response.get("nextToken")
        if not next_token:
            return


# ---------------------------------------------------------------------------
# Internal normalized row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedHubRecommendation:
    """A Cost Optimization Hub row after normalization.

    Mirrors :class:`NormalizedRecommendation` from the Compute
    Optimizer module so the orchestrator can merge them with one
    code path.  ``action_type`` is kept as the raw AWS string so the
    deduplicator can key on it deterministically.
    """

    recommendation_id: str
    resource_id: str
    resource_arn: Optional[str]
    resource_type: str  # "EC2" | "EBS_VOLUME" | "LAMBDA_FUNCTION" | "RDS_DB_INSTANCE" | ...
    action_type: str  # e.g. "Rightsize" | "DeleteUnused" | "StopIdle" | "ReleaseEip"
    region: Optional[str]
    account_id: Optional[str]
    estimated_monthly_savings: Optional[Decimal]
    savings_percentage: Optional[Decimal]
    currency: str
    current_resource_summary: dict[str, Any] = field(default_factory=dict)
    recommended_resource_summary: dict[str, Any] = field(default_factory=dict)
    implementation_effort: Optional[str] = None
    restart_needed: Optional[bool] = None
    rollback_possible: Optional[bool] = None
    source: str = "AWS_COST_OPTIMIZATION_HUB"


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # pragma: no cover - defensive
        return None


def _normalize_resource_summary(value: Any) -> dict[str, Any]:
    """Coerce the hub's summary blob to a plain dict.

    The hub returns a JSON-like structure; Boto3 already decodes it
    to a dict, but we defensively copy so the consumer never sees
    the Boto3 internal type.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}


def _normalize_recommendation(row: dict[str, Any]) -> NormalizedHubRecommendation:
    """Map a raw ``ListRecommendations`` row to our internal shape."""
    rec_id = row.get("recommendationId") or ""
    resource_id = row.get("resourceId") or ""
    return NormalizedHubRecommendation(
        recommendation_id=str(rec_id),
        resource_id=str(resource_id),
        resource_arn=row.get("resourceArn"),
        resource_type=str(row.get("resourceType") or ""),
        action_type=str(row.get("actionType") or ""),
        region=row.get("region"),
        account_id=row.get("accountId"),
        estimated_monthly_savings=_decimal_or_none(row.get("estimatedMonthlySavings")),
        savings_percentage=_decimal_or_none(row.get("savingsPercentage")),
        currency=row.get("currencyCode") or "USD",
        current_resource_summary=_normalize_resource_summary(row.get("currentResourceSummary")),
        recommended_resource_summary=_normalize_resource_summary(row.get("recommendedResourceSummary")),
        implementation_effort=row.get("implementationEffort"),
        restart_needed=row.get("restartNeeded"),
        rollback_possible=row.get("rollbackPossible"),
    )


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def list_recommendations(
    client: BaseClient,
    *,
    region: Optional[str] = None,
    account_id: Optional[str] = None,
    resource_types: Optional[Sequence[str]] = None,
    action_types: Optional[Sequence[str]] = None,
    max_pages: Optional[int] = None,
) -> List[NormalizedHubRecommendation]:
    """Return every Cost Optimization Hub recommendation that matches the filters.

    Pagination is transparent: ``MaxResults`` is sent on every call so
    AWS returns smaller pages and we stay well under any pagination
    timeouts.  ``max_pages`` caps the loop as a defensive guard against
    runaway pagination in tests and against runaway API spend in
    production — leave it ``None`` for the unfiltered call path.
    """
    assert_read_only(client, "list_recommendations")
    base_kwargs: dict[str, Any] = {"maxResults": HUB_PAGE_SIZE}
    if region:
        base_kwargs["filter"] = {
            "regions": [region],
            **(
                {"resourceTypes": list(resource_types)}
                if resource_types
                else {}
            ),
            **(
                {"actionTypes": list(action_types)}
                if action_types
                else {}
            ),
        }
    if account_id:
        base_kwargs["accountId"] = account_id
    out: List[NormalizedHubRecommendation] = []
    pages = 0
    for response in _paginate(
        client=client, method_name="list_recommendations", base_kwargs=base_kwargs
    ):
        pages += 1
        items = response.get("items") or response.get("recommendations") or []
        for row in items:
            try:
                out.append(_normalize_recommendation(row))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "cost_optimization_hub: failed to normalize row err=%s",
                    type(exc).__name__,
                )
                continue
        if max_pages is not None and pages >= max_pages:
            break
    return out


def list_recommendation_summaries(
    client: BaseClient,
    *,
    region: Optional[str] = None,
    account_id: Optional[str] = None,
    group_by: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> List[dict[str, Any]]:
    """Return ``ListRecommendationSummaries`` results.

    Each row carries at minimum ``group`` and ``estimatedMonthlySavings``.
    The shape varies depending on ``group_by``; we normalize to
    ``{group: str, count: int, estimated_monthly_savings: Decimal}``
    so the caller can render them uniformly.
    """
    assert_read_only(client, "list_recommendation_summaries")
    base_kwargs: dict[str, Any] = {"maxResults": HUB_PAGE_SIZE}
    if group_by:
        base_kwargs["groupBy"] = group_by
    if region:
        base_kwargs["filter"] = {"regions": [region]}
    if account_id:
        base_kwargs["accountId"] = account_id
    out: List[dict[str, Any]] = []
    pages = 0
    for response in _paginate(
        client=client, method_name="list_recommendation_summaries", base_kwargs=base_kwargs
    ):
        pages += 1
        items = response.get("items") or response.get("summaries") or []
        for row in items:
            out.append(
                {
                    "group": row.get("group") or row.get("key") or "",
                    "count": int(row.get("count") or 0),
                    "estimated_monthly_savings": _decimal_or_none(
                        row.get("estimatedMonthlySavings")
                    ),
                    "currency": row.get("currencyCode") or "USD",
                }
            )
        if max_pages is not None and pages >= max_pages:
            break
    return out


# ---------------------------------------------------------------------------
# Single-recommendation detail (kept for completeness; the route
# intentionally does not call this — see module docstring).
# ---------------------------------------------------------------------------


def get_recommendation(client: BaseClient, *, recommendation_id: str) -> dict[str, Any]:
    """Return a single recommendation's detail.

    Wired in for completeness but unused by the optimization route —
    the list endpoint already carries every field we normalize.
    Calling this per-row would create an N+1 API storm.
    """
    assert_read_only(client, "get_recommendation")
    try:
        response = client.get_recommendation(recommendationId=recommendation_id)
    except (ClientError, BotoCoreError) as exc:
        raise _sanitize_boto_error(exc) from None
    return dict(response)


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "HUB_PAGE_SIZE",
    "CostOptimizationHubError",
    "HubEnrollmentStatus",
    "NormalizedHubRecommendation",
    "get_cost_optimization_hub_client",
    "get_preferences",
    "get_recommendation",
    "list_enrollment_statuses",
    "list_recommendation_summaries",
    "list_recommendations",
]
