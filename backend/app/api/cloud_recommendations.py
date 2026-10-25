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
    CloudRecommendationAccountError,
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
from app.services.cloud.aws.recommendation_aggregator import (
    AwsRecommendationRollup,
    aggregate_aws_recommendations_for_accounts,
)
from app.services.cloud.rackspace.recommendations import (
    RACKSPACE_RECOMMENDATION_SOURCE,
    RackspaceRecommendationRollup,
    aggregate_rackspace_recommendations_for_account,
)
from app.services.cloud.rackspace.resources import (
    RackspaceResourceError,
    RackspaceResourceErrorCategory,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.cloud_recommendations")


router = APIRouter(prefix="/cloud", tags=["cloud"])


# ---------------------------------------------------------------------------
# Cache key — must include EVERY dimension that affects the response.
# Phase 7D.1 adds an AWS/Azure-isolated key so recommendation caches
# for the two providers (and All-AWS vs All-Azure) can never collide.
# ---------------------------------------------------------------------------


def _build_recommendation_cache_key(
    *,
    user_id: int,
    provider: str,
    scope: str,
    region: Optional[str],
    days: int,
) -> str:
    """Stable cache key for the canonical recommendation route.

    Mirrors the isolation rules from :mod:`app.api.cloud_costs` and
    :mod:`app.api.cloud_resources`: every dimension that can change
    the response is folded into the key so an AWS request can never
    satisfy an Azure request, and ``All AWS Accounts`` can never
    satisfy ``All Azure Subscriptions``.  The route does not persist
    to the ``cost_cache`` table; the key is used as a stable
    correlation id and is asserted directly by the cache-isolation
    regression tests.
    """
    import hashlib

    raw = "|".join(
        [
            "v1",
            provider.upper(),
            scope,
            f"user={user_id}",
            region or "all",
            f"days={days}",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


# The selector prefixes that are only meaningful to one provider.  A
# selector minted for one provider must never be interpreted by the
# other (an Azure ``sub:<guid>`` must never reach AWS, and an AWS
# ``acct:<id>`` must never reach Azure).
_SELECTOR_PREFIX_BY_PROVIDER = {
    CloudProviderKind.AWS: "acct:",
    CloudProviderKind.AZURE: "sub:",
    CloudProviderKind.RACKSPACE: "acct:",
}


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

    # Phase 7D.1 — provider-specific selector coercion.  Reject a
    # selector minted for the *other* provider explicitly so it is
    # never misinterpreted as a provider-native id.  Rackspace
    # scopes accounts the AWS way (``acct:<tenant id>``) so both
    # kinds reject Azure ``sub:`` selectors.
    other_prefix = (
        "sub:"
        if provider in (CloudProviderKind.AWS, CloudProviderKind.RACKSPACE)
        else "acct:"
    )
    if selection.startswith(other_prefix):
        return None, "invalid", JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "ProviderSelectorMismatch",
                "message": (
                    f"selector {selection!r} is not valid for "
                    f"provider={provider.value}; expected "
                    f"{_SELECTOR_PREFIX_BY_PROVIDER.get(provider, provider.value.lower() + ':')!r}"
                ),
            },
        )

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
    # Phase 7D.1 — provider-neutral aliases so AWS and Azure expose
    # the same generic fields (subscription -> account, type ->
    # action, subscription_errors -> account_errors).  The legacy
    # fields above are retained for backward compatibility.
    account_errors = [
        CloudRecommendationAccountError(
            account_id=item.subscription_id,
            display_name=item.display_name,
            code=item.category,
            message=item.message,
        )
        for item in raw_errors
    ]
    by_account = list(by_subscription)
    by_action = list(by_type)
    by_source = (
        [
            CloudRecommendationGroupCount(
                key="AZURE_ADVISOR", count=rollup.total_recommendations
            )
        ]
        if rollup.total_recommendations
        else []
    )
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
        potential_savings=rollup.potential_annual_savings,
        savings_period="ANNUAL",
        savings_by_currency=savings_by_currency,
        by_impact=by_impact,
        by_resource_type=by_resource_type,
        by_subscription=by_subscription,
        by_resource_group=by_resource_group,
        by_type=by_type,
        by_subcategory=by_subcategory,
        subscription_errors=subscription_errors,
        by_account=by_account,
        by_source=by_source,
        by_action=by_action,
        account_errors=account_errors,
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
            category=rec.category,
            impact=rec.impact,
            description=rec.description,
            estimated_savings=rec.estimated_savings,
            currency=rec.currency,
            source=rec.source,
            extensions=dict(rec.extensions) if rec.extensions else {},
        )
        for rec in flat_recommendations
    ]
    return CloudRecommendationListResponse(summary=summary_envelope, recommendations=items)


def _group_counts(mapping) -> list:
    """Sort a ``{key: count}`` mapping into a stable group-count list."""
    return [
        CloudRecommendationGroupCount(key=k, count=v)
        for k, v in sorted(mapping.items(), key=lambda x: (-x[1], x[0]))
    ]


def _build_aws_envelope(
    flat_recommendations,
    rollup: AwsRecommendationRollup,
    *,
    scope: str,
) -> CloudRecommendationListResponse:
    """Translate AWS canonical recommendations into the envelope.

    AWS-specific attribution (per-account) is surfaced through the
    provider-neutral ``by_account`` field; the AWS-native source
    values (``AWS_COST_OPTIMIZATION_HUB`` / ``AWS_COMPUTE_OPTIMIZER``
    / ``UNKNOWN``) ride in ``by_source``.  Savings are MONTHLY and
    are never summed across currencies.
    """
    savings_by_currency = [
        CloudRecommendationCurrencyTotal(currency=currency, amount=amount)
        for currency, amount in sorted(rollup.savings_by_currency.items())
    ]
    account_errors = [
        CloudRecommendationAccountError(
            account_id=e.account_id,
            display_name=e.display_name,
            code=e.code,
            message=e.message,
        )
        for e in rollup.account_errors
    ]
    summary_envelope = CloudRecommendationSummaryEnvelope(
        provider="AWS",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=rollup.status,
        savings_status=rollup.savings_status,
        currency=rollup.currency,
        total_recommendations=rollup.total_recommendations,
        recommendations_with_savings=rollup.recommendations_with_savings,
        recommendations_without_savings=rollup.recommendations_without_savings,
        potential_annual_savings=None,
        potential_savings=rollup.potential_savings,
        savings_period=rollup.savings_period,
        savings_by_currency=savings_by_currency,
        by_impact=_group_counts(rollup.by_impact),
        by_resource_type=_group_counts(rollup.by_resource_type),
        by_subscription=[],
        by_resource_group=[],
        by_type=[],
        by_subcategory=[],
        subscription_errors=[],
        by_account=_group_counts(rollup.by_account),
        by_source=_group_counts(rollup.by_source),
        by_action=_group_counts(rollup.by_action),
        account_errors=account_errors,
        source="AWS_OPTIMIZATION",
    )
    items = [
        CloudRecommendationItem(
            provider="AWS",
            account_id=rec.account_id,
            region=rec.region,
            recommendation_id=rec.recommendation_id,
            resource_id=rec.resource_id,
            resource_type=rec.resource_type,
            service=rec.service,
            action=rec.action,
            title=rec.title,
            category=rec.category,
            impact=rec.impact,
            description=rec.description,
            estimated_savings=rec.estimated_savings,
            currency=rec.currency,
            source=rec.source,
            extensions=dict(rec.extensions) if rec.extensions else {},
        )
        for rec in flat_recommendations
    ]
    return CloudRecommendationListResponse(summary=summary_envelope, recommendations=items)


def _build_rackspace_envelope(
    flat_recommendations,
    rollup: RackspaceRecommendationRollup,
    *,
    scope: str,
) -> CloudRecommendationListResponse:
    """Translate the Rackspace rollup into the canonical envelope.

    Mirrors :func:`_build_aws_envelope`.  Savings are MONTHLY-period
    by convention but currently UNKNOWN for every 9C.1/9C.2 finding:
    ``savings_status="EMPTY"`` with ``potential_savings=None`` —
    unknown savings are never reported as ``$0``.  Per-service
    resource failures surface through the provider-neutral
    ``account_errors`` slot (the account id is the safe Cloud tenant;
    the display name names the Rackspace service that failed).
    """
    savings_by_currency = [
        CloudRecommendationCurrencyTotal(currency=currency, amount=amount)
        for currency, amount in sorted(rollup.savings_by_currency.items())
    ]
    account_errors = [
        CloudRecommendationAccountError(
            account_id=e.account_id,
            display_name=e.display_name,
            code=e.code,
            message=e.message,
        )
        for e in rollup.account_errors
    ]
    summary_envelope = CloudRecommendationSummaryEnvelope(
        provider="RACKSPACE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=rollup.status,
        savings_status=rollup.savings_status,
        currency=rollup.currency,
        total_recommendations=rollup.total_recommendations,
        recommendations_with_savings=rollup.recommendations_with_savings,
        recommendations_without_savings=rollup.recommendations_without_savings,
        potential_annual_savings=None,
        potential_savings=rollup.potential_savings,
        savings_period=rollup.savings_period,
        savings_by_currency=savings_by_currency,
        by_impact=_group_counts(rollup.by_impact),
        by_resource_type=_group_counts(rollup.by_resource_type),
        by_subscription=[],
        by_resource_group=[],
        by_type=[],
        by_subcategory=[],
        subscription_errors=[],
        by_account=_group_counts(rollup.by_account),
        by_source=_group_counts(rollup.by_source),
        by_action=_group_counts(rollup.by_action),
        account_errors=account_errors,
        source=RACKSPACE_RECOMMENDATION_SOURCE,
    )
    items = [
        CloudRecommendationItem(
            provider="RACKSPACE",
            account_id=rec.account_id,
            region=rec.region,
            recommendation_id=rec.recommendation_id,
            resource_id=rec.resource_id,
            resource_type=rec.resource_type,
            service=rec.service,
            action=rec.action,
            title=rec.title,
            category=rec.category,
            impact=rec.impact,
            description=rec.description,
            estimated_savings=rec.estimated_savings,
            # "" means "no currency claim accompanies an unknown
            # savings figure" — rendered as None on the wire.
            currency=rec.currency or None,
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
    region: Optional[str] = Query(
        default=None,
        description=(
            "Optional region scope.  AWS defaults to the configured "
            "default region when omitted; Azure ignores it (Advisor "
            "recommendations are subscription-scoped)."
        ),
    ),
    days: int = Query(
        default=30,
        description="Lookback window in days (AWS).  Azure ignores it.",
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

    # AWS keeps the legacy default-region behaviour: absent an
    # explicit region the recommendation fan-out scopes to the
    # configured default region (matching /api/aws/optimization/*).
    effective_region = region
    if kind == CloudProviderKind.AWS and not effective_region:
        from app.core.config import get_settings
        effective_region = get_settings().aws_default_region

    # Stable, provider+account+region+window isolated correlation id.
    # Not persisted (the canonical route is stateless) but asserted by
    # the cache-isolation regression tests.
    _cache_key = _build_recommendation_cache_key(
        user_id=getattr(user, "id", 0) or 0,
        provider=kind.value,
        scope=scope,
        region=effective_region,
        days=days,
    )
    _ = _cache_key

    if kind == CloudProviderKind.AWS:
        try:
            flat, rollup_aws, _errors = await aggregate_aws_recommendations_for_accounts(
                rows=rows, region=effective_region, days=days
            )
        except Exception as exc:  # noqa: BLE001 - sanitised
            logger.warning(
                "cloud.recommendations.upstream_failed provider=%s code=%s",
                kind.value, type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "status": "error",
                    "error_code": "UPSTREAM_ERROR",
                    "message": "upstream recommendations provider failed",
                },
            )
        envelope = _build_aws_envelope(flat, rollup_aws, scope=scope)
        return envelope.model_dump(mode="json")

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

    if kind == CloudProviderKind.RACKSPACE:
        # Phase 9C.2 — the deterministic Rackspace engine behind the
        # same canonical envelope.  ``region`` / ``days`` are ignored
        # (multi-region discovery; no historical lookback) exactly
        # the way Azure ignores ``days``.
        try:
            flat, rollup, _failures = (
                await aggregate_rackspace_recommendations_for_account(
                    rows=rows, region=region, days=days,
                )
            )
        except RackspaceResourceError as exc:
            return _rackspace_error_response(exc)
        except Exception as exc:  # noqa: BLE001 - sanitised
            logger.warning(
                "cloud.recommendations.upstream_failed provider=%s code=%s",
                kind.value, type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "status": "error",
                    "error_code": "UPSTREAM_ERROR",
                    "message": "upstream recommendations provider failed",
                },
            )
        envelope = _build_rackspace_envelope(flat, rollup, scope=scope)
        return envelope.model_dump(mode="json")

    # Any other (future) provider kind is not wired for the canonical
    # recommendation contract yet.  AWS and Azure are both served
    # above; this is a defensive guard for providers beyond
    # RACKSPACE.
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "status": "error",
            "error_code": "UnsupportedProviderForCanonicalRoute",
            "message": (
                f"canonical recommendations do not support "
                f"provider={kind.value}"
            ),
        },
    )


def _rackspace_error_response(exc: RackspaceResourceError) -> JSONResponse:
    """Translate a :class:`RackspaceResourceError` into a sanitised envelope.

    Follows the vocabulary used by the canonical resources route so
    the front-end can reuse its status handling.  A failed resource
    inventory is NEVER converted into a zero-recommendation success.
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
