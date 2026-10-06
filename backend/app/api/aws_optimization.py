"""FastAPI routes for the optimization engine — Phase 3.

Three endpoints under ``/api/aws/optimization``:

* ``GET /api/aws/optimization/capabilities`` — enrollment state for
  Compute Optimizer and Cost Optimization Hub plus supported rules
  / resource types / lookbacks.
* ``GET /api/aws/optimization/recommendations?region=&days=`` — the
  deduplicated recommendation list.
* ``GET /api/aws/optimization/summary?region=&days=`` — aggregate
  counts + savings by resource type / action / source / confidence.

All three reuse the Phase 2 validation invariants (``{7, 30, 60, 90}``
lookback allow-list, sanitized errors, read-only AWS guard).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.api.aws_utilization import _descriptors_from_phase1
from app.api.deps import require_role
from app.core.config import get_settings
from app.db.models import AppUser
from app.schemas.optimization import (
    CapabilitiesResponse,
    RecommendationsResponse,
    SummaryResponse,
)
from app.services.aws.cloudwatch_metrics import (
    ALLOWED_LOOKBACK_DAYS,
    CloudWatchError,
    aggregate_results,
    batch_query,
    build_metric_queries,
    get_cloudwatch_client,
    period_seconds_for,
)
from app.services.aws.identity import AwsIdentityError, get_caller_identity
from app.services.aws.resources import enumerate_all_services
from app.services.optimization_engine import (
    OptimizationInputs,
    build_capabilities,
    build_recommendations,
    build_summary,
)

logger = logging.getLogger("cost-detective-backend.aws_optimization")

_settings = get_settings


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_lookback(days: int) -> Optional[JSONResponse]:
    """Return a sanitized 422 ``JSONResponse`` if ``days`` is invalid.

    Returning ``None`` means "valid".  This keeps the route handlers
    short and consistent.
    """
    if days not in ALLOWED_LOOKBACK_DAYS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "error",
                "error_code": "InvalidLookbackDays",
                "message": f"days={days!r} is not allowed; "
                f"allowed values are {sorted(ALLOWED_LOOKBACK_DAYS)}",
            },
        )
    return None


def _resolve_identity(region: str) -> Optional[Dict[str, Any]]:
    """Return the STS identity or ``None`` on a sanitized failure."""
    try:
        return get_caller_identity(region=region)
    except AwsIdentityError as exc:
        logger.warning("aws.optimization identity error_code=%s", exc.code)
        return None


def _fetch_utilization(
    *,
    region: str,
    days: int,
    phase1_services: Dict[str, Any],
) -> Dict[str, list]:
    """Return ``{resource_id: [MetricSeries, ...]}`` for the request.

    Returns an empty dict on CloudWatch failure (with a logged warning).
    The orchestrator treats an empty utilization map as "no coverage"
    and downgrades the deterministic confidence accordingly.
    """
    descriptors = _descriptors_from_phase1(phase1_services, region=region, types_filter=None)
    if not descriptors:
        return {}
    try:
        queries = build_metric_queries(descriptors)
        cw_client = get_cloudwatch_client(region=region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        period = period_seconds_for(days)
        results = asyncio.run(
            asyncio.to_thread(
                batch_query, cw_client, queries, start=start, end=end, period_seconds=period
            )
        )
    except (CloudWatchError, ClientError, BotoCoreError) as exc:
        logger.warning("aws.optimization cloudwatch error=%s", type(exc).__name__)
        return {}
    series = aggregate_results(
        results,
        queries=queries,
        resources_by_id={d.resource_id: d for d in descriptors},
        lookback_days=days,
        period_seconds=period,
    )
    out: Dict[str, list] = {}
    for s in series:
        out.setdefault(s.resource_id, []).append(s)
    return out


# ---------------------------------------------------------------------------
# Route — GET /api/aws/optimization/capabilities
# ---------------------------------------------------------------------------


@aws_router.get(
    "/optimization/capabilities",
    summary="Compute Optimizer + Cost Optimization Hub enrollment state",
)
def get_optimization_capabilities(
    region: Optional[str] = Query(default=None),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    settings = _settings()
    effective_region = region or settings.aws_default_region
    identity = _resolve_identity(effective_region)
    account_id = identity.get("account") if identity else None
    caps = build_capabilities(region=effective_region, account_id=account_id)
    return caps.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Route — GET /api/aws/optimization/recommendations
# ---------------------------------------------------------------------------


@aws_router.get(
    "/optimization/recommendations",
    summary="Deduplicated AWS optimization recommendations",
)
def get_optimization_recommendations(
    region: Optional[str] = Query(default=None),
    days: int = Query(default=30),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    bad_lookback = _validate_lookback(days)
    if bad_lookback is not None:
        return bad_lookback

    settings = _settings()
    effective_region = region or settings.aws_default_region

    identity = _resolve_identity(effective_region)
    account_id = identity.get("account") if identity else None
    if identity is None:
        # The capabilities endpoint survives identity failure (the
        # route returns a 200 with warnings); for recommendations we
        # still return a sanitized 502 because no resource can be
        # attributed to an account.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "IdentityUnavailable",
                "region": effective_region,
            },
        )

    # Phase 1 inventory — never raises.
    phase1_services = enumerate_all_services(region=effective_region)

    # CloudWatch utilization — degrades to empty (no coverage) on failure.
    utilization_by_resource = _fetch_utilization(
        region=effective_region, days=days, phase1_services=phase1_services
    )

    inputs = OptimizationInputs(
        region=effective_region,
        account_id=account_id,
        days=days,
        phase1_services=phase1_services,
        utilization_by_resource=utilization_by_resource,
    )
    recommendations, warnings = build_recommendations(inputs)

    # Compute status.  Both AWS-native sources failed AND no deterministic
    # rules ran -> FAILED.  Otherwise PARTIAL_SUCCESS if there are any
    # warnings, else SUCCESS.
    if not recommendations:
        status_value = "FAILED" if warnings else "SUCCESS"
    elif warnings:
        status_value = "PARTIAL_SUCCESS"
    else:
        status_value = "SUCCESS"

    response = RecommendationsResponse(
        region=effective_region,
        account_id=account_id,
        days=days,
        status=status_value,
        count=len(recommendations),
        recommendations=recommendations,
        warnings=warnings,
    )
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Route — GET /api/aws/optimization/summary
# ---------------------------------------------------------------------------


@aws_router.get(
    "/optimization/summary",
    summary="Aggregate counts + savings by category for the recommendations",
)
def get_optimization_summary(
    region: Optional[str] = Query(default=None),
    days: int = Query(default=30),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    bad_lookback = _validate_lookback(days)
    if bad_lookback is not None:
        return bad_lookback

    settings = _settings()
    effective_region = region or settings.aws_default_region

    identity = _resolve_identity(effective_region)
    account_id = identity.get("account") if identity else None
    if identity is None:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "IdentityUnavailable",
                "region": effective_region,
            },
        )

    phase1_services = enumerate_all_services(region=effective_region)
    utilization_by_resource = _fetch_utilization(
        region=effective_region, days=days, phase1_services=phase1_services
    )

    inputs = OptimizationInputs(
        region=effective_region,
        account_id=account_id,
        days=days,
        phase1_services=phase1_services,
        utilization_by_resource=utilization_by_resource,
    )
    recommendations, warnings = build_recommendations(inputs)
    summary = build_summary(
        region=effective_region,
        account_id=account_id,
        days=days,
        recommendations=recommendations,
        warnings=warnings,
    )
    return summary.model_dump(mode="json")


__all__ = [
    "get_optimization_capabilities",
    "get_optimization_recommendations",
    "get_optimization_summary",
]
