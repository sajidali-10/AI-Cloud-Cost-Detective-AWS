"""FastAPI route for the evidence endpoint — Phase 2.

``POST /api/aws/evidence`` composes cost + CloudWatch + Phase 1
resource metadata into a deterministic evidence payload.  Per the
Phase 2 spec the per-resource ``cost_context.scope`` is always one
of ``ACCOUNT | SERVICE | REGION`` — we never invent a per-resource
dollar figure.

Per-source failures surface as structured ``Warning`` entries; the
top-level ``status`` is computed by
:func:`app.services.cost_evidence_builder.build_evidence`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.aws import router as aws_router
from app.api.deps import require_role
from app.db.models import AppUser
from app.schemas.evidence import (
    EvidenceResponse,
    Warning,
)
from app.schemas.cost import (
    CostPeriod,
    CostReport,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)
from app.schemas.utilization import Datapoint
from app.services.aws.cloudwatch_metrics import (
    ALLOWED_LOOKBACK_DAYS,
    CloudWatchError,
    ResourceDescriptor,
    aggregate_results,
    batch_query,
    build_metric_queries,
    get_cloudwatch_client,
    period_seconds_for,
)
from app.services.aws.cost_explorer import (
    CostExplorerError,
    aggregate_cost_report,
    get_cost_explorer_client,
)
from app.services.aws.identity import AwsIdentityError, get_caller_identity
from app.services.aws.resources import enumerate_all_services
from app.services.cost_evidence_builder import build_evidence

logger = logging.getLogger("cost-detective-backend.aws_evidence")


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    region: str = Field(min_length=1)
    days: int = Field(default=30)
    resource_types: Optional[list[str]] = None


@aws_router.post(
    "/evidence",
    summary="Cost + CloudWatch + resource evidence for the selected region/days",
)
async def post_aws_evidence(
    payload: EvidenceRequest,
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    if payload.days not in ALLOWED_LOOKBACK_DAYS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "error",
                "error_code": "InvalidLookbackDays",
                "message": f"days={payload.days!r} is not allowed",
            },
        )
    region = payload.region

    # Phase 1 enumerator — never raises, may produce warnings per service.
    services = enumerate_all_services(region=region)
    resources_warning: Optional[Warning] = None
    if any(getattr(s, "status", None) in ("denied", "error") for s in services.values()):
        first_bad = next(s for s in services.values() if getattr(s, "status", None) in ("denied", "error"))
        resources_warning = Warning(
            source="resources",
            service=first_bad.service,
            code=first_bad.error_code or first_bad.status.upper(),
            message="One or more Phase 1 enumerators returned a non-ok status",
        )

    # Cost Explorer — optional, fails soft into a Warning.
    cost_report = None
    cost_warning: Optional[Warning] = None
    try:
        ce_client = get_cost_explorer_client(region=region)
        cost_report = await asyncio.to_thread(aggregate_cost_report, ce_client, payload.days)
    except (CostExplorerError, ClientError, BotoCoreError) as exc:
        cost_warning = Warning(
            source="cost_explorer",
            service="ce",
            code=getattr(exc, "code", type(exc).__name__) or type(exc).__name__,
            message="Cost Explorer was not available for this request.",
        )
        logger.warning("aws.evidence cost_explorer error=%s", type(exc).__name__)

    # Convert cost_report dataclass -> Pydantic so the builder can
    # use the same shape regardless of source.
    cost_report_pyd = _to_pydantic_cost_report(cost_report) if cost_report is not None else None

    # CloudWatch — also fails soft.
    util_series = []
    util_warning: Optional[Warning] = None
    try:
        descriptors = _descriptors(services, region=region, types_filter=payload.resource_types)
        if descriptors:
            queries = build_metric_queries(descriptors)
            cw_client = get_cloudwatch_client(region=region)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=payload.days)
            period = period_seconds_for(payload.days)
            results = await asyncio.to_thread(
                batch_query, cw_client, queries, start=start, end=end, period_seconds=period
            )
            util_series = aggregate_results(
                results,
                queries=queries,
                resources_by_id={d.resource_id: d for d in descriptors},
                lookback_days=payload.days,
                period_seconds=period,
            )
    except CloudWatchError as exc:
        util_warning = Warning(
            source="cloudwatch",
            service="cloudwatch",
            code=exc.code,
            message="CloudWatch utilization was not available.",
        )
    except (ClientError, BotoCoreError) as exc:
        util_warning = Warning(
            source="cloudwatch",
            service="cloudwatch",
            code=type(exc).__name__,
            message="CloudWatch utilization was not available.",
        )

    response = build_evidence(
        region=region,
        days=payload.days,
        phase1_services=services,
        cost_report=cost_report_pyd,
        utilization_series=util_series,
        cost_warning=cost_warning,
        utilization_warning=util_warning,
        resources_warning=resources_warning,
    )
    return response.model_dump(mode="json")


def _to_pydantic_cost_report(raw) -> CostReport:
    """Convert a ``cost_explorer.CostReport`` dataclass to Pydantic."""
    period = CostPeriod(start=raw.period.start, end=raw.period.end, days=raw.period.days)
    previous = CostPeriod(start=raw.previous.start, end=raw.previous.end, days=raw.previous.days)
    change_amount = raw.current_total - raw.previous_total
    from decimal import Decimal
    if raw.previous_total == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change_amount / raw.previous_total).quantize(Decimal("0.01"))
    return CostReport(
        account_id="000000000000",
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


def _descriptors(services: Dict[str, Any], *, region: str, types_filter: Optional[list[str]]) -> list:
    from app.api.aws_utilization import _descriptors_from_phase1
    return _descriptors_from_phase1(services, region=region, types_filter=types_filter)


__all__ = ["post_aws_evidence"]
