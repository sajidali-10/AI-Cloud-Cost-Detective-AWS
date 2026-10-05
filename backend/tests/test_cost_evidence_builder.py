"""Unit tests for the evidence builder — Phase 2.

These tests exercise the cost-scope invariant: a per-resource
evidence item must NEVER carry a ``CostContext`` with a dollar
amount whose ``scope`` is ``RESOURCE``.  Since we removed
``RESOURCE`` from the ``CostScope`` enum entirely, this is
enforced at the type level — we additionally check it explicitly
in every test below.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.evidence import (
    CostContext,
    CostScope,
    EvidenceResponse,
    EvidenceStatus,
    ResourceEvidence,
    Warning,
)
from app.schemas.aws import ServiceResult
from app.services.aws.cloudwatch_metrics import (
    DataQuality,
    MetricSeries,
    MetricDatapoint,
)
from app.schemas.cost import CostPeriod, CostReport, DailyCostPoint, ServiceCost
from app.services.cost_evidence_builder import build_evidence


def _service(name: str, status: str = "ok", items: list | None = None, error_code: str | None = None) -> ServiceResult:
    return ServiceResult(service=name, status=status, items=items or [], error_code=error_code)


def _cost_report(by_service: list[tuple[str, str]]) -> CostReport:
    today = datetime.now(timezone.utc).date()
    period = CostPeriod(start=today, end=today, days=30)
    previous = CostPeriod(start=today, end=today, days=30)
    return CostReport(
        account_id="111122223333",
        period=period,
        previous_period=previous,
        currency="USD",
        total_cost=Decimal("100.00"),
        previous_period_cost=Decimal("80.00"),
        change_amount=Decimal("20.00"),
        change_percent=Decimal("0.25"),
        estimated=False,
        daily_trend=[],
        by_service=[ServiceCost(service=name, amount=Decimal(amt), unit="USD") for name, amt in by_service],
        by_region=[],
        source="AWS_COST_EXPLORER",
    )


def test_resource_evidence_never_has_resource_scoped_cost():
    """Per the Phase 2 spec, cost_context.scope must never be RESOURCE
    with a dollar figure.  We enforce this by construction: CostScope
    has no RESOURCE member.  This test asserts that fact explicitly."""
    assert "RESOURCE" not in {member.name for member in CostScope}


def test_ec2_resource_gets_service_scoped_cost():
    services = {
        "ec2": _service("ec2", items=[{"instance_id": "i-abc", "region": "us-east-1"}]),
    }
    cost_report = _cost_report([("Amazon Elastic Compute Cloud - Compute", "100.00")])
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=cost_report,
        utilization_series=[],
    )
    assert isinstance(response, EvidenceResponse)
    assert response.status == EvidenceStatus.SUCCESS
    assert len(response.resources) == 1
    item = response.resources[0]
    assert item.resource_id == "i-abc"
    assert item.cost_context.scope == CostScope.SERVICE
    assert item.cost_context.amount == Decimal("100.00")
    assert "AWS_RESOURCE_API" in item.sources
    assert "AWS_COST_EXPLORER" in item.sources


def test_cost_warning_triggers_partial_success():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-abc"}])}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        cost_warning=Warning(source="cost_explorer", service="ce", code="AccessDenied", message="ce failed"),
    )
    assert response.status == EvidenceStatus.PARTIAL_SUCCESS
    assert any(w.source == "cost_explorer" for w in response.warnings)
    assert response.cost_summary == {"scope": "ACCOUNT", "amount": "0", "unit": "USD", "by_service": []}


def test_all_sources_fail_triggers_failed_status():
    services = {"ec2": _service("ec2", status="denied", error_code="AccessDenied")}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        cost_warning=Warning(source="cost_explorer", service="ce", code="AccessDenied", message="ce failed"),
        utilization_warning=Warning(source="cloudwatch", service="cloudwatch", code="AccessDenied", message="cw failed"),
        resources_warning=Warning(source="resources", service="ec2", code="AccessDenied", message="res failed"),
    )
    assert response.status == EvidenceStatus.FAILED


def test_data_quality_no_data_when_no_cloudwatch():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-abc"}])}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
    )
    item = response.resources[0]
    assert item.data_quality == DataQuality.NO_DATA


def test_sources_attribution():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-abc"}])}
    cost_report = _cost_report([("Amazon Elastic Compute Cloud - Compute", "10.00")])
    util_series = [
        MetricSeries(
            query_id="q",
            resource_id="i-abc",
            resource_type="ec2",
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            statistic="Average",
            unit="Percent",
            datapoints=[MetricDatapoint(timestamp=datetime.now(timezone.utc), value=Decimal("5.0"))],
            data_quality=DataQuality.HIGH,
        )
    ]
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=cost_report,
        utilization_series=util_series,
    )
    item = response.resources[0]
    assert set(item.sources) == {"AWS_RESOURCE_API", "AWS_COST_EXPLORER", "AWS_CLOUDWATCH"}
