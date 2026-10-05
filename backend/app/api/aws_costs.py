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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.core.config import get_settings
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
    get_cost_explorer_client,
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


def _build_cache_key(*, days: int) -> str:
    settings = _settings()
    raw = "|".join(
        [
            "v1",
            settings.cost_detective_db,
            str(days),
            settings.aws_default_region,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Route — GET /api/aws/costs
# ---------------------------------------------------------------------------


@aws_router.get(
    "/costs",
    summary="AWS Cost Explorer spend for the selected lookback window",
)
def get_aws_costs(
    days: int = Query(
        default=30,
        description=(
            "Lookback window in days. Allowed values: "
            f"{sorted(ALLOWED_LOOKBACK_DAYS)}. Any other value yields 422."
        ),
    ),
) -> Any:
    """Return the Cost Explorer report for the requested lookback window."""
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
    cache_key = _build_cache_key(days=days)

    ce_client = get_cost_explorer_client(region=region)

    async def _loader() -> CostReport:
        # ``aggregate_cost_report`` is synchronous; run it in a thread
        # so the FastAPI event loop is not blocked.
        report = await asyncio.to_thread(aggregate_cost_report, ce_client, days)
        # Map the dataclass -> Pydantic so the cache layer round-trips
        # through JSONB cleanly.
        return _to_pydantic_report(report, account_id=account_id)

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
