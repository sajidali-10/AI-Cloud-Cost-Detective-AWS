"""Phase 9C.2 — Rackspace canonical recommendation tests.

Covers the provider-neutral ``/api/cloud/recommendations`` contract
for ``provider=RACKSPACE``:

* Phase 9C.1 finding -> canonical :class:`CloudRecommendation` mapping
  (stable IDs, severity/confidence/resource/region/action preserved,
  limitations preserved, unknown savings stay ``None`` — never ``$0``)
* live rule behaviour through the canonical route with mocked
  canonical inputs (detached/attached volumes, 100 vs 101 GB severity
  threshold, ACTIVE servers never rightsized, non-ACTIVE review,
  zero load balancers, empty inventory)
* partial resource inventory (servers valid / volumes unavailable ->
  ``PARTIAL_SUCCESS`` with ``account_errors``, no fabricated findings)
* error mapping (a total resource failure is never a zero-recommendation
  success; NOT_CONFIGURED / AUTH_FAILED / ... map onto the canonical
  HTTP error vocabulary)
* determinism (same canonical input -> identical recommendation
  identity, count, ordering, severity, confidence, evidence, savings)
* provider capability (RACKSPACE recommendations SUPPORTED, AWS and
  Azure unchanged)
* AWS / Azure selector + envelope regression (backward compatible)
* credential material never leaks into the canonical payload
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.api import cloud_recommendations as route_mod
from app.api.cloud_recommendations import (
    _build_rackspace_envelope,
    _build_recommendation_cache_key,
    _resolve_rows,
    get_cloud_recommendations,
)
from app.services.cloud.base import CloudCapability, CloudCapabilityState
from app.services.cloud.models import (
    CloudProviderKind,
    CloudResource,
)
from app.services.cloud.rackspace import adapter as rackspace_adapter_module
from app.services.cloud.rackspace import recommendations as recs
from app.services.cloud.rackspace.costs import (
    RackspaceCostSnapshot,
    RackspaceServiceCost,
)
from app.services.cloud.rackspace.optimization import (
    RackspaceFindingConfidence,
    RackspaceFindingSeverity,
    evaluate_rackspace_optimization,
)
from app.services.cloud.rackspace.recommendations import (
    RACKSPACE_RECOMMENDATION_ACTION,
    RACKSPACE_RECOMMENDATION_SOURCE,
    RACKSPACE_SAVINGS_PERIOD,
    build_rackspace_recommendation_summary,
    finding_to_cloud_recommendation,
)
from app.services.cloud.rackspace.resources import (
    RackspaceResourceError,
    RackspaceResourceErrorCategory,
    RackspaceServiceFailure,
)
from app.services.cloud.registry import ProviderRegistry


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


TENANT = "tenant-100"
REGION = "DFW"


def _row(
    account_id: str = TENANT,
    display_name: str = "Rackspace Cloud",
) -> SimpleNamespace:
    return SimpleNamespace(
        account_id=account_id,
        display_name=display_name,
        enabled=True,
        provider="RACKSPACE",
    )


class _FakeQuery:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _FakeDb:
    """Minimal DB stub: answers the provider/account lookup."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeQuery:
        return _FakeQuery(self._row)


def _vol(
    volume_id: str,
    *,
    region: str = REGION,
    size_gb: int = 50,
    attached: bool = False,
    account_id: str = TENANT,
    state: str = "AVAILABLE",
) -> CloudResource:
    metadata: Dict[str, Any] = {
        "size_gb": size_gb,
        "attachment_state": "ATTACHED" if attached else "DETACHED",
    }
    if attached:
        metadata["attached_server_ids"] = ["srv-a"]
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="VOLUME",
        resource_id=volume_id,
        service="Cloud Block Storage",
        name=f"vol-{volume_id}",
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|VOLUME|{volume_id}",
        metadata=metadata,
    )


def _srv(
    server_id: str,
    *,
    region: str = REGION,
    state: str = "ACTIVE",
    account_id: str = TENANT,
) -> CloudResource:
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="SERVER",
        resource_id=server_id,
        service="Cloud Servers",
        name=f"srv-{server_id}",
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|SERVER|{server_id}",
        metadata={"flavor_name": "general1-4", "flavor_vcpus": 4},
    )


def _lb(
    lb_id: str,
    *,
    region: str = REGION,
    state: str = "ACTIVE",
    account_id: str = TENANT,
) -> CloudResource:
    return CloudResource(
        provider=CloudProviderKind.RACKSPACE,
        account_id=account_id,
        region=region,
        resource_type="LOAD_BALANCER",
        resource_id=lb_id,
        service="Cloud Load Balancers",
        name=f"lb-{lb_id}",
        state=state,
        composite_id=f"RACKSPACE|{account_id}|{region}|LOAD_BALANCER|{lb_id}",
        metadata={"protocol": "HTTP", "port": 80},
    )


def _snapshot(*, currency: str = "USD") -> RackspaceCostSnapshot:
    return RackspaceCostSnapshot(
        currency=currency,
        period_start=date(2026, 10, 1),
        period_end=date(2026, 11, 1),
        total=Decimal("123.45"),
        by_service=(
            RackspaceServiceCost("Cloud Servers", Decimal("100.00")),
            RackspaceServiceCost("Cloud Block Storage", Decimal("23.45")),
        ),
        record_count=2,
    )


def _install_inputs(
    monkeypatch,
    *,
    resources: List[CloudResource],
    failures: Optional[List[RackspaceServiceFailure]] = None,
    error: Optional[RackspaceResourceError] = None,
    cost_error: Optional[Exception] = None,
    snapshot: Optional[RackspaceCostSnapshot] = None,
    rows: Optional[List[SimpleNamespace]] = None,
) -> None:
    """Patch the Phase 9B canonical seams the aggregator consumes.

    ``aggregate_rackspace_resources`` and ``fetch_cost_snapshot`` are
    patched inside the recommendations module namespace so the route
    exercises the REAL Phase 9C.1 engine + mapping + envelope with only
    the canonical upstream inputs faked.
    """

    async def fake_aggregate(*, rows: Any, **_kwargs: Any):
        if error is not None:
            raise error
        return None, list(resources), list(failures or [])

    async def fake_fetch(client: Any) -> RackspaceCostSnapshot:
        if cost_error is not None:
            raise cost_error
        return snapshot if snapshot is not None else _snapshot()

    monkeypatch.setattr(recs, "aggregate_rackspace_resources", fake_aggregate)
    monkeypatch.setattr(recs, "fetch_cost_snapshot", fake_fetch)
    monkeypatch.setattr(recs, "get_billing_client", lambda: object())
    monkeypatch.setattr(
        route_mod, "iter_enabled_accounts",
        lambda db, provider="AWS": list(rows if rows is not None else [_row()]),
    )


async def _call_route(
    monkeypatch,
    *,
    provider: str = "RACKSPACE",
    account_id: Optional[str] = None,
    **install_kwargs: Any,
) -> Any:
    _install_inputs(monkeypatch, **install_kwargs)
    return await get_cloud_recommendations(
        provider=provider,
        account_id=account_id,
        region=None,
        days=30,
        user=SimpleNamespace(id=1),
        db=None,
    )


# ---------------------------------------------------------------------------
# Finding -> CloudRecommendation mapping (unit)
# ---------------------------------------------------------------------------


class TestFindingMapping:
    def _finding(self):
        result = evaluate_rackspace_optimization(
            [_vol("vol-1", size_gb=80), _srv("srv-1")]
        )
        assert result.findings_total == 1
        return result.findings[0]

    def test_finding_maps_to_canonical_recommendation(self):
        finding = self._finding()
        cloud = finding_to_cloud_recommendation(finding)

        assert cloud.provider == CloudProviderKind.RACKSPACE
        assert cloud.recommendation_id == finding.finding_id
        assert cloud.resource_id == finding.resource_id == "vol-1"
        assert cloud.resource_type == finding.resource_type == "VOLUME"
        assert cloud.service == "Cloud Block Storage"
        assert cloud.account_id == finding.account_id == TENANT
        assert cloud.region == finding.region == REGION
        assert cloud.action == RACKSPACE_RECOMMENDATION_ACTION == "REVIEW"
        assert cloud.title == finding.title
        assert cloud.category == finding.category == "STORAGE_REVIEW"
        assert cloud.impact == finding.severity.value
        assert cloud.description == finding.summary
        assert cloud.source == finding.source == "RACKSPACE_CLOUD_API"

    def test_severity_and_confidence_preserved(self):
        finding = self._finding()
        cloud = finding_to_cloud_recommendation(finding)
        assert cloud.impact == "MEDIUM"
        assert cloud.extensions["severity"] == "MEDIUM"
        assert cloud.extensions["confidence"] == finding.confidence.value
        assert (
            cloud.extensions["confidence"]
            == RackspaceFindingConfidence.MEDIUM.value
        )

    def test_rule_id_and_recommended_action_preserved(self):
        finding = self._finding()
        cloud = finding_to_cloud_recommendation(finding)
        assert cloud.extensions["rule_id"] == finding.rule_id == "RS-VOL-001"
        assert cloud.extensions["recommended_action"] == finding.recommended_action
        assert "owner validation" in cloud.extensions["recommended_action"]

    def test_evidence_and_limitations_preserved(self):
        finding = self._finding()
        cloud = finding_to_cloud_recommendation(finding)
        assert cloud.extensions["evidence"] == [dict(e) for e in finding.evidence]
        assert cloud.extensions["limitations"] == list(finding.limitations)
        assert cloud.extensions["savings_basis"] == finding.savings_basis
        assert cloud.extensions["resource_name"] == finding.resource_name

    def test_unknown_savings_stay_none_never_zero(self):
        finding = self._finding()
        cloud = finding_to_cloud_recommendation(finding)
        # unknown != $0 — the canonical item must not claim a figure
        assert cloud.estimated_savings is None
        assert cloud.currency == ""  # no currency claim without savings
        assert cloud.extensions["savings_basis"] == "UNAVAILABLE"

    def test_stable_recommendation_id_from_stable_inputs(self):
        result1 = evaluate_rackspace_optimization([_vol("vol-9")])
        result2 = evaluate_rackspace_optimization([_vol("vol-9")])
        cloud1 = finding_to_cloud_recommendation(result1.findings[0])
        cloud2 = finding_to_cloud_recommendation(result2.findings[0])
        assert cloud1.recommendation_id == cloud2.recommendation_id
        assert cloud1.recommendation_id.startswith("rs-det-rs-vol-001-")
        # different resource -> different stable id
        other = finding_to_cloud_recommendation(
            evaluate_rackspace_optimization([_vol("vol-8")]).findings[0]
        )
        assert other.recommendation_id != cloud1.recommendation_id

    def test_service_label_by_resource_type(self):
        srv = finding_to_cloud_recommendation(
            evaluate_rackspace_optimization(
                [_srv("srv-1", state="SHUTOFF")]
            ).findings[0]
        )
        assert srv.service == "Cloud Servers"
        vol = finding_to_cloud_recommendation(
            evaluate_rackspace_optimization([_vol("v")]).findings[0]
        )
        assert vol.service == "Cloud Block Storage"


# ---------------------------------------------------------------------------
# Rollup / savings semantics
# ---------------------------------------------------------------------------


class TestRollup:
    def _result(self, resources):
        return evaluate_rackspace_optimization(resources)

    def test_summary_reconciles(self):
        resources = [
            _vol("v1", size_gb=80),
            _vol("v2", size_gb=50),
            _srv("s1", state="SHUTOFF"),
        ]
        result = self._result(resources)
        rollup = build_rackspace_recommendation_summary(
            result, rows=[_row()], resource_failures=[]
        )
        assert rollup.total_recommendations == result.findings_total == 3
        assert sum(rollup.by_impact.values()) == 3
        assert sum(rollup.by_resource_type.values()) == 3
        assert sum(rollup.by_action.values()) == 3
        assert sum(rollup.by_source.values()) == 3
        assert rollup.by_impact == {"MEDIUM": 2, "LOW": 1}
        assert rollup.by_resource_type == {"VOLUME": 2, "SERVER": 1}
        assert rollup.by_action == {"REVIEW": 3}
        assert rollup.status == "SUCCESS"

    def test_all_unknown_savings_is_empty_never_zero(self):
        result = self._result([_vol("v1"), _vol("v2")])
        rollup = build_rackspace_recommendation_summary(
            result, rows=[_row()], resource_failures=[]
        )
        assert rollup.recommendations_with_savings == 0
        assert rollup.recommendations_without_savings == 2
        assert rollup.savings_status == "EMPTY"
        assert rollup.potential_savings is None  # not Decimal("0")
        assert rollup.currency is None
        assert rollup.savings_by_currency == {}
        assert rollup.savings_period == RACKSPACE_SAVINGS_PERIOD == "MONTHLY"

    def test_zero_findings_over_valid_resources_is_no_data(self):
        result = self._result([_srv("s1")])  # ACTIVE -> no finding
        rollup = build_rackspace_recommendation_summary(
            result, rows=[_row()], resource_failures=[]
        )
        assert rollup.status == "NO_DATA"
        assert rollup.total_recommendations == 0

    def test_empty_inventory_is_no_data(self):
        result = self._result([])
        rollup = build_rackspace_recommendation_summary(
            result, rows=[_row()], resource_failures=[]
        )
        assert rollup.status == "NO_DATA"

    def test_resource_failures_make_partial_success(self):
        result = self._result([_srv("s1", state="SHUTOFF")])
        failures = [
            RackspaceServiceFailure(
                service="Cloud Block Storage",
                region="DFW",
                category=RackspaceResourceErrorCategory.NETWORK_ERROR,
                message="transport failure",
            )
        ]
        rollup = build_rackspace_recommendation_summary(
            result, rows=[_row()], resource_failures=failures
        )
        assert rollup.status == "PARTIAL_SUCCESS"
        assert len(rollup.account_errors) == 1
        error = rollup.account_errors[0]
        assert error.account_id == TENANT
        assert error.display_name == "Cloud Block Storage"
        assert error.code == "NETWORK_ERROR"
        assert error.message == "transport failure"


# ---------------------------------------------------------------------------
# Live rule behaviour through the canonical route (mocked inputs)
# ---------------------------------------------------------------------------


class TestRouteRuleBehaviour:
    async def test_detached_volume_produces_canonical_recommendation(
        self, monkeypatch
    ):
        out = await _call_route(monkeypatch, resources=[_vol("vol-1", size_gb=50)])
        summary = out["summary"]
        assert summary["provider"] == "RACKSPACE"
        assert summary["status"] == "SUCCESS"
        assert summary["total_recommendations"] == 1
        assert summary["source"] == RACKSPACE_RECOMMENDATION_SOURCE
        assert len(out["recommendations"]) == 1
        item = out["recommendations"][0]
        assert item["resource_id"] == "vol-1"
        assert item["extensions"]["rule_id"] == "RS-VOL-001"
        assert item["action"] == "REVIEW"
        assert item["impact"] == "MEDIUM"

    async def test_attached_volume_no_recommendation(self, monkeypatch):
        out = await _call_route(
            monkeypatch, resources=[_vol("vol-1", attached=True)]
        )
        assert out["summary"]["total_recommendations"] == 0
        assert out["recommendations"] == []
        assert out["summary"]["status"] == "NO_DATA"

    async def test_severity_threshold_100_vs_101_gb(self, monkeypatch):
        out_100 = await _call_route(
            monkeypatch, resources=[_vol("vol-100", size_gb=100)]
        )
        assert out_100["recommendations"][0]["impact"] == "MEDIUM"

        out_101 = await _call_route(
            monkeypatch, resources=[_vol("vol-101", size_gb=101)]
        )
        assert out_101["recommendations"][0]["impact"] == "HIGH"
        assert (
            out_101["recommendations"][0]["extensions"]["severity"] == "HIGH"
        )

    async def test_active_server_no_rightsizing_recommendation(
        self, monkeypatch
    ):
        out = await _call_route(
            monkeypatch,
            # ACTIVE with a real flavor — must NOT become a
            # rightsizing / idle / downsizing / waste recommendation.
            resources=[_srv("srv-1", state="ACTIVE")],
        )
        assert out["summary"]["total_recommendations"] == 0
        assert out["recommendations"] == []

    async def test_active_server_flavor_is_never_rightsizing_evidence(
        self, monkeypatch
    ):
        # Even mixed with detached volumes, ACTIVE servers stay absent.
        out = await _call_route(
            monkeypatch,
            resources=[
                _srv("srv-active", state="ACTIVE"),
                _srv("srv-build", state="BUILD"),
                _vol("vol-1", size_gb=50),
            ],
        )
        rec_resources = {r["resource_id"] for r in out["recommendations"]}
        assert rec_resources == {"vol-1"}

    async def test_non_active_server_review_recommendation(
        self, monkeypatch
    ):
        out = await _call_route(
            monkeypatch, resources=[_srv("srv-1", state="SHUTOFF")]
        )
        assert out["summary"]["total_recommendations"] == 1
        item = out["recommendations"][0]
        assert item["resource_id"] == "srv-1"
        assert item["extensions"]["rule_id"] == "RS-SRV-001"
        assert item["impact"] == "LOW"
        assert item["extensions"]["confidence"] == "LOW"
        assert item["action"] == "REVIEW"  # review, never stop/delete

    async def test_zero_load_balancers_no_lb_recommendation(
        self, monkeypatch
    ):
        out = await _call_route(
            monkeypatch, resources=[_vol("vol-1"), _srv("srv-1")]
        )
        assert all(
            r["resource_type"] != "LOAD_BALANCER"
            for r in out["recommendations"]
        )

    async def test_empty_inventory_valid_zero_recommendation_result(
        self, monkeypatch
    ):
        out = await _call_route(monkeypatch, resources=[])
        # valid (empty) inventory + zero findings -> successful empty
        # result, never an error.
        assert out["summary"]["status"] == "NO_DATA"
        assert out["summary"]["total_recommendations"] == 0
        assert out["recommendations"] == []

    async def test_deterministic_output(self, monkeypatch):
        resources = [
            _vol("v1", size_gb=80),
            _vol("v2", size_gb=50),
            _vol("v3", size_gb=101),
            _srv("s1", state="SHUTOFF"),
        ]
        out1 = await _call_route(monkeypatch, resources=resources)
        out2 = await _call_route(monkeypatch, resources=resources)
        assert out1 == out2  # identical envelope, byte-for-byte
        ids1 = [r["recommendation_id"] for r in out1["recommendations"]]
        assert len(ids1) == len(set(ids1))  # unique
        # ordering: severity desc, then rule, then size desc, ...
        order = [
            (r["impact"], r["extensions"]["rule_id"], r["resource_id"])
            for r in out1["recommendations"]
        ]
        assert order == sorted(
            order,
            key=lambda t: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[t[0]], t[1], t[2]),
        )


# ---------------------------------------------------------------------------
# Envelope shape / savings semantics on the wire
# ---------------------------------------------------------------------------


class TestEnvelope:
    async def test_envelope_shape_and_savings_semantics(self, monkeypatch):
        out = await _call_route(
            monkeypatch, resources=[_vol("v1", size_gb=80)]
        )
        summary = out["summary"]
        assert summary["provider"] == "RACKSPACE"
        assert summary["scope"] == "all"
        assert summary["scope_kind"] == "all"
        assert summary["status"] == "SUCCESS"
        assert summary["savings_status"] == "EMPTY"
        assert summary["potential_savings"] is None  # unknown != 0
        assert summary["potential_annual_savings"] is None
        assert summary["recommendations_with_savings"] == 0
        assert summary["recommendations_without_savings"] == 1
        assert summary["savings_by_currency"] == []
        assert summary["savings_period"] == "MONTHLY"
        assert summary["by_impact"] == [{"key": "MEDIUM", "count": 1}]
        assert summary["by_resource_type"] == [{"key": "VOLUME", "count": 1}]
        assert summary["by_action"] == [{"key": "REVIEW", "count": 1}]
        assert summary["by_source"] == [
            {"key": "RACKSPACE_CLOUD_API", "count": 1}
        ]
        assert summary["account_errors"] == []
        item = out["recommendations"][0]
        assert item["estimated_savings"] is None
        assert item["currency"] is None  # "" rendered as None on the wire
        assert item["provider"] == "RACKSPACE"
        assert item["region"] == REGION
        assert item["account_id"] == TENANT

    async def test_ids_unique_and_match_engine_findings(self, monkeypatch):
        resources = [_vol(f"v{i}", size_gb=50) for i in range(5)]
        out = await _call_route(monkeypatch, resources=resources)
        engine = evaluate_rackspace_optimization(resources)
        engine_ids = [f.finding_id for f in engine.findings]
        route_ids = [r["recommendation_id"] for r in out["recommendations"]]
        assert len(set(route_ids)) == len(route_ids)
        assert set(route_ids) == set(engine_ids)

    async def test_single_account_scope(self, monkeypatch):
        _install_inputs(monkeypatch, resources=[_vol("v1")])
        out = await get_cloud_recommendations(
            provider="RACKSPACE",
            account_id=f"acct:{TENANT}",
            region=None,
            days=30,
            user=SimpleNamespace(id=1),
            db=_FakeDb(_row()),
        )
        assert out["summary"]["scope"] == f"acct:{TENANT}"
        assert out["summary"]["scope_kind"] == "single"

    async def test_rackspace_rejects_azure_selector(self, monkeypatch):
        _install_inputs(monkeypatch, resources=[])
        out = await get_cloud_recommendations(
            provider="RACKSPACE",
            account_id="sub:abc",
            region=None,
            days=30,
            user=SimpleNamespace(id=1),
            db=None,
        )
        assert isinstance(out, object)
        assert out.status_code == 400
        body = json.loads(out.body)
        assert body["error_code"] == "ProviderSelectorMismatch"

    async def test_cost_context_failure_does_not_fail_recommendations(
        self, monkeypatch
    ):
        out = await _call_route(
            monkeypatch,
            resources=[_vol("v1")],
            cost_error=RuntimeError("billing unavailable"),
        )
        # Billing is context-only: its failure must not break the
        # recommendations themselves.
        assert out["summary"]["status"] == "SUCCESS"
        assert out["summary"]["total_recommendations"] == 1


# ---------------------------------------------------------------------------
# Partial / error semantics
# ---------------------------------------------------------------------------


class TestPartialAndError:
    async def test_partial_resource_inventory(self, monkeypatch):
        # servers valid, volumes unavailable -> server rules may run,
        # no volume findings, PARTIAL_SUCCESS with account_errors.
        out = await _call_route(
            monkeypatch,
            resources=[_srv("srv-1", state="SHUTOFF")],
            failures=[
                RackspaceServiceFailure(
                    service="Cloud Block Storage",
                    region="DFW",
                    category=RackspaceResourceErrorCategory.NETWORK_ERROR,
                    message="transport failure",
                )
            ],
        )
        summary = out["summary"]
        assert summary["status"] == "PARTIAL_SUCCESS"
        assert summary["total_recommendations"] == 1
        assert out["recommendations"][0]["extensions"]["rule_id"] == "RS-SRV-001"
        assert all(
            r["resource_type"] != "VOLUME" for r in out["recommendations"]
        )
        errors = summary["account_errors"]
        assert len(errors) == 1
        assert errors[0]["account_id"] == TENANT
        assert errors[0]["display_name"] == "Cloud Block Storage"
        assert errors[0]["code"] == "NETWORK_ERROR"

    async def test_volume_service_unavailable_no_volume_findings(
        self, monkeypatch
    ):
        # The detached volume never entered the inventory (service
        # failed) — nothing may be fabricated for it.
        out = await _call_route(
            monkeypatch,
            resources=[_srv("srv-1")],
            failures=[
                RackspaceServiceFailure(
                    service="Cloud Block Storage",
                    region="DFW",
                    category=RackspaceResourceErrorCategory.UNSUPPORTED,
                    message="service not present in the Identity catalog",
                )
            ],
        )
        assert out["summary"]["status"] == "PARTIAL_SUCCESS"
        assert out["recommendations"] == []

    async def test_resource_upstream_error_is_not_zero_resource_success(
        self, monkeypatch
    ):
        out = await _call_route(
            monkeypatch,
            resources=[],
            error=RackspaceResourceError(
                RackspaceResourceErrorCategory.NETWORK_ERROR,
                "transport failure",
            ),
        )
        assert out.status_code == 502
        body = json.loads(out.body)
        assert body == {
            "status": "error",
            "error_code": "UPSTREAM_ERROR",
            "category": "NETWORK_ERROR",
        }

    @pytest.mark.parametrize(
        "category,http_code,public_code",
        [
            (
                RackspaceResourceErrorCategory.NOT_CONFIGURED,
                503,
                "NOT_CONFIGURED",
            ),
            (
                RackspaceResourceErrorCategory.AUTH_FAILED,
                401,
                "AUTH_FAILED",
            ),
            (
                RackspaceResourceErrorCategory.ACCESS_DENIED,
                403,
                "ACCESS_DENIED",
            ),
            (
                RackspaceResourceErrorCategory.NOT_FOUND,
                404,
                "NOT_FOUND",
            ),
            (
                RackspaceResourceErrorCategory.THROTTLED,
                429,
                "THROTTLED",
            ),
        ],
    )
    async def test_error_category_mapping(
        self, monkeypatch, category, http_code, public_code
    ):
        out = await _call_route(
            monkeypatch,
            resources=[],
            error=RackspaceResourceError(category, "boom"),
        )
        assert out.status_code == http_code
        body = json.loads(out.body)
        assert body["error_code"] == public_code
        assert body["status"] == "error"

    async def test_malformed_upstream_metadata_cannot_leak_credential_material(
        self, monkeypatch
    ):
        # Defensive: a (hypothetical) upstream payload that smuggled
        # credential-looking keys into resource metadata must not
        # surface in the canonical recommendation payload.  Only the
        # safe operational metadata the Phase 9B.2 layer promotes
        # (size / attachment state) ever reaches the engine.
        leaky = CloudResource(
            provider=CloudProviderKind.RACKSPACE,
            account_id=TENANT,
            region=REGION,
            resource_type="VOLUME",
            resource_id="vol-1",
            service="Cloud Block Storage",
            name="vol-1",
            state="AVAILABLE",
            composite_id=f"RACKSPACE|{TENANT}|{REGION}|VOLUME|vol-1",
            metadata={
                "size_gb": 50,
                "attachment_state": "DETACHED",
                "adminPass": "supersecret-password",
                "RACKSPACE_API_KEY": "leaked-api-key",
                "X-Auth-Token": "leaked-token-value",
                "addresses": {"private": [{"addr": "10.0.0.1"}]},
            },
        )
        out = await _call_route(monkeypatch, resources=[leaky])
        payload = json.dumps(out)
        assert "supersecret-password" not in payload
        assert "leaked-api-key" not in payload
        assert "leaked-token-value" not in payload
        assert "10.0.0.1" not in payload
        assert "adminPass" not in payload
        assert "RACKSPACE_API_KEY" not in payload
        assert "X-Auth-Token" not in payload
        # the honest finding is still present
        assert out["summary"]["total_recommendations"] == 1


# ---------------------------------------------------------------------------
# Capability / provider registry
# ---------------------------------------------------------------------------


class TestCapability:
    def test_optimization_supported_when_configured(self, monkeypatch):
        monkeypatch.setattr(
            rackspace_adapter_module,
            "get_config_status",
            lambda: type("S", (), {"configured": True, "missing": []})(),
        )
        snapshot = (
            ProviderRegistry.current()
            .require(CloudProviderKind.RACKSPACE)
            .get_capabilities()
        )
        assert snapshot.has(CloudCapability.OPTIMIZATION)
        details = snapshot.details[CloudCapability.OPTIMIZATION]
        assert details["phase"] == "9C.2"
        assert details["read_only"] is True
        assert details["rules"] == ["RS-VOL-001", "RS-SRV-001", "RS-LB-001"]
        assert "never zero" in details["savings_semantics"]

    def test_optimization_not_configured_when_identity_missing(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            rackspace_adapter_module,
            "get_config_status",
            lambda: type(
                "S", (), {"configured": False, "missing": ["RACKSPACE_USERNAME"]}
            )(),
        )
        snapshot = (
            ProviderRegistry.current()
            .require(CloudProviderKind.RACKSPACE)
            .get_capabilities()
        )
        assert not snapshot.has(CloudCapability.OPTIMIZATION)

    def test_aws_and_azure_capabilities_unchanged(self):
        registry = ProviderRegistry.current()
        aws = registry.require(CloudProviderKind.AWS).get_capabilities()
        azure = registry.require(CloudProviderKind.AZURE).get_capabilities()
        assert aws.has(CloudCapability.OPTIMIZATION)
        assert azure.capabilities[CloudCapability.OPTIMIZATION] in (
            CloudCapabilityState.SUPPORTED,
            CloudCapabilityState.NOT_CONFIGURED,
        )

    async def test_adapter_get_recommendations_delegates(self, monkeypatch):
        async def fake_aggregate(*, rows, region=None, days=None):
            cloud = finding_to_cloud_recommendation(
                evaluate_rackspace_optimization([_vol("v1")]).findings[0]
            )
            rollup = build_rackspace_recommendation_summary(
                evaluate_rackspace_optimization([_vol("v1")]),
                rows=list(rows),
                resource_failures=[],
            )
            return [cloud], rollup, []

        # The adapter imports the aggregator into its own namespace —
        # patch it there.
        monkeypatch.setattr(
            rackspace_adapter_module,
            "aggregate_rackspace_recommendations_for_account",
            fake_aggregate,
        )
        adapter = ProviderRegistry.current().require(CloudProviderKind.RACKSPACE)
        flat = await adapter.get_recommendations(accounts=[_row()])
        assert len(flat) == 1
        assert flat[0].resource_id == "v1"
        assert flat[0].provider == CloudProviderKind.RACKSPACE


# ---------------------------------------------------------------------------
# AWS / Azure regression
# ---------------------------------------------------------------------------


class TestAwsAzureRegression:
    def test_aws_rejects_azure_sub_selector(self, monkeypatch):
        monkeypatch.setattr(
            route_mod, "iter_enabled_accounts",
            lambda db, provider="AWS": [_row("111", "acct")],
        )
        import asyncio

        out = asyncio.run(
            get_cloud_recommendations(
                provider="AWS",
                account_id="sub:abc",
                region="us-east-1",
                days=30,
                user=SimpleNamespace(id=1),
                db=None,
            )
        )
        assert out.status_code == 400
        assert json.loads(out.body)["error_code"] == "ProviderSelectorMismatch"

    def test_azure_rejects_aws_acct_selector(self, monkeypatch):
        monkeypatch.setattr(
            route_mod, "iter_enabled_accounts",
            lambda db, provider="AZURE": [_row("guid-1", "sub")],
        )
        import asyncio

        out = asyncio.run(
            get_cloud_recommendations(
                provider="AZURE",
                account_id="acct:1",
                region=None,
                days=30,
                user=SimpleNamespace(id=1),
                db=None,
            )
        )
        assert out.status_code == 400
        assert json.loads(out.body)["error_code"] == "ProviderSelectorMismatch"

    def test_rackspace_accepts_acct_selector(self, monkeypatch):
        _install_inputs(monkeypatch, resources=[_vol("v1")])
        import asyncio

        out = asyncio.run(
            get_cloud_recommendations(
                provider="RACKSPACE",
                account_id=f"acct:{TENANT}",
                region=None,
                days=30,
                user=SimpleNamespace(id=1),
                db=_FakeDb(_row()),
            )
        )
        assert out["summary"]["scope"] == f"acct:{TENANT}"

    def test_cache_key_isolation_rackspace_vs_aws(self):
        rs_all = _build_recommendation_cache_key(
            user_id=1, provider="RACKSPACE", scope="all",
            region=None, days=30,
        )
        aws_all = _build_recommendation_cache_key(
            user_id=1, provider="AWS", scope="all",
            region=None, days=30,
        )
        azure_all = _build_recommendation_cache_key(
            user_id=1, provider="AZURE", scope="all",
            region=None, days=30,
        )
        assert len({rs_all, aws_all, azure_all}) == 3

    def test_shared_schema_backward_compatible(self):
        # No schema fields were added for Rackspace — the canonical
        # envelope still constructs from the same schema.
        from app.schemas.cloud_recommendation import (
            CloudRecommendationItem,
            CloudRecommendationListResponse,
            CloudRecommendationSummaryEnvelope,
        )

        summary = CloudRecommendationSummaryEnvelope(
            provider="AWS",
            scope="all",
            scope_kind="all",
            status="SUCCESS",
            savings_status="OK",
            currency="USD",
            total_recommendations=1,
            recommendations_with_savings=1,
            recommendations_without_savings=0,
        )
        item = CloudRecommendationItem(
            provider="AWS",
            recommendation_id="r1",
            resource_id="i-1",
            resource_type="ec2",
            service="ec2",
            action="RIGHTSIZE",
            title="t",
            source="AWS_COMPUTE_OPTIMIZER",
        )
        envelope = CloudRecommendationListResponse(
            summary=summary, recommendations=[item]
        )
        assert envelope.summary.provider == "AWS"
