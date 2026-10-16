"""Phase 7C.1 — AI evidence provider attribution tests.

Pins the contract added in Phase 7C.1:

* ``ResourceEvidence.provider`` and ``ResourceEvidence.account_id``
  are always present (default ``'AWS'`` / ``None``).
* ``EvidenceResponse.provider`` and ``EvidenceResponse.account_id``
  are always present (default ``'AWS'`` / ``None``).
* The builder accepts the optional ``provider`` and ``account_id``
  kwargs and threads them into every emitted record.
* Existing Phase 2 callers (no kwargs) still produce schemas that
  pass validation, so the backwards-compat invariant is intact.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.schemas.aws import ServiceResult
from app.schemas.cost import CostPeriod, CostReport, ServiceCost
from app.schemas.evidence import EvidenceStatus
from app.services.cost_evidence_builder import build_evidence


def _service(name: str, items: list | None = None) -> ServiceResult:
    return ServiceResult(service=name, status="ok", items=items or [], error_code=None)


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
        by_service=[
            ServiceCost(service=name, amount=Decimal(amt), unit="USD")
            for name, amt in by_service
        ],
        by_region=[],
        source="AWS_COST_EXPLORER",
    )


def test_evidence_defaults_provider_to_aws():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-1"}])}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
    )
    assert response.provider == "AWS"
    assert response.account_id is None
    item = response.resources[0]
    assert item.provider == "AWS"
    assert item.account_id is None


def test_evidence_threads_provider_and_account_id():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-1"}])}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        provider="AWS",
        account_id="111122223333",
    )
    assert response.provider == "AWS"
    assert response.account_id == "111122223333"
    item = response.resources[0]
    assert item.provider == "AWS"
    assert item.account_id == "111122223333"


def test_evidence_accepts_azure_provider_for_future_adapters():
    """Phase 7C.1 does not emit Azure evidence (no adapter), but the
    builder accepts a non-AWS provider string for forward
    compatibility.  The Pydantic model accepts any string."""
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-1"}])}
    response = build_evidence(
        region="eastus",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
    )
    assert response.provider == "AZURE"
    item = response.resources[0]
    assert item.provider == "AZURE"


def test_evidence_account_id_overrides_item_when_item_missing():
    """If a Phase 1 row does not carry an ``account_id`` (the legacy
    Phase 2 shape), the builder falls back to the top-level kwarg so
    the AI always sees an attribution."""
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-1"}])}
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        provider="AWS",
        account_id="222233334444",
    )
    assert response.resources[0].account_id == "222233334444"


def test_evidence_item_account_id_preferred_over_top_level():
    """When a Phase 1 row already carries its own ``account_id``,
    the row's value wins so a per-row attribution is preserved."""
    services = {
        "ec2": _service(
            "ec2",
            items=[
                {
                    "instance_id": "i-1",
                    "account_id": "row-specific-acct",
                }
            ],
        )
    }
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=None,
        utilization_series=[],
        provider="AWS",
        account_id="top-level-acct",
    )
    assert response.account_id == "top-level-acct"
    # Per-row wins.
    assert response.resources[0].account_id == "row-specific-acct"


def test_evidence_top_level_attribution_with_full_cost_report():
    services = {"ec2": _service("ec2", items=[{"instance_id": "i-1"}])}
    cost_report = _cost_report([("Amazon Elastic Compute Cloud - Compute", "10.00")])
    response = build_evidence(
        region="us-east-1",
        days=30,
        phase1_services=services,
        cost_report=cost_report,
        utilization_series=[],
        provider="AWS",
        account_id="111122223333",
    )
    assert response.provider == "AWS"
    assert response.account_id == "111122223333"
    assert response.status == EvidenceStatus.SUCCESS
    assert response.resources[0].provider == "AWS"
    assert response.resources[0].account_id == "111122223333"
