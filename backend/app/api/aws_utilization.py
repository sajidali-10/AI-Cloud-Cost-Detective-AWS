"""FastAPI route for CloudWatch utilization — Phase 2.

``POST /api/aws/utilization`` accepts a small JSON body
(``{region, lookback_days, resource_types?}``) and returns per-resource
CloudWatch metric series with a structured ``data_quality`` per
resource.  The request schema rejects arbitrary AWS operation names
— the backend owns the allowlist of which metrics are valid for which
resource type.

Per-service and per-region CloudWatch failures produce structured
``Warning`` entries in the response body; they NEVER fail the whole
call (per the Phase 2 spec on partial failures).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.schemas.utilization import (
    Datapoint,
    MetricSeries,
    ResourceUtilization,
    UtilizationRequest,
    UtilizationResponse,
    Warning,
)
from app.services.aws.cloudwatch_metrics import (
    ALLOWED_LOOKBACK_DAYS,
    CloudWatchError,
    MetricQuery,
    ResourceDescriptor,
    aggregate_results,
    batch_query,
    build_metric_queries,
    get_cloudwatch_client,
    period_seconds_for,
)
from app.services.aws.resources import enumerate_all_services

logger = logging.getLogger("cost-detective-backend.aws_utilization")

# Resource-type allowlist per resource type slug.  These mirror the
# keys in :mod:`app.services.aws.cloudwatch_metrics` ``_METRIC_SPECS_BY_TYPE``.
_SUPPORTED_TYPES = ("ec2", "rds", "lambda", "alb", "nlb")


# ---------------------------------------------------------------------------
# Helpers — Phase 1 normalized rows -> ResourceDescriptor list
# ---------------------------------------------------------------------------


def _descriptors_from_phase1(
    services: Dict[str, Any],
    *,
    region: str,
    types_filter: Optional[List[str]],
) -> List[ResourceDescriptor]:
    """Convert the Phase 1 enumerator output into ``ResourceDescriptor`` rows."""
    out: List[ResourceDescriptor] = []
    wants = set(types_filter) if types_filter else set(_SUPPORTED_TYPES)

    ec2 = services.get("ec2")
    if "ec2" in wants and ec2 and ec2.status == "ok":
        for item in ec2.items:
            instance_id = item.get("instance_id")
            if not instance_id:
                continue
            out.append(
                ResourceDescriptor(
                    resource_id=instance_id,
                    resource_type="ec2",
                    region=region,
                )
            )

    rds = services.get("rds")
    if "rds" in wants and rds and rds.status == "ok":
        for item in rds.items:
            dbi = item.get("db_instance_identifier")
            if not dbi:
                continue
            out.append(
                ResourceDescriptor(resource_id=dbi, resource_type="rds", region=region)
            )

    lam = services.get("lambda")
    if "lambda" in wants and lam and lam.status == "ok":
        for item in lam.items:
            name = item.get("function_name")
            if not name:
                continue
            out.append(
                ResourceDescriptor(resource_id=name, resource_type="lambda", region=region)
            )

    elbv2 = services.get("elbv2")
    if elbv2 and elbv2.status == "ok":
        for item in elbv2.items:
            arn = item.get("arn")
            lb_type = item.get("type")
            if not arn or lb_type not in ("application", "network"):
                continue
            metric_type = "alb" if lb_type == "application" else "nlb"
            if metric_type not in wants:
                continue
            out.append(
                ResourceDescriptor(resource_id=arn, resource_type=metric_type, region=region)
            )

    return out


def _type_for_resource(resources_by_id: Dict[str, ResourceDescriptor], resource_id: str) -> str:
    desc = resources_by_id.get(resource_id)
    if desc is None:
        return "unknown"
    mapping = {"ec2": "EC2", "rds": "RDS", "lambda": "Lambda", "alb": "ELBv2", "nlb": "ELBv2"}
    return mapping.get(desc.resource_type, "unknown")


# ---------------------------------------------------------------------------
# Route — POST /api/aws/utilization
# ---------------------------------------------------------------------------


@aws_router.post(
    "/utilization",
    summary="CloudWatch utilization for the selected resource scope",
)
async def post_aws_utilization(payload: UtilizationRequest) -> Any:
    """Return CloudWatch utilization for the requested resource scope."""
    if payload.lookback_days not in ALLOWED_LOOKBACK_DAYS:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "error",
                "error_code": "InvalidLookbackDays",
                "message": f"lookback_days={payload.lookback_days!r} is not allowed; "
                f"allowed values are {sorted(ALLOWED_LOOKBACK_DAYS)}",
            },
        )

    region = payload.region
    types_filter = payload.resource_types

    # Phase 1 enumerator — read-only, never raises.
    services = enumerate_all_services(region=region)
    descriptors = _descriptors_from_phase1(
        services, region=region, types_filter=types_filter
    )

    warnings: List[Warning] = []
    for svc_name, svc_result in services.items():
        if svc_result.status in ("denied", "error"):
            warnings.append(
                Warning(
                    source="resources",
                    service=svc_name,
                    code=svc_result.error_code or svc_result.status.upper(),
                    message=f"Phase 1 enumerator returned status={svc_result.status}",
                )
            )

    if not descriptors:
        return UtilizationResponse(
            region=region,
            lookback_days=payload.lookback_days,
            resources=[],
            warnings=warnings,
        ).model_dump(mode="json")

    queries = build_metric_queries(descriptors)
    resources_by_id = {d.resource_id: d for d in descriptors}
    cw_client = get_cloudwatch_client(region=region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=payload.lookback_days)
    period_seconds = period_seconds_for(payload.lookback_days)

    try:
        results = await asyncio.to_thread(
            batch_query,
            cw_client,
            queries,
            start=start,
            end=end,
            period_seconds=period_seconds,
        )
    except CloudWatchError as exc:
        # A region-wide CloudWatch failure still preserves Phase 1
        # resource metadata via the warnings list.
        warnings.append(
            Warning(
                source="cloudwatch",
                service="cloudwatch",
                code=exc.code,
                message=f"CloudWatch utilization was not available: {exc.message}",
            )
        )
        return UtilizationResponse(
            region=region,
            lookback_days=payload.lookback_days,
            resources=[],
            warnings=warnings,
        ).model_dump(mode="json")
    except (ClientError, BotoCoreError) as exc:
        warnings.append(
            Warning(
                source="cloudwatch",
                service="cloudwatch",
                code=type(exc).__name__,
                message="CloudWatch utilization was not available.",
            )
        )
        return UtilizationResponse(
            region=region,
            lookback_days=payload.lookback_days,
            resources=[],
            warnings=warnings,
        ).model_dump(mode="json")

    series = aggregate_results(
        results,
        queries=queries,
        resources_by_id=resources_by_id,
        lookback_days=payload.lookback_days,
        period_seconds=period_seconds,
    )

    # Group series by resource so the response is per-resource, not
    # per-metric.
    by_resource: Dict[str, List[Any]] = {}
    for s in series:
        by_resource.setdefault(s.resource_id, []).append(s)

    resource_utils: List[ResourceUtilization] = []
    for desc in descriptors:
        metric_series_list = by_resource.get(desc.resource_id, [])
        # Resource-level data_quality is the worst (lowest confidence)
        # of any metric series; if no series, default no_data.
        if metric_series_list:
            order = {"high": 0, "medium": 1, "low": 2, "no_data": 3}
            worst = min(
                metric_series_list,
                key=lambda s: order.get(s.data_quality.value, 4),
            )
            overall = worst.data_quality
        else:
            overall = __import__("app.services.aws.cloudwatch_metrics", fromlist=["DataQuality"]).DataQuality.NO_DATA
        resource_utils.append(
            ResourceUtilization(
                resource_id=desc.resource_id,
                resource_type=desc.resource_type,
                service=_type_for_resource(resources_by_id, desc.resource_id),
                region=region,
                lookback_days=payload.lookback_days,
                metrics=[
                    MetricSeries(
                        namespace=m.namespace,
                        metric_name=m.metric_name,
                        statistic=m.statistic,
                        unit=m.unit,
                        datapoints=[
                            Datapoint(timestamp=d.timestamp, value=d.value)
                            for d in m.datapoints
                        ],
                        data_quality=m.data_quality,
                    )
                    for m in metric_series_list
                ],
                data_quality=overall,
            )
        )

    return UtilizationResponse(
        region=region,
        lookback_days=payload.lookback_days,
        resources=resource_utils,
        warnings=warnings,
    ).model_dump(mode="json")


__all__ = ["post_aws_utilization"]
