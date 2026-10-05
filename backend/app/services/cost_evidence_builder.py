"""Evidence builder — Phase 2.

Joins three data sources into a deterministic per-resource evidence
model:

* Phase 1 resource metadata (EC2 / RDS / Lambda / ALB / NLB / ...)
* Cost Explorer ``by_service`` aggregate (gives SERVICE-scope spend
  per AWS service name)
* CloudWatch utilization series (per resource, with data_quality)

The output contract is intentionally strict: per-resource items
carry a ``CostContext`` whose ``scope`` is always one of
``ACCOUNT | SERVICE | REGION``.  We NEVER report a per-resource
dollar figure because Cost Explorer does not give us one.

Per-source failures surface as ``Warning`` rows in the response so
a CloudWatch outage does not destroy valid Cost Explorer data and
vice versa.  The overall status is computed from the warning count:

* ``SUCCESS`` — no warnings.
* ``PARTIAL_SUCCESS`` — at least one source returned data and at
  least one source failed.
* ``FAILED`` — every source failed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.services.aws.cloudwatch_metrics import DataQuality, MetricSeries, MetricQuery, ResourceDescriptor
from app.services.aws.cost_explorer import CostReport
from app.schemas.evidence import (
    CostContext,
    CostScope,
    EvidenceResponse,
    EvidenceStatus,
    ResourceEvidence,
    Warning,
)

logger = logging.getLogger("cost-detective.cost_evidence_builder")


def _service_name_for_resource(resource_type: str) -> str:
    """Map our resource_type slug to the AWS service name as Cost
    Explorer reports it (canonical Phase 2 mapping)."""
    return {
        "ec2": "Amazon Elastic Compute Cloud - Compute",
        "rds": "Amazon Relational Database Service",
        "lambda": "AWS Lambda",
        "alb": "Elastic Load Balancing",
        "nlb": "Elastic Load Balancing",
    }.get(resource_type, resource_type)


def build_evidence(
    *,
    region: str,
    days: int,
    phase1_services: Dict[str, Any],
    cost_report: Optional[CostReport],
    utilization_series: List[MetricSeries],
    cost_warning: Optional[Warning] = None,
    utilization_warning: Optional[Warning] = None,
    resources_warning: Optional[Warning] = None,
) -> EvidenceResponse:
    """Compose an ``EvidenceResponse`` from the three sources."""
    # Index cost-by-service so we can look up the SERVICE-scope spend
    # for each resource.
    service_spend: Dict[str, Decimal] = {}
    total_spend = Decimal("0")
    if cost_report is not None:
        for row in cost_report.by_service:
            service_spend[str(row.service)] = Decimal(str(row.amount))
            total_spend += Decimal(str(row.amount))

    # Index CloudWatch series by resource id.
    util_by_resource: Dict[str, List[MetricSeries]] = {}
    overall_quality: Dict[str, str] = {"worst": "high"}
    for s in utilization_series:
        util_by_resource.setdefault(s.resource_id, []).append(s)

    # Iterate over every resource Phase 1 reported (use EC2 as the
    # canonical "I have a resource" indicator; Phase 1 enumerators
    # attach a ``resource_id`` regardless of service).
    resources: List[ResourceEvidence] = []
    from app.services.aws.resources import enumerate_all_services  # local import to avoid cycle
    # We expect ``phase1_services`` to be the output of
    # ``enumerate_all_services(region)`` — but the route handler
    # already calls it; the builder only consumes the normalized rows.
    for svc_name, svc_result in phase1_services.items():
        if getattr(svc_result, "status", None) != "ok":
            continue
        for item in svc_result.items or []:
            resource_id, resource_type = _extract_resource_id(svc_name, item)
            if resource_id is None or resource_type is None:
                continue
            service_name = _service_name_for_resource(resource_type)
            amount = service_spend.get(service_name, Decimal("0"))
            series = util_by_resource.get(resource_id, [])
            if series:
                # Resource-level data_quality: take the worst metric.
                order = {"high": 0, "medium": 1, "low": 2, "no_data": 3}
                worst = min(series, key=lambda s: order.get(s.data_quality.value, 4))
                rq = worst.data_quality
            else:
                rq = DataQuality.NO_DATA
            sources = ["AWS_RESOURCE_API"]
            if amount > 0:
                sources.append("AWS_COST_EXPLORER")
            if series:
                sources.append("AWS_CLOUDWATCH")
            resources.append(
                ResourceEvidence(
                    resource_id=resource_id,
                    resource_type=resource_type,
                    service=service_name,
                    region=region,
                    utilization={
                        "series": [
                            {
                                "namespace": s.namespace,
                                "metric_name": s.metric_name,
                                "statistic": s.statistic,
                                "unit": s.unit,
                                "datapoints": [
                                    {"timestamp": d.timestamp.isoformat(), "value": str(d.value)}
                                    for d in s.datapoints
                                ],
                                "data_quality": s.data_quality.value,
                            }
                            for s in series
                        ],
                    },
                    cost_context=CostContext(
                        scope=CostScope.SERVICE,
                        service=service_name,
                        region=region,
                        amount=amount,
                        unit="USD",
                    ),
                    data_quality=rq,
                    sources=sources,
                )
            )

    # Status computation.
    warnings: List[Warning] = []
    if cost_warning is not None:
        warnings.append(cost_warning)
    if utilization_warning is not None:
        warnings.append(utilization_warning)
    if resources_warning is not None:
        warnings.append(resources_warning)
    failed_sources = sum(
        1
        for w in warnings
        if w.source in ("cost_explorer", "cloudwatch", "resources")
    )
    if failed_sources == 0:
        status = EvidenceStatus.SUCCESS
    elif resources and failed_sources < 3:
        status = EvidenceStatus.PARTIAL_SUCCESS
    else:
        status = EvidenceStatus.FAILED

    cost_summary = {
        "scope": "ACCOUNT",
        "amount": str(total_spend),
        "unit": "USD",
        "by_service": [
            {"service": k, "amount": str(v), "unit": "USD"}
            for k, v in sorted(service_spend.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }

    return EvidenceResponse(
        region=region,
        days=days,
        status=status,
        cost_summary=cost_summary,
        resources=resources,
        warnings=warnings,
    )


def _extract_resource_id(svc_name: str, item: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract (resource_id, resource_type_slug) from a Phase 1 row."""
    if svc_name == "ec2":
        return item.get("instance_id"), "ec2"
    if svc_name == "rds":
        return item.get("db_instance_identifier"), "rds"
    if svc_name == "lambda":
        return item.get("function_name"), "lambda"
    if svc_name == "elbv2":
        lb_type = item.get("type")
        if lb_type not in ("application", "network"):
            return None, None
        return item.get("arn"), "alb" if lb_type == "application" else "nlb"
    return None, None


__all__ = ["build_evidence"]
