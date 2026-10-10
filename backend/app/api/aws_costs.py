"""FastAPI routes for Cost Explorer — Phase 2.

Mounted on the existing :data:`router` from :mod:`app.api.aws` so the
public URL stays consistent with Phase 1 (``/api/aws/costs``).

Only one endpoint ships in this slice:

    GET /api/aws/costs?days=7|30|60|90

Days outside the allowed set yield a sanitized HTTP 422 (FastAPI's
default Pydantic validation envelope) so the caller never sees a
Boto3 stack trace.

The route handler is intentionally thin: it pulls the AWS account ID
via STS (``GetCallerIdentity`` — which is already read-only), builds
the cache key, and delegates to :func:`app.services.cost_cache.get_or_refresh`
for the actual cost retrieval.  Cost Explorer failures surface as a
sanitized 502 with a stable ``error_code``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.api.deps import require_role
from app.core.config import get_settings
from app.db.models import AppUser
from app.schemas.cost import (
    CacheStatus,
    CostPeriod,
    CostReport,
    CostReportResponse,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)
from app.services.aws.cost_explorer import (
    ALLOWED_LOOKBACK_DAYS,
    CostExplorerError,
    aggregate_cost_report,
    build_explicit_period,
    build_month_period,
    build_period,
    get_cost_explorer_client,
    matched_mtd_previous_period,
    parse_month as parse_month_unvalidated,
    previous_month_period,
)
from app.services.aws.identity import (
    AwsIdentityError,
    get_caller_identity,
)
from app.services.cost_cache import get_or_refresh

logger = logging.getLogger("cost-detective-backend.aws_costs")

_settings = get_settings


# ---------------------------------------------------------------------------
# Helper — build cache key covering every dimension that affects the
# response.  Anything that changes the AWS output MUST appear here.
# ---------------------------------------------------------------------------


def _build_cache_key(*, kind: str, signature: str) -> str:
    """Build a cache key covering the period selector.

    ``kind`` is one of ``"days"``, ``"month"``, ``"explicit"``.
    ``signature`` is the encoded parameter (``7`` / ``2026-10`` /
    ``2026-10-01:2026-11-01``).  Anything that changes the AWS
    output MUST appear here.
    """
    settings = _settings()
    raw = "|".join(
        [
            "v2",
            settings.cost_detective_db,
            kind,
            signature,
            settings.aws_default_region,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Route — GET /api/aws/costs
# ---------------------------------------------------------------------------


@aws_router.get(
    "/costs",
    summary="AWS Cost Explorer spend for the selected period",
)
def get_aws_costs(
    days: Optional[int] = Query(
        default=None,
        description=(
            "Rolling lookback window. Allowed values: "
            f"{sorted(ALLOWED_LOOKBACK_DAYS)}. Backward-compatible."
        ),
    ),
    month: Optional[str] = Query(
        default=None,
        description="Calendar month in YYYY-MM form (Phase 7A preferred).",
    ),
    start_date: Optional[date] = Query(
        default=None,
        description="Optional explicit start date (YYYY-MM-DD).",
    ),
    end_date: Optional[date] = Query(
        default=None,
        description="Optional explicit end date (YYYY-MM-DD, exclusive).",
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    """Return the Cost Explorer report for the requested period.

    Period resolution order (Phase 7A):

    1. ``month=YYYY-MM`` — calendar month, MTD-clipped if current.
    2. ``start_date`` + ``end_date`` — explicit UTC range.
    3. ``days`` — legacy rolling lookback (back-compat).
    4. ``days`` defaults to 30 if none of the above are supplied.

    The comparison period (returned as ``previous_period`` on the
    wire) is:

    * For calendar-month requests: matched MTD when the month is
      the current month, full previous calendar month otherwise.
    * For explicit ranges: the immediately-preceding equal-length
      window.
    * For rolling-lookback: the existing ``previous_period``
      helper is used.
    """
    settings = _settings()
    region = settings.aws_default_region

    try:
        identity = get_caller_identity(region=region)
    except AwsIdentityError as exc:
        logger.warning("aws.costs identity error_code=%s", exc.code)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": exc.code,
                "region": region,
            },
        )

    account_id = identity["account"]

    # ----- period resolution -----
    if month is not None:
        try:
            today = datetime.now(timezone.utc).date()
            # Detect whether the requested month is the current
            # month BEFORE clipping — that's how we know to compare
            # against matched-MTD instead of the full prior month.
            requested_year, requested_m = parse_month_unvalidated(month)
            is_current_month = (
                requested_year == today.year and requested_m == today.month
            )
            period = build_month_period(month, today=today)
            if is_current_month:
                prev = matched_mtd_previous_period(month, today=today)
            else:
                prev = previous_month_period(month)
            cache_kind = "month"
            cache_signature = f"{month}|{period.days}"
        except CostExplorerError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "status": "error",
                    "error_code": exc.code,
                    "message": exc.message,
                },
            )
    elif start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "status": "error",
                    "error_code": "InvalidDates",
                    "message": "start_date and end_date must be supplied together",
                },
            )
        try:
            period = build_explicit_period(start_date, end_date)
            prev = CostPeriod(
                start=period.start - timedelta(days=period.days),
                end=period.start,
                days=period.days,
            )
            cache_kind = "explicit"
            cache_signature = f"{period.start.isoformat()}:{period.end.isoformat()}"
        except CostExplorerError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "status": "error",
                    "error_code": exc.code,
                    "message": exc.message,
                },
            )
    else:
        effective_days = days if days is not None else 30
        if effective_days not in ALLOWED_LOOKBACK_DAYS:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "status": "error",
                    "error_code": "InvalidLookbackDays",
                    "message": f"days={effective_days!r} is not allowed; "
                    f"allowed values are {sorted(ALLOWED_LOOKBACK_DAYS)}",
                },
            )
        period = build_period(effective_days)
        prev = previous_period(period)
        cache_kind = "days"
        cache_signature = str(effective_days)

    cache_key = _build_cache_key(kind=cache_kind, signature=cache_signature)

    ce_client = get_cost_explorer_client(region=region)

    async def _loader() -> CostReport:
        # Phase 7A: build the report using the resolved period directly
        # so calendar-month and explicit ranges both flow through the
        # same Cost Explorer logic, then wrap the dataclass into the
        # Pydantic envelope used by the cache layer.
        raw = await asyncio.to_thread(
            _aggregate_for_period, ce_client, period, prev
        )
        return _to_pydantic_report(raw, account_id=account_id)

    try:
        response = asyncio.run(get_or_refresh(
            account_id=account_id,
            cache_key=cache_key,
            loader=_loader,
        ))
    except CostExplorerError as exc:
        logger.warning("aws.costs cost_explorer error_code=%s", exc.code)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": exc.code,
                "region": region,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning("aws.costs boto error=%s", type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": type(exc).__name__,
                "region": region,
            },
        )

    return response.model_dump(mode="json")


def _aggregate_for_period(
    client: Any,
    period: "CostPeriod",
    previous: "CostPeriod",
) -> Any:
    """Compose the report for an arbitrary ``CostPeriod`` (Phase 7A)."""
    from app.services.aws.cost_explorer import (
        aggregate_cost_report_for_period,
    )
    return aggregate_cost_report_for_period(client, period, previous)


# ---------------------------------------------------------------------------
# Dataclass -> Pydantic adapter (kept here so ``cost_explorer.py``
# stays free of FastAPI / Pydantic dependencies).
# ---------------------------------------------------------------------------


def _to_pydantic_report(raw, account_id: str) -> CostReport:  # type: ignore[no-untyped-def]
    """Convert a ``cost_explorer.CostReport`` dataclass into a Pydantic model."""
    period = CostPeriod(start=raw.period.start, end=raw.period.end, days=raw.period.days)
    previous = CostPeriod(start=raw.previous.start, end=raw.previous.end, days=raw.previous.days)
    change_amount = raw.current_total - raw.previous_total
    if raw.previous_total == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change_amount / raw.previous_total).quantize(Decimal("0.01"))
    return CostReport(
        account_id=account_id,
        period=period,
        previous_period=previous,
        currency=raw.unit,
        total_cost=raw.current_total,
        previous_period_cost=raw.previous_total,
        change_amount=change_amount,
        change_percent=change_percent,
        estimated=raw.estimated,
        daily_trend=[
            DailyCostPoint(date=day, amount=amount, unit=unit)
            for (day, amount, unit) in raw.daily
        ],
        by_service=[
            ServiceCost(service=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_service
        ],
        by_region=[
            RegionCost(region=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_region
        ],
        source="AWS_COST_EXPLORER",
    )


__all__ = ["get_aws_costs"]
