"""Provider-neutral Cloud recommendations API — Phase 7C.2D.

Canonical recommendation surface for every registered provider.
Lives on ``/api/cloud/recommendations`` so the front-end can hit
a single endpoint regardless of provider.

Architectural notes
-------------------

* AWS keeps its existing :mod:`app.api.aws_optimization` routes
  (``/api/aws/optimization/*``) for backwards compatibility.  The
  canonical provider-neutral route delegates to the AWS adapter
  for ``provider='AWS'`` so the same normalised shape is
  returned, but every AWS byte-for-byte behaviour is preserved.
* Azure recommendation data is fetched by the Azure adapter,
  which delegates to
  :mod:`app.services.cloud.azure.advisor_aggregator`.  No raw
  Azure payload ever leaks past the adapter layer — every
  recommendation is normalised into the provider-neutral
  :class:`~app.services.cloud.models.CloudRecommendation` shape
  before reaching the route.

Selection semantics
-------------------

* ``provider`` — required, ``"AZURE"`` for this phase.
* ``account_id`` (optional) — ``"all"`` (default), ``"sub:<id>"``
  (Azure), or a bare numeric row id.

Error mapping
-------------

Distinct from the AWS route: Azure Advisor responses are mapped
onto a provider-specific envelope so the front-end can
distinguish:

* ``NO_DATA`` — Advisor returned zero rows AND no subscription
  errors (the union is genuinely empty).
* ``PARTIAL_SUCCESS`` — at least one subscription succeeded,
  another failed.
* ``AUTH_FAILED`` / ``ACCESS_DENIED`` / ``NOT_FOUND`` /
  ``THROTTLED`` / ``UPSTREAM_ERROR`` — mapped to the corresponding
  HTTP status with a sanitised error_code.

The route never echoes the raw Azure payload.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cloud_recommendation import (
    CloudRecommendationCurrencyTotal,
    CloudRecommendationGroupCount,
    CloudRecommendationItem,
    CloudRecommendationListResponse,
    CloudRecommendationSubscriptionError,
    CloudRecommendationSummaryEnvelope,
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
from app.services.cloud.azure.advisor import (
    AzureAdvisorError,
    AzureAdvisorErrorCategory,
)
from app.services.cloud.azure.advisor_aggregator import (
    AdvisorRollup,
    aggregate_advisor_recommendations_for_subscriptions,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.cloud_recommendations")


router = APIRouter(prefix="/cloud", tags=["cloud"])


# ---------------------------------------------------------------------------
# Selection / row resolution
# ---------------------------------------------------------------------------


def _resolve_rows(
    db: Session,
    *,
    provider: CloudProviderKind,
    selection: Optional[str],
) -> tuple[Optional[list], str, Optional[JSONResponse]]:
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
                    "message": f"no {provider.value} accounts are registered",
                },
            )
        return rows, "all", None

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
                "message": f"no {provider.value} account matches selection={selection!r}",
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
# Envelope translation
# ---------------------------------------------------------------------------


def _build_azure_envelope(
    flat_recommendations,
    rollup: AdvisorRollup,
    *,
    rows,
    scope,
    failures,
) -> CloudRecommendationListResponse:
    """Translate Azure Advisor output into the canonical envelope."""
    by_impact = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_impact.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_resource_type = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_resource_type.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_subscription = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_subscription.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_resource_group = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_resource_group.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_type = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_type.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_subcategory = [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(rollup.by_subcategory.items(), key=lambda x: (-x[1], x[0]))
    ]
    raw_errors = list(rollup.failures or [])
    for f in failures or []:
        raw_errors.append(f)
    subscription_errors = [
        CloudRecommendationSubscriptionError(
            subscription_id=item.subscription_id,
            display_name=item.display_name,
            code=item.category,
            message=item.message,
        )
        for item in raw_errors
    ]
    savings_by_currency = [
        CloudRecommendationCurrencyTotal(currency=currency, amount=amount)
        for currency, amount in sorted(rollup.savings_by_currency.items())
    ]
    summary_envelope = CloudRecommendationSummaryEnvelope(
        provider="AZURE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=rollup.status,
        savings_status=rollup.savings_status,
        currency=rollup.currency,
        total_recommendations=rollup.total_recommendations,
        recommendations_with_savings=rollup.recommendations_with_savings,
        recommendations_without_savings=rollup.recommendations_without_savings,
        potential_annual_savings=rollup.potential_annual_savings,
        savings_by_currency=savings_by_currency,
        by_impact=by_impact,
        by_resource_type=by_resource_type,
        by_subscription=by_subscription,
        by_resource_group=by_resource_group,
        by_type=by_type,
        by_subcategory=by_subcategory,
        subscription_errors=subscription_errors,
        source="AZURE_ADVISOR",
    )
    items = [
        CloudRecommendationItem(
            provider="AZURE",
            account_id=rec.account_id,
            region=rec.region,
            recommendation_id=rec.recommendation_id,
            resource_id=rec.resource_id,
            resource_type=rec.resource_type,
            service=rec.service,
            action=rec.action,
            title=rec.title,
            estimated_savings=rec.estimated_savings,
            currency=rec.currency,
            source=rec.source,
            extensions=dict(rec.extensions) if rec.extensions else {},
        )
        for rec in flat_recommendations
    ]
    return CloudRecommendationListResponse(summary=summary_envelope, recommendations=items)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/recommendations",
    summary="Provider-neutral recommendation list for the selected provider and account context.",
)
async def get_cloud_recommendations(
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

    if kind == CloudProviderKind.AZURE:
        try:
            flat, rollup, failures = await aggregate_advisor_recommendations_for_subscriptions(
                rows=rows
            )
        except AzureAdvisorError as exc:
            return _azure_error_response(exc)
        except Exception as exc:  # noqa: BLE001 - sanitised
            logger.warning(
                "cloud.recommendations.upstream_failed provider=%s code=%s",
                kind.value, type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "status": "error",
                    "error_code": type(exc).__name__,
                    "message": "upstream recommendations provider failed",
                },
            )
        envelope = _build_azure_envelope(
            flat, rollup, rows=rows, scope=scope, failures=failures,
        )
        return envelope.model_dump(mode="json")
    # AWS keeps its existing /api/aws/optimization/* routes
    # (Phase 7B).  This canonical Azure route returns 400 for AWS
    # so callers are routed to the AWS-specific surface that
    # carries the Phase 7B RecommendationReport envelope.
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "status": "error",
            "error_code": "UnsupportedProviderForCanonicalRoute",
            "message": (
                "AWS recommendations are served by /api/aws/optimization/*; "
                "this canonical route currently supports provider=AZURE."
            ),
        },
    )


def _azure_error_response(exc: AzureAdvisorError) -> JSONResponse:
    """Translate an :class:`AzureAdvisorError` into a sanitised envelope."""
    mapping = {
        AzureAdvisorErrorCategory.AUTH_FAILED: (
            status.HTTP_401_UNAUTHORIZED, "AUTH_FAILED"
        ),
        AzureAdvisorErrorCategory.ACCESS_DENIED: (
            status.HTTP_403_FORBIDDEN, "ACCESS_DENIED"
        ),
        AzureAdvisorErrorCategory.NOT_FOUND: (
            status.HTTP_404_NOT_FOUND, "NOT_FOUND"
        ),
        AzureAdvisorErrorCategory.THROTTLED: (
            status.HTTP_429_TOO_MANY_REQUESTS, "THROTTLED"
        ),
        AzureAdvisorErrorCategory.UPSTREAM_ERROR: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
        AzureAdvisorErrorCategory.NETWORK_ERROR: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
        AzureAdvisorErrorCategory.INVALID_RESPONSE: (
            status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"
        ),
        AzureAdvisorErrorCategory.PAGINATION_INCOMPLETE: (
            status.HTTP_502_BAD_GATEWAY, "PAGINATION_INCOMPLETE"
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


__all__ = ["get_cloud_recommendations"]
