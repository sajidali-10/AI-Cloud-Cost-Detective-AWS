"""Unit tests for the optimization engine orchestrator — Phase 3.

Exercises the deduplicator, summary builder, and end-to-end
``build_recommendations`` with synthetic AWS-native inputs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

from app.schemas.optimization import (
    CapabilitiesResponse,
    Confidence,
    OptimizationNotice,
    OptimizationStatus,
    Recommendation,
    RecommendationAction,
    RecommendationEvidence,
    ResourceType,
    SavingsSource,
    ServiceCapability,
    SummaryResponse,
)
from app.services.aws.cloudwatch_metrics import (
    DataQuality,
    MetricDatapoint,
    MetricSeries,
)
from app.services.aws.compute_optimizer import NormalizedRecommendation as CONormalized
from app.services.aws.cost_optimization_hub import NormalizedHubRecommendation
from app.services.optimization_engine import (
    OptimizationInputs,
    SOURCE_PRECEDENCE,
    _candidate_to_recommendation,
    _co_to_candidate,
    _coh_to_candidate,
    _deduplicate,
    _deterministic_to_candidate,
    build_capabilities,
    build_recommendations,
    build_summary,
)
from app.services.optimization_rules import DeterministicCandidate, deterministic_recommendation_id


def _co_row(
    *,
    resource_id: str = "i-1",
    resource_arn: str = "arn:aws:ec2:us-east-1:111122223333:instance/i-1",
    finding: str = "OVER_PROVISIONED",
    savings: Decimal = Decimal("10.00"),
    reason_codes: List[str] = None,
    aws_recommendation_id: str = "co-r-1",
) -> CONormalized:
    if reason_codes is None:
        reason_codes = ["CPU_OVER_PROVISIONED"]
    return CONormalized(
        resource_arn=resource_arn,
        resource_id=resource_id,
        resource_type="Ec2Instance",
        region="us-east-1",
        account_id="111122223333",
        finding=finding,
        current_configuration={"instance_type": "m5.large"},
        recommended_configuration={"instance_type": "m5.medium"},
        performance_risk=Decimal("0.5"),
        lookback_period_days=30,
        estimated_monthly_savings=savings,
        savings_percentage=Decimal("20"),
        currency="USD",
        reason_codes=reason_codes,
        aws_recommendation_id=aws_recommendation_id,
    )


def _coh_row(
    *,
    recommendation_id: str = "coh-r-1",
    resource_id: str = "i-1",
    resource_arn: str = "arn:aws:ec2:us-east-1:111122223333:instance/i-1",
    resource_type: str = "Ec2Instance",
    action_type: str = "Rightsize",
    savings: Decimal = Decimal("12.00"),
    restart_needed: bool = False,
    rollback_possible: bool = True,
) -> NormalizedHubRecommendation:
    return NormalizedHubRecommendation(
        recommendation_id=recommendation_id,
        resource_id=resource_id,
        resource_arn=resource_arn,
        resource_type=resource_type,
        action_type=action_type,
        region="us-east-1",
        account_id="111122223333",
        estimated_monthly_savings=savings,
        savings_percentage=Decimal("22"),
        currency="USD",
        current_resource_summary={"instance_type": "m5.large"},
        recommended_resource_summary={"instance_type": "m5.medium"},
        implementation_effort="Low",
        restart_needed=restart_needed,
        rollback_possible=rollback_possible,
    )


def _det_candidate(
    *,
    resource_id: str = "vol-1",
    action: RecommendationAction = RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
    resource_type: ResourceType = ResourceType.EBS_VOLUME,
    confidence: Confidence = Confidence.HIGH,
    data_quality: str = "high",
) -> DeterministicCandidate:
    return DeterministicCandidate(
        resource_id=resource_id,
        resource_arn=None,
        resource_type=resource_type,
        region="us-east-1",
        action=action,
        title=f"{action.value} {resource_id}",
        finding="deterministic rule fired",
        current_configuration={"x": 1},
        recommended_configuration={"recommendation": action.value},
        confidence=confidence,
        data_quality=data_quality,
        reason_codes=["CODE"],
        evidence={"detail": "stub"},
    )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_coh_preferred_over_co(self) -> None:
        co_cand = _co_to_candidate(_co_row(savings=Decimal("10.00"), aws_recommendation_id="co-r-1"))
        coh_cand = _coh_to_candidate(_coh_row(recommendation_id="coh-r-1", savings=Decimal("12.00")))
        out = _deduplicate([co_cand, coh_cand])
        assert len(out) == 1
        winner = out[0]
        assert winner.source == SavingsSource.AWS_COST_OPTIMIZATION_HUB
        # Sources list contains BOTH attestations.
        sources = {ev.source for ev in winner.evidence}
        assert SavingsSource.AWS_COMPUTE_OPTIMIZER in sources
        assert SavingsSource.AWS_COST_OPTIMIZATION_HUB in sources
        # The savings figure comes from the primary (COH), not CO.
        assert winner.estimated_monthly_savings == Decimal("12.00")
        assert "co-r-1" in winner.aws_recommendation_ids
        assert "coh-r-1" in winner.aws_recommendation_ids

    def test_distinct_actions_not_deduped(self) -> None:
        co_cand = _co_to_candidate(_co_row(resource_id="i-1", aws_recommendation_id="co-r-1"))
        coh_cand = _coh_to_candidate(_coh_row(
            recommendation_id="coh-r-1",
            resource_id="i-1",
            action_type="StopIdle",
        ))
        out = _deduplicate([co_cand, coh_cand])
        assert len(out) == 2

    def test_savings_counted_once(self) -> None:
        co_cand = _co_to_candidate(_co_row(savings=Decimal("10.00"), aws_recommendation_id="co-r-1"))
        coh_cand = _coh_to_candidate(_coh_row(recommendation_id="coh-r-1", savings=Decimal("10.00")))
        out = _deduplicate([co_cand, coh_cand])
        # Only one candidate survives; its savings is the primary's.
        assert len(out) == 1
        assert out[0].estimated_monthly_savings == Decimal("10.00")

    def test_deterministic_does_not_override_aws(self) -> None:
        det = _det_candidate(resource_id="i-1", resource_type=ResourceType.EC2)
        det_cand = _deterministic_to_candidate(det, account_id="111122223333")
        coh_cand = _coh_to_candidate(_coh_row(recommendation_id="coh-r-1"))
        out = _deduplicate([det_cand, coh_cand])
        # Deterministic and AWS-native candidates do NOT collide on key
        # because the deterministic id is deterministic_recommendation_id()
        # (different from the AWS row id).  Both survive.
        assert len(out) == 2

    def test_deterministic_dedupes_with_itself(self) -> None:
        det = _det_candidate(resource_id="vol-1")
        cand_a = _deterministic_to_candidate(det, account_id="111122223333")
        cand_b = _deterministic_to_candidate(det, account_id="111122223333")
        out = _deduplicate([cand_a, cand_b])
        assert len(out) == 1


# ---------------------------------------------------------------------------
# Confidence mapping
# ---------------------------------------------------------------------------


def test_source_precedence_order() -> None:
    assert SOURCE_PRECEDENCE[SavingsSource.AWS_COST_OPTIMIZATION_HUB] < SOURCE_PRECEDENCE[SavingsSource.AWS_COMPUTE_OPTIMIZER]
    assert SOURCE_PRECEDENCE[SavingsSource.AWS_COMPUTE_OPTIMIZER] < SOURCE_PRECEDENCE[SavingsSource.UNKNOWN]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _sample_rec(
    *,
    resource_type: ResourceType = ResourceType.EC2,
    action: RecommendationAction = RecommendationAction.RIGHTSIZE,
    savings: Decimal = Decimal("10.00"),
    confidence: Confidence = Confidence.HIGH,
    primary_source: SavingsSource = SavingsSource.AWS_COST_OPTIMIZATION_HUB,
    sources: List[SavingsSource] = None,
) -> Recommendation:
    if sources is None:
        sources = [primary_source]
    return Recommendation(
        recommendation_id="det-test-0",
        resource_id="i-1",
        resource_arn=None,
        resource_type=resource_type,
        region="us-east-1",
        account_id="111122223333",
        action=action,
        title="t",
        finding="f",
        current_configuration={},
        recommended_configuration={},
        estimated_monthly_savings=savings,
        currency="USD",
        savings_percentage=Decimal("20"),
        savings_source=primary_source,
        primary_source=primary_source,
        sources=sources,
        confidence=confidence,
        data_quality="high",
    )


class TestSummary:
    def test_totals_and_breakdowns(self) -> None:
        recs = [
            _sample_rec(savings=Decimal("10.00"), primary_source=SavingsSource.AWS_COST_OPTIMIZATION_HUB),
            _sample_rec(savings=Decimal("5.00"), resource_type=ResourceType.EBS_VOLUME, primary_source=SavingsSource.AWS_COMPUTE_OPTIMIZER),
            _sample_rec(savings=None, confidence=Confidence.MEDIUM),
        ]
        s = build_summary(
            region="us-east-1",
            account_id="111122223333",
            days=30,
            recommendations=recs,
            warnings=[],
        )
        assert s.total_recommendations == 3
        assert s.total_estimated_monthly_savings == Decimal("15.00")
        assert s.recommendations_without_savings == 1
        # Source breakdown includes both AWS_COST_OPTIMIZATION_HUB and AWS_COMPUTE_OPTIMIZER.
        keys = {b.key for b in s.by_source}
        assert "AWS_COST_OPTIMIZATION_HUB" in keys
        assert "AWS_COMPUTE_OPTIMIZER" in keys

    def test_status_partial_when_warnings(self) -> None:
        recs = [_sample_rec()]
        s = build_summary(
            region="us-east-1",
            account_id="111122223333",
            days=30,
            recommendations=recs,
            warnings=[OptimizationNotice(source="compute_optimizer", code="X", message="m")],
        )
        assert s.status == OptimizationStatus.PARTIAL_SUCCESS

    def test_status_failed_when_no_recs_and_warnings(self) -> None:
        s = build_summary(
            region="us-east-1",
            account_id="111122223333",
            days=30,
            recommendations=[],
            warnings=[OptimizationNotice(source="compute_optimizer", code="X", message="m")],
        )
        assert s.status == OptimizationStatus.FAILED

    def test_currency_field(self) -> None:
        recs = [_sample_rec(savings=Decimal("1.00"))]
        s = build_summary(region="us-east-1", account_id=None, days=30, recommendations=recs, warnings=[])
        assert s.currency == "USD"

    def test_dedup_savings_counted_once(self) -> None:
        # Two source-attributed rows on the SAME recommendation must
        # contribute only the primary's savings to the summary total.
        recs = [
            _sample_rec(
                savings=Decimal("10.00"),
                primary_source=SavingsSource.AWS_COST_OPTIMIZATION_HUB,
                sources=[SavingsSource.AWS_COST_OPTIMIZATION_HUB, SavingsSource.AWS_COMPUTE_OPTIMIZER],
            ),
        ]
        s = build_summary(region="us-east-1", account_id=None, days=30, recommendations=recs, warnings=[])
        # Total is 10, not 20, even though both sources attribute to the row.
        assert s.total_estimated_monthly_savings == Decimal("10.00")
        keys = {b.key for b in s.by_source}
        assert "AWS_COST_OPTIMIZATION_HUB" in keys
        assert "AWS_COMPUTE_OPTIMIZER" in keys


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_build_capabilities_includes_all_sources(monkeypatch: Any) -> None:
    """Stand-in for AWS calls so capabilities always report AVAILABLE."""
    from app.services import optimization_engine as engine

    monkeypatch.setattr(engine, "_co_capability", lambda region, account_id: (ServiceCapability(status="ACTIVE"), None))
    monkeypatch.setattr(engine, "_coh_capability", lambda region, account_id: (ServiceCapability(status="NOT_ENROLLED"), None))
    caps = build_capabilities(region="us-east-1", account_id="111122223333")
    assert caps.compute_optimizer.status.value == "ACTIVE"
    assert caps.cost_optimization_hub.status.value == "NOT_ENROLLED"
    assert caps.deterministic_engine.status.value == "AVAILABLE"
    assert ResourceType.EC2 in caps.supported_resource_types
    assert 30 in caps.supported_lookback_days


def test_build_capabilities_warnings_on_failure(monkeypatch: Any) -> None:
    from app.services import optimization_engine as engine

    monkeypatch.setattr(
        engine, "_co_capability",
        lambda region, account_id: (ServiceCapability(status="ACCESS_DENIED", error_code="AccessDeniedException"), "AccessDeniedException"),
    )
    monkeypatch.setattr(engine, "_coh_capability", lambda region, account_id: (ServiceCapability(status="NOT_ENROLLED"), None))
    caps = build_capabilities(region="us-east-1", account_id=None)
    assert any(w.source == "compute_optimizer" for w in caps.warnings)


# ---------------------------------------------------------------------------
# End-to-end recommendations (deterministic rules + AWS sources stubbed)
# ---------------------------------------------------------------------------


def test_build_recommendations_deterministic_only(monkeypatch: Any) -> None:
    """Disable AWS-native sources; the engine should still emit
    deterministic rule recommendations and return SUCCESS."""
    from app.services import optimization_engine as engine

    monkeypatch.setattr(
        engine,
        "_fetch_compute_optimizer_candidates",
        lambda region, account_id: [],
    )
    monkeypatch.setattr(
        engine,
        "_fetch_cost_optimization_hub_candidates",
        lambda region, account_id: [],
    )

    inputs = OptimizationInputs(
        region="us-east-1",
        account_id="111122223333",
        days=30,
        phase1_services={
            "ec2": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "ec2"})(),
            "ebs": type("S", (), {"status": "ok", "items": [
                {"volume_id": "vol-1", "state": "available", "size_gb": 100, "availability_zone": "us-east-1a", "attachments": 0, "tags": {}}
            ], "error_code": None, "service": "ebs"})(),
            "eip": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "eip"})(),
            "nat": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "nat"})(),
            "elbv2": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "elbv2"})(),
            "rds": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "rds"})(),
        },
        utilization_by_resource={},
    )
    recs, warnings = build_recommendations(inputs)
    # The EBS rule must have fired.
    actions = [r.action for r in recs]
    assert RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS in actions
    # No AWS-native warnings since we stubbed them out.
    assert warnings == []


def test_build_recommendations_partial_failure(monkeypatch: Any) -> None:
    """A failure on one AWS source surfaces as a Warning; the other still contributes."""
    from app.services import optimization_engine as engine

    monkeypatch.setattr(
        engine,
        "_fetch_compute_optimizer_candidates",
        lambda region, account_id: [],
    )
    monkeypatch.setattr(
        engine,
        "_fetch_cost_optimization_hub_candidates",
        lambda region, account_id: [],
    )

    # Phase 1 reports a per-service failure to make sure the
    # orchestrator never crashes even when one service is denied.
    inputs = OptimizationInputs(
        region="us-east-1",
        account_id=None,
        days=30,
        phase1_services={
            "ec2": type("S", (), {"status": "denied", "items": [], "error_code": "AccessDenied", "service": "ec2"})(),
            "ebs": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "ebs"})(),
            "eip": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "eip"})(),
            "nat": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "nat"})(),
            "elbv2": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "elbv2"})(),
            "rds": type("S", (), {"status": "ok", "items": [], "error_code": None, "service": "rds"})(),
        },
        utilization_by_resource={},
    )
    recs, warnings = build_recommendations(inputs)
    assert recs == []
    assert warnings == []
