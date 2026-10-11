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
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.aws import router as aws_router
from app.api.aws_utilization import _descriptors_from_phase1
from app.api.deps import require_role
from app.db.session import get_db
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


def _route_deadline_seconds() -> float:
    """Hard deadline for the optimization route handler.

    Phase 7A: the route must return *something* (even a partial
    response) well before the 25 s frontend budget expires.  We pick
    22 s so network/protocol overhead still has headroom.
    """
    try:
        return float(os.getenv("OPTIMIZATION_ROUTE_DEADLINE_SECONDS", "22.0"))
    except (TypeError, ValueError):
        return 22.0


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

    Sync entry point used by the FastAPI ``def`` route handlers.
    Returns an empty dict on CloudWatch failure (with a logged
    warning).  The orchestrator treats an empty utilization map
    as "no coverage" and downgrades the deterministic confidence
    accordingly.

    Phase 7A: the CloudWatch batch query is bounded by the
    ``OPTIMIZATION_TIMEOUT_CLOUDWATCH`` budget (default 10s).  When
    exceeded the function returns an empty dict and emits a
    ``TIMED_OUT`` notice via the route layer — deterministic rules
    remain usable on the partial evidence.

    Callers already running inside an event loop (e.g. Phase 4
    ``AIService._gather_async``) MUST use
    :func:`_fetch_utilization_async` instead — calling this sync
    wrapper from an async context would deadlock the loop on
    ``asyncio.run``.
    """
    from app.services.optimization_engine import _run_with_budget

    descriptors = _descriptors_from_phase1(phase1_services, region=region, types_filter=None)
    if not descriptors:
        return {}
    queries = build_metric_queries(descriptors)
    cw_client = get_cloudwatch_client(region=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    period = period_seconds_for(days)

    def _do() -> Dict[str, list]:
        try:
            results = _run_batch_query_blocking(
                cw_client=cw_client,
                queries=queries,
                start=start,
                end=end,
                period=period,
            )
        except (CloudWatchError, ClientError, BotoCoreError) as exc:
            logger.warning("aws.optimization cloudwatch error=%s", type(exc).__name__)
            return {}
        return _aggregate_batch_results(
            results=results,
            queries=queries,
            descriptors=descriptors,
            days=days,
            period=period,
        )

    timed_out, result = _run_with_budget("cloudwatch_utilization", _do)
    if timed_out:
        logger.warning(
            "aws.optimization cloudwatch timeout budget=%ss",
            os.getenv("OPTIMIZATION_TIMEOUT_CLOUDWATCH", "10.0"),
        )
        return {}
    if isinstance(result, BaseException):
        logger.warning("aws.optimization cloudwatch error=%s", type(result).__name__)
        return {}
    return result  # type: ignore[return-value]


async def _fetch_utilization_async(
    *,
    region: str,
    days: int,
    phase1_services: Dict[str, Any],
) -> Dict[str, list]:
    """Async variant of :func:`_fetch_utilization`.

    Used by :func:`app.services.ai_service._gather_async` which is
    itself running inside an event loop.  Awaits
    ``asyncio.to_thread(...)`` so the underlying coroutine is
    actually consumed — ``asyncio.run(asyncio.to_thread(...))`` is
    a bug because ``to_thread`` returns a coroutine that never gets
    awaited.
    """
    descriptors = _descriptors_from_phase1(phase1_services, region=region, types_filter=None)
    if not descriptors:
        return {}
    queries = build_metric_queries(descriptors)
    cw_client = get_cloudwatch_client(region=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    period = period_seconds_for(days)
    try:
        results = await _run_batch_query_async(
            cw_client=cw_client,
            queries=queries,
            start=start,
            end=end,
            period=period,
        )
    except (CloudWatchError, ClientError, BotoCoreError) as exc:
        logger.warning("aws.optimization cloudwatch error=%s", type(exc).__name__)
        return {}
    return _aggregate_batch_results(
        results=results,
        queries=queries,
        descriptors=descriptors,
        days=days,
        period=period,
    )


def _run_batch_query_blocking(
    *,
    cw_client: Any,
    queries: list,
    start: datetime,
    end: datetime,
    period: int,
) -> Dict[str, list]:
    """Run ``batch_query`` in a fresh event loop (sync context).

    Used by the FastAPI ``def`` route handlers which run outside an
    existing event loop.  Spins up a dedicated loop with
    ``asyncio.run`` and awaits ``asyncio.to_thread`` inside it so
    the underlying coroutine is consumed properly.
    """

    async def _runner() -> Dict[str, list]:
        return await asyncio.to_thread(
            batch_query,
            cw_client,
            queries,
            start=start,
            end=end,
            period_seconds=period,
        )

    return asyncio.run(_runner())


async def _run_batch_query_async(
    *,
    cw_client: Any,
    queries: list,
    start: datetime,
    end: datetime,
    period: int,
) -> Dict[str, list]:
    """Run ``batch_query`` in the current event loop.

    Used by callers already inside an event loop (Phase 4
    ``AIService._gather_async``).  Awaits ``asyncio.to_thread`` so
    the underlying coroutine is actually consumed.
    """
    return await asyncio.to_thread(
        batch_query,
        cw_client,
        queries,
        start=start,
        end=end,
        period_seconds=period,
    )


def _aggregate_batch_results(
    *,
    results: Any,
    queries: list,
    descriptors: list,
    days: int,
    period: int,
) -> Dict[str, list]:
    """Aggregate ``batch_query`` results into the ``{rid: [series]}`` shape."""
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
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all'."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    settings = _settings()
    effective_region = region or settings.aws_default_region

    # Phase 7B: route the identity probe through the registry when
    # the caller names a specific account.  The default (``all``)
    # preserves Phase 7A behaviour (management instance profile).
    selection = (account_id or "all").strip()
    target_row = None
    if selection != "all":
        from app.services.aws.account_provider import resolve_account_or_404
        target_row = resolve_account_or_404(db, selection)
        if target_row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error",
                    "error_code": "AccountNotFound",
                    "message": f"no AWS account matches selection={selection!r}",
                },
            )

    factory = get_default_session_factory()
    try:
        if target_row is None:
            session = factory.management_session()
        elif target_row.is_management:
            session = factory.management_session()
        else:
            session = factory.target_session(
                role_arn=target_row.role_arn,
                external_id=target_row.external_id,
                region=effective_region,
            )
        from app.services.aws.identity import get_caller_identity
        identity = get_caller_identity(
            region=effective_region, session=session
        )
    except Exception as exc:  # noqa: BLE001 — sanitized
        logger.warning(
            "aws.optimization.identity_failed code=%s",
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "IdentityUnavailable",
                "region": effective_region,
            },
        )
    resolved_account_id = identity.get("account")
    caps = build_capabilities(
        region=effective_region, account_id=resolved_account_id
    )
    payload = caps.model_dump(mode="json")
    if target_row is not None:
        payload["account_id"] = target_row.account_id
        payload["account_display_name"] = target_row.display_name
    return payload


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
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all'."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    bad_lookback = _validate_lookback(days)
    if bad_lookback is not None:
        return bad_lookback

    settings = _settings()
    effective_region = region or settings.aws_default_region

    selection = (account_id or "all").strip()
    target_row = None
    if selection != "all":
        from app.services.aws.account_provider import resolve_account_or_404
        target_row = resolve_account_or_404(db, selection)
        if target_row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error",
                    "error_code": "AccountNotFound",
                    "message": f"no AWS account matches selection={selection!r}",
                },
            )

    factory = get_default_session_factory()
    try:
        if target_row is None or target_row.is_management:
            session = factory.management_session()
        else:
            session = factory.target_session(
                role_arn=target_row.role_arn,
                external_id=target_row.external_id,
                region=effective_region,
            )
        from app.services.aws.identity import get_caller_identity
        identity = get_caller_identity(
            region=effective_region, session=session
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("aws.optimization identity error_code=%s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "IdentityUnavailable",
                "region": effective_region,
            },
        )
    account_id_resolved = identity.get("account")

    # Phase 1 inventory scoped through the registry row when supplied.
    from app.services.aws import clients as _clients_module
    original = _clients_module.get_aws_client

    def _patched(service_name: str, region: Optional[str] = None):
        if target_row is None or target_row.is_management:
            return factory.management_client(service_name, region=region or effective_region)
        return factory.target_client(
            service_name,
            role_arn=target_row.role_arn,
            external_id=target_row.external_id,
            region=region or effective_region,
        )

    _clients_module.get_aws_client = _patched
    try:
        phase1_services = enumerate_all_services(region=effective_region)
        utilization_by_resource = _fetch_utilization(
            region=effective_region, days=days, phase1_services=phase1_services
        )
    finally:
        _clients_module.get_aws_client = original

    inputs = OptimizationInputs(
        region=effective_region,
        account_id=account_id_resolved,
        days=days,
        phase1_services=phase1_services,
        utilization_by_resource=utilization_by_resource,
    )
    recommendations, warnings = build_recommendations(inputs)

    if not recommendations:
        status_value = "FAILED" if warnings else "SUCCESS"
    elif warnings:
        status_value = "PARTIAL_SUCCESS"
    else:
        status_value = "SUCCESS"

    response = RecommendationsResponse(
        region=effective_region,
        account_id=account_id_resolved,
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
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all'."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    bad_lookback = _validate_lookback(days)
    if bad_lookback is not None:
        return bad_lookback

    settings = _settings()
    effective_region = region or settings.aws_default_region

    selection = (account_id or "all").strip()
    target_row = None
    if selection != "all":
        from app.services.aws.account_provider import resolve_account_or_404
        target_row = resolve_account_or_404(db, selection)
        if target_row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "status": "error",
                    "error_code": "AccountNotFound",
                    "message": f"no AWS account matches selection={selection!r}",
                },
            )

    factory = get_default_session_factory()
    try:
        if target_row is None or target_row.is_management:
            session = factory.management_session()
        else:
            session = factory.target_session(
                role_arn=target_row.role_arn,
                external_id=target_row.external_id,
                region=effective_region,
            )
        from app.services.aws.identity import get_caller_identity
        identity = get_caller_identity(
            region=effective_region, session=session
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("aws.optimization identity error_code=%s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "IdentityUnavailable",
                "region": effective_region,
            },
        )
    account_id_resolved = identity.get("account")

    from app.services.aws import clients as _clients_module
    original = _clients_module.get_aws_client

    def _patched(service_name: str, region: Optional[str] = None):
        if target_row is None or target_row.is_management:
            return factory.management_client(service_name, region=region or effective_region)
        return factory.target_client(
            service_name,
            role_arn=target_row.role_arn,
            external_id=target_row.external_id,
            region=region or effective_region,
        )

    _clients_module.get_aws_client = _patched
    try:
        phase1_services = enumerate_all_services(region=effective_region)
        utilization_by_resource = _fetch_utilization(
            region=effective_region, days=days, phase1_services=phase1_services
        )
    finally:
        _clients_module.get_aws_client = original

    inputs = OptimizationInputs(
        region=effective_region,
        account_id=account_id_resolved,
        days=days,
        phase1_services=phase1_services,
        utilization_by_resource=utilization_by_resource,
    )
    recommendations, warnings = build_recommendations(inputs)
    summary = build_summary(
        region=effective_region,
        account_id=account_id_resolved,
        days=days,
        recommendations=recommendations,
        warnings=warnings,
    )
    payload = summary.model_dump(mode="json")
    if target_row is not None:
        payload["account_id"] = target_row.account_id
        payload["account_display_name"] = target_row.display_name
    return payload


__all__ = [
    "get_optimization_capabilities",
    "get_optimization_recommendations",
    "get_optimization_summary",
]
