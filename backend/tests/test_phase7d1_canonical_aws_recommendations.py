"""Phase 7D.1 — canonical AWS recommendations tests.

Covers the provider-neutral ``/api/cloud/recommendations`` contract
for ``provider=AWS``:

* AWS → canonical :class:`CloudRecommendation` mapping
  (Compute Optimizer, Cost Optimization Hub, deterministic engine)
* QA-Dev + Production + All-AWS rollup, attribution, dedup
* partial-failure isolation (one account cannot erase another's)
* mixed-currency handling (never summed, explicit state)
* cache-key isolation (AWS vs Azure, all vs single, user/region/days)
* selector coercion (Azure ``sub:`` never reaches AWS, and vice versa)
* legacy ``/api/aws/optimization/*`` compatibility
* Azure canonical regression (no weakened behaviour)
* no credential / token leakage in envelopes
"""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import cloud_recommendations as route_mod
from app.api.cloud_recommendations import (
    _build_aws_envelope,
    _build_recommendation_cache_key,
    _resolve_rows,
    get_cloud_recommendations,
)
from app.schemas.optimization import (
    Confidence,
    Recommendation,
    RecommendationAction,
    ResourceType,
    SavingsSource,
)
from app.services.aws.cost_aggregator import AccountError
from app.services.aws.optimization_aggregator import (
    AccountRecommendationBundle,
    MultiAccountOptimizationReport,
    _merge_recommendations,
)
from app.services.cloud.aws import recommendation_aggregator as agg
from app.services.cloud.aws.adapter import AWSCloudProvider
from app.services.cloud.aws.recommendation_aggregator import (
    AWS_SAVINGS_PERIOD,
    aggregate_aws_recommendations_for_accounts,
    build_aws_recommendation_summary,
    recommendation_to_cloud,
)
from app.services.cloud.models import CloudProviderKind
from app.services.cloud.registry import ProviderRegistry


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_rec(
    *,
    rid: str = "r1",
    resource_id: str = "i-1",
    account_id: str | None = None,
    action: RecommendationAction = RecommendationAction.RIGHTSIZE,
    resource_type: ResourceType = ResourceType.EC2,
    source: SavingsSource = SavingsSource.AWS_COMPUTE_OPTIMIZER,
    confidence: Confidence = Confidence.HIGH,
    savings: Decimal | None = None,
    currency: str = "USD",
    region: str = "us-east-1",
    title: str = "rightsize instance",
    finding: str = "instance is over-provisioned",
    account_display_name: str | None = None,
) -> Recommendation:
    rec = Recommendation(
        recommendation_id=rid,
        resource_id=resource_id,
        resource_type=resource_type,
        region=region,
        account_id=account_id,
        action=action,
        title=title,
        finding=finding,
        estimated_monthly_savings=savings,
        currency=currency,
        savings_source=source,
        primary_source=source,
        sources=[source],
        confidence=confidence,
    )
    if account_display_name is not None:
        rec = rec.model_copy(update={"account_display_name": account_display_name})
    return rec


def _report(
    *,
    status: str = "SUCCESS",
    recs=(),
    by_account=(),
    errors=(),
) -> MultiAccountOptimizationReport:
    return MultiAccountOptimizationReport(
        status=status,
        days=30,
        region=None,
        recommendations=list(recs),
        warnings=[],
        by_account=list(by_account),
        account_errors=list(errors),
    )


def _row(account_id: str, display_name: str = "acct") -> SimpleNamespace:
    return SimpleNamespace(
        account_id=account_id,
        display_name=display_name,
        provider="AWS",
        enabled=True,
        is_management=False,
        role_arn="arn:aws:iam::123456789012:role/read",
        external_id=None,
        preferred_region="us-east-1",
        tenant_id=None,
        subscription_id=None,
    )


# ---------------------------------------------------------------------------
# 1. Mapping — Compute Optimizer / Cost Optimization Hub / deterministic
# ---------------------------------------------------------------------------


class TestAwsMapping:
    def test_compute_optimizer_mapping(self):
        rec = _make_rec(
            source=SavingsSource.AWS_COMPUTE_OPTIMIZER,
            savings=Decimal("42.50"),
            account_id="111122223333",
        )
        cloud = recommendation_to_cloud(rec)
        assert cloud.provider == CloudProviderKind.AWS
        assert cloud.source == "AWS_COMPUTE_OPTIMIZER"
        assert cloud.account_id == "111122223333"
        assert cloud.estimated_savings == Decimal("42.50")
        assert cloud.currency == "USD"
        assert cloud.extensions["confidence"] == "HIGH"

    def test_cost_optimization_hub_mapping(self):
        rec = _make_rec(
            source=SavingsSource.AWS_COST_OPTIMIZATION_HUB,
            savings=Decimal("100"),
            confidence=Confidence.MEDIUM,
        )
        cloud = recommendation_to_cloud(rec)
        assert cloud.source == "AWS_COST_OPTIMIZATION_HUB"
        assert cloud.extensions["sources"] == ["AWS_COST_OPTIMIZATION_HUB"]

    def test_deterministic_mapping_unknown_source(self):
        # The deterministic engine emits UNKNOWN savings source and no
        # authoritative figure — we never fabricate one.
        rec = _make_rec(
            source=SavingsSource.UNKNOWN,
            savings=None,
            confidence=Confidence.LOW,
        )
        cloud = recommendation_to_cloud(rec)
        assert cloud.source == "UNKNOWN"
        assert cloud.estimated_savings is None

    def test_provider_and_source_attribution_preserved(self):
        rec = _make_rec(
            account_id="999988887777",
            region="eu-west-1",
            resource_id="vol-abc",
            resource_type=ResourceType.EBS_VOLUME,
            action=RecommendationAction.DELETE_UNUSED,
        )
        cloud = recommendation_to_cloud(rec)
        assert cloud.account_id == "999988887777"
        assert cloud.region == "eu-west-1"
        assert cloud.resource_id == "vol-abc"
        assert cloud.resource_type == "EBS_VOLUME"
        assert cloud.action == "DELETE_UNUSED"

    def test_generic_columns_promoted(self):
        rec = _make_rec(action=RecommendationAction.RIGHTSIZE, confidence=Confidence.HIGH)
        cloud = recommendation_to_cloud(rec)
        assert cloud.category == "RIGHTSIZE"
        assert cloud.impact == "HIGH"
        assert cloud.description == "instance is over-provisioned"

    def test_summary_percentage_preserved_in_extensions(self):
        rec = _make_rec(savings=Decimal("10"))
        rec = rec.model_copy(update={"savings_percentage": Decimal("25.00")})
        cloud = recommendation_to_cloud(rec)
        assert cloud.extensions["savings_percentage"] == 25.0
        assert cloud.extensions["savings_period"] == AWS_SAVINGS_PERIOD


# ---------------------------------------------------------------------------
# 2. Rollup — status, attribution, savings, currency
# ---------------------------------------------------------------------------


class TestAwsRollup:
    def test_clean_empty_is_no_data(self):
        rollup = build_aws_recommendation_summary(_report(recs=[]))
        assert rollup.status == "NO_DATA"
        assert rollup.savings_status == "EMPTY"
        assert rollup.potential_savings is None

    def test_success_with_data(self):
        rollup = build_aws_recommendation_summary(
            _report(recs=[_make_rec(savings=Decimal("50"))])
        )
        assert rollup.status == "SUCCESS"
        assert rollup.savings_status == "OK"
        assert rollup.currency == "USD"
        assert rollup.potential_savings == Decimal("50")
        assert rollup.savings_period == "MONTHLY"

    def test_partial_success_preserves_other_accounts(self):
        err = AccountError(
            account_id="222222222222",
            display_name="Prod",
            code="AccessDenied",
            message="AWS denied access for this account",
        )
        rollup = build_aws_recommendation_summary(
            _report(
                status="PARTIAL_SUCCESS",
                recs=[_make_rec(account_id="111122223333", savings=Decimal("10"))],
                by_account=[("111122223333", "QA-Dev", 1)],
                errors=[err],
            )
        )
        # One failing account must NOT erase the healthy account's recs.
        assert rollup.status == "PARTIAL_SUCCESS"
        assert rollup.total_recommendations == 1
        assert len(rollup.account_errors) == 1
        assert rollup.account_errors[0].account_id == "222222222222"

    def test_error_when_all_failed(self):
        err = AccountError("111", "QA", "AccessDenied", "AWS denied access for this account")
        rollup = build_aws_recommendation_summary(
            _report(status="ERROR", recs=[], errors=[err])
        )
        assert rollup.status == "ERROR"
        assert rollup.total_recommendations == 0

    def test_account_attribution(self):
        rollup = build_aws_recommendation_summary(
            _report(
                recs=[
                    _make_rec(rid="r1", account_id="111", savings=Decimal("1")),
                    _make_rec(rid="r2", resource_id="i-2", account_id="222", savings=Decimal("2")),
                ]
            )
        )
        assert rollup.by_account == {"111": 1, "222": 1}

    def test_source_attribution(self):
        rollup = build_aws_recommendation_summary(
            _report(
                recs=[
                    _make_rec(rid="r1", source=SavingsSource.AWS_COST_OPTIMIZATION_HUB, savings=Decimal("1")),
                    _make_rec(rid="r2", resource_id="i-2", source=SavingsSource.AWS_COMPUTE_OPTIMIZER, savings=Decimal("2")),
                    _make_rec(rid="r3", resource_id="i-3", source=SavingsSource.UNKNOWN),
                ]
            )
        )
        assert rollup.by_source == {
            "AWS_COST_OPTIMIZATION_HUB": 1,
            "AWS_COMPUTE_OPTIMIZER": 1,
            "UNKNOWN": 1,
        }

    def test_savings_ok_single_currency(self):
        rollup = build_aws_recommendation_summary(
            _report(
                recs=[
                    _make_rec(rid="r1", savings=Decimal("10"), currency="USD"),
                    _make_rec(rid="r2", resource_id="i-2", savings=Decimal("5"), currency="USD"),
                ]
            )
        )
        assert rollup.savings_status == "OK"
        assert rollup.potential_savings == Decimal("15")
        assert rollup.savings_by_currency == {"USD": Decimal("15")}
        assert rollup.recommendations_with_savings == 2

    def test_mixed_currency_no_total(self):
        rollup = build_aws_recommendation_summary(
            _report(
                recs=[
                    _make_rec(rid="r1", savings=Decimal("10"), currency="USD"),
                    _make_rec(rid="r2", resource_id="i-2", savings=Decimal("20"), currency="EUR"),
                ]
            )
        )
        assert rollup.savings_status == "MIXED_CURRENCY"
        assert rollup.has_mixed_currencies is True
        # Never summed across currencies; never FX-converted.
        assert rollup.potential_savings is None
        assert rollup.currency is None
        assert rollup.savings_by_currency == {"USD": Decimal("10"), "EUR": Decimal("20")}

    def test_recommendations_without_savings_retained(self):
        rollup = build_aws_recommendation_summary(
            _report(
                recs=[
                    _make_rec(rid="r1", savings=Decimal("10")),
                    _make_rec(rid="r2", resource_id="i-2", savings=None),
                ]
            )
        )
        assert rollup.total_recommendations == 2
        assert rollup.recommendations_with_savings == 1
        assert rollup.recommendations_without_savings == 1


# ---------------------------------------------------------------------------
# 3. Aggregation delegation + dedup
# ---------------------------------------------------------------------------


class TestAwsAggregation:
    @pytest.mark.asyncio
    async def test_empty_rows_returns_no_data_without_fanout(self):
        flat, rollup, errors = await aggregate_aws_recommendations_for_accounts(
            rows=[], region="us-east-1", days=30
        )
        assert flat == []
        assert rollup.status == "NO_DATA"
        assert errors == []

    @pytest.mark.asyncio
    async def test_delegates_to_phase7b_aggregator(self, monkeypatch):
        captured = {}

        async def _fake(*, rows, region, days):
            captured["rows"] = rows
            captured["region"] = region
            captured["days"] = days
            return _report(
                recs=[_make_rec(account_id="111", savings=Decimal("7"))],
                by_account=[("111", "QA-Dev", 1)],
            )

        monkeypatch.setattr(agg, "aggregate_recommendations_for_context", _fake)
        row = _row("111", "QA-Dev")
        flat, rollup, errors = await aggregate_aws_recommendations_for_accounts(
            rows=[row], region="us-west-2", days=7
        )
        assert captured["rows"] == [row]
        assert captured["region"] == "us-west-2"
        assert captured["days"] == 7
        assert len(flat) == 1
        assert flat[0].provider == CloudProviderKind.AWS
        assert rollup.status == "SUCCESS"

    def test_phase7b_dedup_merges_duplicate_resource_action(self):
        # Two accounts reporting the SAME resource_id + action collapse
        # into one row (multi-account annotation) — savings are never
        # summed across the duplicates.
        b1 = AccountRecommendationBundle(
            row=_row("111", "QA-Dev"),
            recommendations=[
                _make_rec(rid="r1", resource_id="i-shared", action=RecommendationAction.RIGHTSIZE,
                          savings=Decimal("10"), account_id="111")
            ],
            warnings=[],
        )
        b2 = AccountRecommendationBundle(
            row=_row("222", "Prod"),
            recommendations=[
                _make_rec(rid="r2", resource_id="i-shared", action=RecommendationAction.RIGHTSIZE,
                          savings=Decimal("10"), account_id="222")
            ],
            warnings=[],
        )
        merged = _merge_recommendations([b1, b2])
        assert len(merged) == 1
        assert "more" in (merged[0].account_display_name or "")


# ---------------------------------------------------------------------------
# 4. Envelope
# ---------------------------------------------------------------------------


class TestAwsEnvelope:
    def _rollup(self, recs, **kw):
        return build_aws_recommendation_summary(_report(recs=recs, **kw))

    def test_envelope_shape(self):
        recs = [_make_rec(account_id="111", savings=Decimal("30"))]
        rollup = self._rollup(recs)
        flat = [recommendation_to_cloud(r) for r in recs]
        env = _build_aws_envelope(flat, rollup, scope="all")
        assert env.summary.provider == "AWS"
        assert env.summary.scope == "all"
        assert env.summary.scope_kind == "all"
        assert env.summary.savings_period == "MONTHLY"
        assert env.summary.potential_savings == Decimal("30")
        assert env.summary.potential_annual_savings is None
        assert env.summary.subscription_errors == []
        assert env.recommendations[0].provider == "AWS"
        assert env.recommendations[0].estimated_savings == Decimal("30")

    def test_envelope_mixed_currency_explicit(self):
        recs = [
            _make_rec(rid="r1", savings=Decimal("10"), currency="USD"),
            _make_rec(rid="r2", resource_id="i-2", savings=Decimal("20"), currency="EUR"),
        ]
        rollup = self._rollup(recs)
        flat = [recommendation_to_cloud(r) for r in recs]
        env = _build_aws_envelope(flat, rollup, scope="all")
        assert env.summary.savings_status == "MIXED_CURRENCY"
        assert env.summary.potential_savings is None
        assert {c.currency for c in env.summary.savings_by_currency} == {"USD", "EUR"}

    def test_envelope_account_errors_sanitized(self):
        err = AccountError("222", "Prod", "AccessDenied", "AWS denied access for this account")
        rollup = self._rollup([_make_rec(account_id="111", savings=Decimal("1"))], status="PARTIAL_SUCCESS", errors=[err])
        flat = [recommendation_to_cloud(r) for r in [_make_rec(account_id="111", savings=Decimal("1"))]]
        env = _build_aws_envelope(flat, rollup, scope="all")
        assert env.summary.status == "PARTIAL_SUCCESS"
        assert env.summary.account_errors[0].account_id == "222"
        assert env.summary.by_account[0].key == "111"


# ---------------------------------------------------------------------------
# 5. Adapter contract + capability
# ---------------------------------------------------------------------------


class TestAwsAdapterContract:
    @pytest.mark.asyncio
    async def test_get_recommendations_returns_cloud_list(self, monkeypatch):
        async def _fake(*, rows, region, days):
            return (
                [recommendation_to_cloud(_make_rec(savings=Decimal("5")))],
                build_aws_recommendation_summary(_report(recs=[_make_rec(savings=Decimal("5"))])),
                [],
            )

        monkeypatch.setattr(agg, "aggregate_aws_recommendations_for_accounts", _fake)
        provider = AWSCloudProvider()
        out = await provider.get_recommendations(
            accounts=[_row("111")], days=30, region="us-east-1"
        )
        assert len(out) == 1
        assert out[0].provider == CloudProviderKind.AWS

    def test_capability_optimization_supported(self):
        provider = AWSCloudProvider()
        caps = provider.get_capabilities()
        assert caps.has(route_capability())

    def test_registered_in_registry(self):
        registry = ProviderRegistry.current()
        adapter = registry.require(CloudProviderKind.AWS)
        assert isinstance(adapter, AWSCloudProvider)


def route_capability():
    from app.services.cloud.base import CloudCapability
    return CloudCapability.OPTIMIZATION


# ---------------------------------------------------------------------------
# 6. Cache isolation
# ---------------------------------------------------------------------------


class TestCacheIsolation:
    def test_aws_vs_azure_never_collide(self):
        aws = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=30)
        azure = _build_recommendation_cache_key(user_id=1, provider="AZURE", scope="all", region=None, days=30)
        assert aws != azure

    def test_all_vs_single_never_collide(self):
        all_aws = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=30)
        one_aws = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="acct:111", region=None, days=30)
        assert all_aws != one_aws

    def test_all_aws_vs_all_azure_never_collide(self):
        a = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=30)
        b = _build_recommendation_cache_key(user_id=1, provider="AZURE", scope="all", region=None, days=30)
        assert a != b

    def test_user_isolation(self):
        a = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=30)
        b = _build_recommendation_cache_key(user_id=2, provider="AWS", scope="all", region=None, days=30)
        assert a != b

    def test_region_isolation(self):
        a = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region="us-east-1", days=30)
        b = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region="us-west-2", days=30)
        assert a != b

    def test_days_isolation(self):
        a = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=7)
        b = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region=None, days=90)
        assert a != b

    def test_stable_for_identical_dimensions(self):
        a = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region="us-east-1", days=30)
        b = _build_recommendation_cache_key(user_id=1, provider="AWS", scope="all", region="us-east-1", days=30)
        assert a == b


# ---------------------------------------------------------------------------
# 7. Selector coercion (provider-specific)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def execute(self, _stmt):
        return _FakeResult(self._row)


class TestSelectorCoercion:
    def test_aws_rejects_azure_sub_selector(self):
        rows, scope, err = _resolve_rows(_FakeDB(None), provider=CloudProviderKind.AWS, selection="sub:abc")
        assert rows is None
        assert err is not None and err.status_code == 400
        assert b"ProviderSelectorMismatch" in err.body

    def test_azure_rejects_aws_acct_selector(self):
        rows, scope, err = _resolve_rows(_FakeDB(None), provider=CloudProviderKind.AZURE, selection="acct:5")
        assert rows is None
        assert err is not None and err.status_code == 400
        assert b"ProviderSelectorMismatch" in err.body

    def test_aws_accepts_acct_selector(self, monkeypatch):
        import app.services.aws.account_provider as ap

        monkeypatch.setattr(ap, "resolve_account_or_404", lambda db, sel, **kw: _row("111", "QA-Dev"))
        rows, scope, err = _resolve_rows(_FakeDB(None), provider=CloudProviderKind.AWS, selection="acct:1")
        assert err is None
        assert scope == "acct:111"
        assert rows and rows[0].account_id == "111"

    def test_azure_accepts_sub_selector(self):
        # A GUID sub:<id> is stripped and resolved by account_id.
        row = SimpleNamespace(account_id="guid-1", display_name="MCPP", enabled=True, provider="AZURE")
        rows, scope, err = _resolve_rows(_FakeDB(row), provider=CloudProviderKind.AZURE, selection="sub:guid-1")
        assert err is None
        assert scope == "sub:guid-1"
        assert rows and rows[0].account_id == "guid-1"


# ---------------------------------------------------------------------------
# 8. Route-level AWS fan-out
# ---------------------------------------------------------------------------


class _User:
    id = 1


class TestAwsRoute:
    @pytest.mark.asyncio
    async def test_aws_all_accounts_returns_canonical_envelope(self, monkeypatch):
        async def _fake(*, rows, region, days):
            recs = [
                _make_rec(rid="r1", account_id="111", savings=Decimal("10"), account_display_name="QA-Dev"),
                _make_rec(rid="r2", resource_id="i-2", account_id="222", savings=Decimal("20"), account_display_name="Prod"),
            ]
            return (
                [recommendation_to_cloud(r) for r in recs],
                build_aws_recommendation_summary(
                    _report(recs=recs, by_account=[("111", "QA-Dev", 1), ("222", "Prod", 1)])
                ),
                [],
            )

        monkeypatch.setattr(route_mod, "iter_enabled_accounts", lambda db, provider="AWS": [_row("111"), _row("222")])
        monkeypatch.setattr(route_mod, "aggregate_aws_recommendations_for_accounts", _fake)

        out = await get_cloud_recommendations(
            provider="AWS", account_id="all", region="us-east-1", days=30,
            user=_User(), db=None,
        )
        assert out["summary"]["provider"] == "AWS"
        assert out["summary"]["total_recommendations"] == 2
        accounts = {b["key"] for b in out["summary"]["by_account"]}
        assert accounts == {"111", "222"}
        assert out["summary"]["savings_period"] == "MONTHLY"

    @pytest.mark.asyncio
    async def test_aws_production_single_account(self, monkeypatch):
        async def _fake(*, rows, region, days):
            recs = [_make_rec(rid="r1", account_id="222", savings=Decimal("20"), account_display_name="Prod")]
            return (
                [recommendation_to_cloud(r) for r in recs],
                build_aws_recommendation_summary(_report(recs=recs, by_account=[("222", "Prod", 1)])),
                [],
            )

        import app.services.aws.account_provider as ap

        monkeypatch.setattr(ap, "resolve_account_or_404", lambda db, sel, **kw: _row("222", "Prod"))
        monkeypatch.setattr(route_mod, "aggregate_aws_recommendations_for_accounts", _fake)
        out = await get_cloud_recommendations(
            provider="AWS", account_id="acct:2", region="us-east-1", days=30,
            user=_User(), db=None,
        )
        assert out["summary"]["scope"] == "acct:222"
        assert out["summary"]["scope_kind"] == "single"

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self):
        out = await get_cloud_recommendations(
            provider="LINODE", account_id="all", region=None, days=30, user=_User(), db=None,
        )
        assert out.status_code == 400


# ---------------------------------------------------------------------------
# 9. Legacy AWS route compatibility
# ---------------------------------------------------------------------------


class TestLegacyAwsRouteCompat:
    # NOTE: nginx strips the ``/api`` prefix, so the backend route
    # paths are ``/aws/optimization/*`` and ``/cloud/recommendations``
    # (the public URLs add ``/api``).
    def test_legacy_paths_still_registered(self):
        from app.main import app

        paths = {r.path for r in app.routes}
        assert "/aws/optimization/recommendations" in paths
        assert "/aws/optimization/summary" in paths
        assert "/aws/optimization/capabilities" in paths

    def test_canonical_path_registered(self):
        from app.main import app

        paths = {r.path for r in app.routes}
        assert "/cloud/recommendations" in paths


# ---------------------------------------------------------------------------
# 10. Azure regression
# ---------------------------------------------------------------------------


class TestAzureRegression:
    def test_azure_translation_unchanged(self):
        from app.services.cloud.azure import advisor_aggregator as az_agg
        from app.services.cloud.azure.advisor import normalize_recommendation_row

        row = {
            "id": "/subscriptions/s1/resourcegroups/rg/providers/Microsoft.Advisor/recommendations/r1",
            "name": "r1",
            "type": "microsoft.advisor/recommendations",
            "subscriptionId": "s1",
            "properties": {
                "category": "Cost",
                "impact": "High",
                "recommendationStatus": "New",
                "recommendationTypeId": "e10b1381",
                "impactedField": "Microsoft.Compute/virtualMachines",
                "resourceMetadata": {"resourceId": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"},
                "shortDescription": {"problem": "Right-size", "solution": "Downsize"},
                "extendedProperties": {"annualSavingsAmount": "1488", "savingsCurrency": "USD", "recommendationSubCategory": "UsageOptimization"},
            },
        }
        rec = normalize_recommendation_row(row)
        cloud = az_agg.advisor_recommendations_to_cloud([rec])[0]
        assert cloud.provider == CloudProviderKind.AZURE
        assert cloud.source == "AZURE_ADVISOR"
        assert cloud.estimated_savings == Decimal("1488")
        # Phase 7.C.2D extensions preserved
        assert cloud.extensions["impact"] == "High"
        assert cloud.extensions["resource_group"] == "rg"
        # Phase 7D.1 generic columns also populated (additive)
        assert cloud.impact == "High"
        assert cloud.category == "Cost"

    def test_azure_envelope_retains_legacy_fields(self):
        from app.api.cloud_recommendations import _build_azure_envelope
        from app.services.cloud.azure.advisor_aggregator import AdvisorRollup
        from app.services.cloud.models import CloudRecommendation

        rollup = AdvisorRollup(
            status="SUCCESS",
            savings_status="OK",
            currency="USD",
            total_recommendations=1,
            recommendations_with_savings=1,
            recommendations_without_savings=0,
            potential_annual_savings=Decimal("1488"),
            savings_by_currency={"USD": Decimal("1488")},
            by_impact={"High": 1},
            by_resource_type={"Microsoft.Compute/virtualMachines": 1},
            by_subscription={"s1": 1},
            by_resource_group={"rg": 1},
            by_type={"e10b1381": 1},
            by_subcategory={"UsageOptimization": 1},
            failures=[],
        )
        rec = CloudRecommendation(
            provider=CloudProviderKind.AZURE,
            account_id="s1",
            region=None,
            recommendation_id="r1",
            resource_id="/subscriptions/s1/.../vm1",
            resource_type="Microsoft.Compute/virtualMachines",
            service="Microsoft.Compute/virtualMachines",
            action="e10b1381",
            title="Right-size",
            estimated_savings=Decimal("1488"),
            currency="USD",
            source="AZURE_ADVISOR",
            extensions={"impact": "High"},
        )
        env = _build_azure_envelope([rec], rollup, rows=[], scope="all", failures=[])
        assert env.summary.potential_annual_savings == Decimal("1488")
        assert env.summary.by_subscription[0].key == "s1"
        assert env.summary.savings_period == "ANNUAL"
        # generic aliases present but legacy fields unchanged
        assert env.summary.by_account[0].key == "s1"
        assert env.recommendations[0].source == "AZURE_ADVISOR"


# ---------------------------------------------------------------------------
# 11. No credential / token leakage
# ---------------------------------------------------------------------------


_FORBIDDEN = ("AKIA", "SecretAccessKey", "aws_secret", "Authorization", "Bearer ", "sk-", "session_token")


def _assert_clean(payload: dict) -> None:
    rendered = json.dumps(payload)
    for needle in _FORBIDDEN:
        assert needle not in rendered, f"leaked {needle!r}"


class TestNoSecretLeakage:
    def test_aws_envelope_has_no_credentials(self):
        recs = [_make_rec(account_id="111", savings=Decimal("10"))]
        rollup = build_aws_recommendation_summary(_report(recs=recs))
        flat = [recommendation_to_cloud(r) for r in recs]
        env = _build_aws_envelope(flat, rollup, scope="all")
        _assert_clean(env.model_dump(mode="json"))

    def test_account_error_message_is_sanitized(self):
        # The Phase 7B aggregator maps raw Boto3 errors onto fixed,
        # operator-readable strings.  Our envelope must carry those,
        # never the raw payload.
        err = AccountError("222", "Prod", "AccessDenied", "AWS denied access for this account")
        recs = [_make_rec(account_id="111", savings=Decimal("1"))]
        rollup = build_aws_recommendation_summary(_report(status="PARTIAL_SUCCESS", recs=recs, errors=[err]))
        flat = [recommendation_to_cloud(r) for r in recs]
        env = _build_aws_envelope(flat, rollup, scope="all")
        dumped = env.model_dump(mode="json")
        assert dumped["summary"]["account_errors"][0]["message"] == "AWS denied access for this account"
        _assert_clean(dumped)