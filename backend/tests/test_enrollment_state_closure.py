"""Phase 3 enrollment-state closure regression tests.

These tests pin the behavior required by the Phase 3 closure:

* Compute Optimizer ``Inactive`` enrollment maps to
  ``CapabilityStatus.INACTIVE`` (NOT ``UNAVAILABLE``).
* Cost Optimization Hub empty enrollment list maps to
  ``CapabilityStatus.NOT_ENROLLED``.
* When Compute Optimizer is INACTIVE / PENDING / FAILED, the
  orchestrator MUST NOT call any of the CO recommendation APIs
  (``GetEC2InstanceRecommendations``, ``GetEBSVolumeRecommendations``,
  ``GetLambdaFunctionRecommendations``,
  ``GetRDSDatabaseRecommendations``).
* When Cost Optimization Hub is INACTIVE / NOT_ENROLLED / PENDING /
  FAILED, the orchestrator MUST NOT call ``ListRecommendations``,
  ``ListRecommendationSummaries``, or ``GetRecommendation``.
* No ``AccessDeniedException`` / ``ComputeOptimizerError`` /
  ``CostOptimizationHubError`` warnings are emitted for expected
  non-active enrollment states.
* The response status stays ``SUCCESS`` (NOT ``PARTIAL_SUCCESS``)
  when the only AWS-native sources are in an expected non-active
  state and the deterministic rule engine emits candidates.
* Deterministic rule recommendations are still emitted unchanged.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from app.schemas.optimization import (
    CapabilityStatus,
    OptimizationStatus,
    RecommendationAction,
    ResourceType,
    SavingsSource,
)
from app.services import optimization_engine as engine
from app.services.optimization_engine import (
    OptimizationInputs,
    build_capabilities,
    build_recommendations,
    build_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _det_inputs(
    *,
    ebs_volumes: List[Dict[str, Any]] = None,
) -> OptimizationInputs:
    """Return an ``OptimizationInputs`` that fires deterministic rules.

    Defaults to one unattached EBS volume so the deterministic engine
    emits at least one REVIEW_DELETE_UNATTACHED_EBS candidate.
    """
    if ebs_volumes is None:
        ebs_volumes = [
            {
                "volume_id": "vol-det-1",
                "state": "available",
                "size_gb": 100,
                "availability_zone": "us-east-1a",
                "attachments": 0,
                "tags": {},
            }
        ]
    return OptimizationInputs(
        region="us-east-1",
        account_id="111122223333",
        days=30,
        phase1_services={
            "ec2": _FakeService("ec2", []),
            "ebs": _FakeService("ebs", ebs_volumes),
            "eip": _FakeService("eip", []),
            "nat": _FakeService("nat", []),
            "elbv2": _FakeService("elbv2", []),
            "rds": _FakeService("rds", []),
        },
        utilization_by_resource={},
    )


class _FakeService:
    """Minimal duck-typed stand-in for Phase 1 service summaries."""

    def __init__(self, name: str, items: List[Dict[str, Any]]) -> None:
        self.service = name
        self.status = "ok"
        self.items = items
        self.error_code = None


def _fake_aws_client(*, op_pages: Dict[str, Dict[str, Any]]) -> MagicMock:
    """Return a MagicMock that mimics the Boto3 client.

    ``op_pages[op_name]`` is the canned response payload for that op.
    Every call is recorded so a test can assert the AWS client was
    NEVER touched for a specific operation.
    """
    client = MagicMock()
    recorded: List[tuple[str, Dict[str, Any]]] = []

    def _make(op_name: str) -> Any:
        def _method(**kwargs: Any) -> Dict[str, Any]:
            recorded.append((op_name, dict(kwargs)))
            return op_pages.get(op_name, {})

        return _method

    for op in (
        "get_enrollment_status",
        "list_enrollment_statuses",
        "get_recommendation_summaries",
        "get_ec2_instance_recommendations",
        "get_ebs_volume_recommendations",
        "get_lambda_function_recommendations",
        "get_rds_database_recommendations",
        "list_recommendations",
        "list_recommendation_summaries",
        "get_recommendation",
        "get_preferences",
    ):
        setattr(client, op, _make(op))
    client._recorded = recorded  # type: ignore[attr-defined]
    return client


def _assert_op_not_called(client: MagicMock, op_name: str) -> None:
    """Assert the AWS op was never called.

    ``_recorded`` carries a tuple ``(op_name, kwargs)`` for every
    invocation; we walk it to guarantee the recommendation APIs
    stayed untouched.
    """
    seen = [r[0] for r in client._recorded]
    assert op_name not in seen, (
        f"expected {op_name!r} NOT to be called, but it was invoked "
        f"{seen.count(op_name)} time(s).  All recorded ops: {seen!r}"
    )


# ---------------------------------------------------------------------------
# 1. Compute Optimizer Inactive -> INACTIVE
# ---------------------------------------------------------------------------


class TestComputeOptimizerInactiveMapsToINACTIVE:
    def test_inactive_status_returns_INACTIVE(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``Inactive`` from AWS must surface as ``CapabilityStatus.INACTIVE``."""
        client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: client)
        cap, err = engine._co_capability(region="us-east-1", account_id="111122223333")
        assert cap.status == CapabilityStatus.INACTIVE
        assert err is None

    def test_capabilities_endpoint_reflects_INACTIVE(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The capabilities endpoint reports INACTIVE when CO is Inactive."""
        co_client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        # Stub the COH client too so its real (unset) client isn't
        # invoked during the capabilities endpoint test — we only
        # care about CO reporting INACTIVE.
        coh_client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: co_client)
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: coh_client
        )
        caps = build_capabilities(region="us-east-1", account_id="111122223333")
        assert caps.compute_optimizer.status == CapabilityStatus.INACTIVE
        assert caps.warnings == []


# ---------------------------------------------------------------------------
# 2. CO recommendation APIs are skipped when Inactive
# ---------------------------------------------------------------------------


class TestCOApisSkippedWhenInactive:
    @pytest.mark.parametrize(
        "aws_status",
        ["Inactive", "Pending", "Failed"],
    )
    def test_co_recommendation_apis_not_called(
        self,
        monkeypatch: pytest.MonkeyPatch,
        aws_status: str,
    ) -> None:
        """Any expected non-active CO state must short-circuit before
        any ``Get*Recommendations`` call.
        """
        client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": aws_status}
                    ]
                }
            }
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: client)

        recs, warnings = build_recommendations(_det_inputs())
        # Deterministic rule still ran.
        assert any(r.action == RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS for r in recs)
        # No AWS CO fetcher was touched.
        _assert_op_not_called(client, "get_ec2_instance_recommendations")
        _assert_op_not_called(client, "get_ebs_volume_recommendations")
        _assert_op_not_called(client, "get_lambda_function_recommendations")
        _assert_op_not_called(client, "get_rds_database_recommendations")
        # And the response carries NO warnings.
        assert warnings == []


# ---------------------------------------------------------------------------
# 3. Empty COH enrollment list -> NOT_ENROLLED
# ---------------------------------------------------------------------------


class TestCOHEmptyEnrollmentMapsToNOT_ENROLLED:
    def test_empty_items_returns_NOT_ENROLLED(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AWS returns an empty ``items`` list when the account has
        never enrolled — surface that as ``NOT_ENROLLED`` on the
        public capabilities response.
        """
        client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: client
        )
        cap, err = engine._coh_capability(region="us-east-1", account_id="111122223333")
        assert cap.status == CapabilityStatus.NOT_ENROLLED
        assert err is None

    def test_capabilities_endpoint_reflects_NOT_ENROLLED(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coh_client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        # Stub the CO client too so the capabilities endpoint
        # doesn't fail on the CO enrollment call when we're focused
        # on the COH empty-items -> NOT_ENROLLED mapping.
        co_client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: coh_client
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: co_client)
        caps = build_capabilities(region="us-east-1", account_id="111122223333")
        assert caps.cost_optimization_hub.status == CapabilityStatus.NOT_ENROLLED
        assert caps.warnings == []


# ---------------------------------------------------------------------------
# 4. COH recommendation APIs are skipped when not enrolled
# ---------------------------------------------------------------------------


class TestCOHApisSkippedWhenNotEnrolled:
    @pytest.mark.parametrize(
        "aws_items",
        [
            [],  # empty items -> NOT_ENROLLED
            [{"accountId": "111122223333", "status": "Inactive"}],  # explicit Inactive
            [{"accountId": "111122223333", "status": "Pending"}],   # Pending
            [{"accountId": "111122223333", "status": "Failed"}],    # Failed
        ],
    )
    def test_coh_recommendation_apis_not_called(
        self,
        monkeypatch: pytest.MonkeyPatch,
        aws_items: List[Dict[str, Any]],
    ) -> None:
        """Any expected non-active COH state must short-circuit before
        any ``List*`` / ``Get*`` recommendation call.
        """
        client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": aws_items}}
        )
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: client
        )

        recs, warnings = build_recommendations(_det_inputs())
        # Deterministic rule still ran.
        assert any(r.action == RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS for r in recs)
        # No AWS COH fetcher was touched.
        _assert_op_not_called(client, "list_recommendations")
        _assert_op_not_called(client, "list_recommendation_summaries")
        _assert_op_not_called(client, "get_recommendation")
        # And the response carries NO warnings.
        assert warnings == []


# ---------------------------------------------------------------------------
# 5. No erroneous warnings for expected enrollment states
# ---------------------------------------------------------------------------


class TestNoErroneousWarnings:
    def test_co_inactive_emits_no_warnings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: client)
        recs, warnings = build_recommendations(_det_inputs())
        # No AccessDeniedException, no ComputeOptimizerError, no
        # cost_optimization_hub warnings.
        codes = {w.code for w in warnings}
        assert "AccessDeniedException" not in codes
        assert "ComputeOptimizerError" not in codes
        assert "CostOptimizationHubError" not in codes
        assert "AccessDenied" not in codes

    def test_coh_not_enrolled_emits_no_warnings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: client
        )
        recs, warnings = build_recommendations(_det_inputs())
        codes = {w.code for w in warnings}
        assert "AccessDeniedException" not in codes
        assert "ComputeOptimizerError" not in codes
        assert "CostOptimizationHubError" not in codes

    def test_both_sources_non_active_still_SUCCESS(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When every AWS-native source is in an expected non-active
        state but the deterministic engine fires, the response stays
        ``SUCCESS`` (NOT ``PARTIAL_SUCCESS``).
        """
        co_client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        coh_client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: co_client)
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: coh_client
        )

        recs, warnings = build_recommendations(_det_inputs())
        assert warnings == []
        summary = build_summary(
            region="us-east-1",
            account_id="111122223333",
            days=30,
            recommendations=recs,
            warnings=warnings,
        )
        assert summary.status == OptimizationStatus.SUCCESS
        assert summary.recommendations_without_savings >= 1


# ---------------------------------------------------------------------------
# 6. Deterministic recommendations still returned
# ---------------------------------------------------------------------------


class TestDeterministicStillRuns:
    def test_ebs_rule_still_fires_when_aws_sources_inactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        co_client = _fake_aws_client(
            op_pages={
                "get_enrollment_status": {
                    "accountEnrollmentStatuses": [
                        {"accountId": "111122223333", "status": "Inactive"}
                    ]
                }
            }
        )
        coh_client = _fake_aws_client(
            op_pages={"list_enrollment_statuses": {"items": []}}
        )
        monkeypatch.setattr(engine, "get_compute_optimizer_client", lambda region=None: co_client)
        monkeypatch.setattr(
            engine, "get_cost_optimization_hub_client", lambda region=None: coh_client
        )

        recs, _warnings = build_recommendations(_det_inputs())
        # The EBS rule fires regardless of AWS enrollment state.
        ebs = [r for r in recs if r.resource_type == ResourceType.EBS_VOLUME]
        assert len(ebs) >= 1
        assert ebs[0].action == RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS
        # Deterministic rule candidates carry UNKNOWN savings source.
        assert all(r.savings_source == SavingsSource.UNKNOWN for r in ebs)
        assert all(r.estimated_monthly_savings is None for r in ebs)
        # CO / COH did NOT contribute any candidates.
        assert all(
            SavingsSource.AWS_COMPUTE_OPTIMIZER not in r.sources
            and SavingsSource.AWS_COST_OPTIMIZATION_HUB not in r.sources
            for r in ebs
        )


# ---------------------------------------------------------------------------
# Defense-in-depth: real fetcher errors still surface as warnings
# ---------------------------------------------------------------------------


class TestRealFailuresStillWarn:
    def test_co_actual_failure_still_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real Compute Optimizer fetcher failure (NOT just an
        inactive enrollment) must still emit a warning so the
        response can flip to PARTIAL_SUCCESS.
        """

        # Force the capability to look ACTIVE so the gating passes,
        # then make the actual fetcher raise.
        monkeypatch.setattr(
            engine,
            "_co_capability",
            lambda region, account_id: (
                __import__(
                    "app.schemas.optimization", fromlist=["ServiceCapability"]
                ).ServiceCapability(status="ACTIVE"),
                None,
            ),
        )

        def _boom(*, region: str, account_id):
            raise __import__(
                "app.services.aws.compute_optimizer", fromlist=["ComputeOptimizerError"]
            ).ComputeOptimizerError(
                code="AccessDeniedException", message="denied"
            )

        monkeypatch.setattr(engine, "_fetch_compute_optimizer_candidates", _boom)
        monkeypatch.setattr(
            engine, "_fetch_cost_optimization_hub_candidates", lambda *, region, account_id: []
        )

        recs, warnings = build_recommendations(_det_inputs())
        assert any(w.source == "compute_optimizer" for w in warnings)
        assert any(w.code == "AccessDeniedException" for w in warnings)

    def test_coh_actual_failure_still_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            engine,
            "_coh_capability",
            lambda region, account_id: (
                __import__(
                    "app.schemas.optimization", fromlist=["ServiceCapability"]
                ).ServiceCapability(status="ACTIVE"),
                None,
            ),
        )
        monkeypatch.setattr(engine, "_fetch_compute_optimizer_candidates", lambda *, region, account_id: [])

        def _boom(*, region: str, account_id):
            raise __import__(
                "app.services.aws.cost_optimization_hub", fromlist=["CostOptimizationHubError"]
            ).CostOptimizationHubError(
                code="AccessDeniedException", message="denied"
            )

        monkeypatch.setattr(engine, "_fetch_cost_optimization_hub_candidates", _boom)

        recs, warnings = build_recommendations(_det_inputs())
        assert any(w.source == "cost_optimization_hub" for w in warnings)
        assert any(w.code == "AccessDeniedException" for w in warnings)
