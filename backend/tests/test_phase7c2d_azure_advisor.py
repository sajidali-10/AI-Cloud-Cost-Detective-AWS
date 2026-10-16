"""Phase 7C.2D — Azure Advisor client + aggregator tests.

Covers:

* KQL construction (Cost + Active filters, no limit/take/sample)
* Status / pagination / pathological-detection
* Single / multi / tenant fan-out
* Per-subscription attribution
* Mixed-currency guard
* Savings invariants (only explicit annualSavingsAmount)
* Aggregation invariants
* Recommendation dedup
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from app.services.cloud.azure import advisor as advisor_mod
from app.services.cloud.azure.advisor import (
    ACTIVE_STATUS,
    ADVISOR_RECOMMENDATION_TYPE,
    AzureAdvisorClient,
    AzureAdvisorError,
    AzureAdvisorErrorCategory,
    AzureAdvisorRecommendation,
    MAX_PAGES,
    PAGE_TOP,
    _build_active_cost_query,
    normalize_recommendation_row,
    parse_retry_after_header,
)
from app.services.cloud.azure import advisor_aggregator as agg
from app.services.cloud.azure.advisor_aggregator import (
    AdvisorRollup,
    AzureAdvisorSubscriptionFailure,
    AzureSubscriptionAdvisorSnapshot,
    AdvisorRollup,
    aggregate_advisor_recommendations_for_subscriptions,
    build_advisor_summary,
    normalize_recommendation_row as _normalize_for_aggregator,
)
from app.services.cloud.models import CloudProviderKind


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_api_version_pinned():
    assert advisor_mod.RESOURCE_GRAPH_API_VERSION == "2024-04-01"


def test_page_top_bounded():
    assert 0 < advisor_mod.PAGE_TOP <= 1000


def test_max_pages_bounded():
    assert 0 < advisor_mod.MAX_PAGES <= 100


# ---------------------------------------------------------------------------
# KQL contract
# ---------------------------------------------------------------------------


class TestAdvisorKql:
    def test_uses_advisorresources_table(self):
        q = _build_active_cost_query()
        assert q.startswith("advisorresources ")

    def test_filters_cost_category(self):
        q = _build_active_cost_query()
        assert 'properties.category == "Cost"' in q

    def test_filters_active_status(self):
        q = _build_active_cost_query()
        assert f'properties.recommendationStatus == "{ACTIVE_STATUS}"' in q

    def test_no_limit_take_sample(self):
        q = _build_active_cost_query()
        for forbidden in ("| limit ", "| take ", "| sample "):
            assert forbidden.lower() not in q.lower()

    def test_projects_required_columns(self):
        q = _build_active_cost_query()
        for col in ("subscriptionId", "id", "name", "type", "properties"):
            assert col in q


# ---------------------------------------------------------------------------
# ARM-id decomposition
# ---------------------------------------------------------------------------


class TestSplitArmResourceId:
    @pytest.mark.parametrize(
        "arm,expected",
        [
            (
                "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
                ("sub-aaaa", "rg1", "Microsoft.Compute/virtualMachines/vm1"),
            ),
            (
                "/subscriptions/sub-aaaa/providers/Microsoft.Consumption/budgets/b1",
                ("sub-aaaa", None, "Microsoft.Consumption/budgets/b1"),
            ),
            ("/subscriptions/sub-aaaa", ("sub-aaaa", None, None)),
            ("", (None, None, None)),
        ],
    )
    def test_split_arm_resource_id(self, arm, expected):
        assert advisor_mod._split_arm_resource_id(arm) == expected


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------


class TestCoerceDecimal:
    def test_valid_numeric_string(self):
        assert advisor_mod._coerce_decimal("123.45") == Decimal("123.45")

    def test_blank_string_is_none(self):
        assert advisor_mod._coerce_decimal("") is None
        assert advisor_mod._coerce_decimal("   ") is None

    def test_garbage_string_is_none(self):
        assert advisor_mod._coerce_decimal("not-a-number") is None

    def test_none_is_none(self):
        assert advisor_mod._coerce_decimal(None) is None

    def test_decimal_passthrough(self):
        assert advisor_mod._coerce_decimal(Decimal("7.5")) == Decimal("7.5")

    def test_bool_rejected(self):
        assert advisor_mod._coerce_decimal(True) is None


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def test_retry_after_integer():
    assert parse_retry_after_header("30") == 30


def test_retry_after_blank_none():
    assert parse_retry_after_header("") is None
    assert parse_retry_after_header(None) is None


# ---------------------------------------------------------------------------
# Stub transport for live HTTP behaviour
# ---------------------------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls: list = []
        self.responses: list = []
        self._call_count = 0

    def push(self, status: int, body: Optional[dict] = None, headers: Optional[dict] = None):
        self.responses.append((status, body, headers or {}))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        idx = self._call_count
        self._call_count += 1
        if idx >= len(self.responses):
            raise AssertionError(f"transport stub ran out of canned responses at call {idx}")
        status, body, headers = self.responses[idx]
        return httpx.Response(
            status_code=status,
            json=body if body is not None else {},
            headers=headers,
            request=request,
        )


@pytest.fixture
def patch_token(monkeypatch):
    monkeypatch.setattr(
        advisor_mod,
        "acquire_access_token",
        lambda **kwargs: "test-token-not-real",
    )


def _make_client(transport, **kw):
    client = AzureAdvisorClient(**kw)
    client._http = httpx.Client(
        transport=transport, base_url="https://management.azure.com"
    )
    return client


def _advisor_row(
    *,
    rec_id: str = "/subscriptions/sub-aaaa/resourcegroups/rg1/providers/Microsoft.Advisor/recommendations/r1",
    sub: str = "sub-aaaa",
    rg: str = "rg1",
    type_id: str = "e10b1381",
    annual: str = "100",
    currency: str = "USD",
    impact: str = "Medium",
    status: str = "Active",
    subcategory: str = "UsageOptimization",
    target_rid: Optional[str] = None,
):
    target = (
        target_rid
        if target_rid is not None
        else f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/vm1"
    )
    return {
        "id": rec_id,
        "name": "rec-1",
        "type": ADVISOR_RECOMMENDATION_TYPE,
        "subscriptionId": sub,
        "properties": {
            "category": "Cost",
            "impact": impact,
            "recommendationStatus": status,
            "recommendationTypeId": type_id,
            "impactedField": "Microsoft.Compute/virtualMachines",
            "lastUpdated": "2026-01-01T00:00:00Z",
            "resourceMetadata": {"resourceId": target},
            "shortDescription": {
                "problem": "Right-size or shutdown underutilized virtual machines",
                "solution": "Stop or downsize the VM",
            },
            "extendedProperties": {
                "annualSavingsAmount": annual,
                "savingsCurrency": currency,
                "recommendationSubCategory": subcategory,
                "term": "P1Y",
                "lookbackPeriod": "P30D",
            },
        },
    }


def _page_body(rows, *, truncated=False, skip_token=None, total_records=None):
    if total_records is None:
        total_records = len(rows) if not truncated else len(rows) + 1000
    body = {"totalRecords": total_records, "count": len(rows), "data": rows}
    if truncated:
        body["resultTruncated"] = True
    if skip_token is not None:
        body["$skipToken"] = skip_token
    return body


# ---------------------------------------------------------------------------
# KQL injection defence
# ---------------------------------------------------------------------------


class TestKqlInjectionDefence:
    def test_subscription_ids_only_accept_strings(self, patch_token):
        transport = _StubTransport()
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["ok", ""])
            assert exc_info.value.category == AzureAdvisorErrorCategory.INVALID_RESPONSE
        finally:
            client.close()

    def test_no_subscription_ids_runs_tenant_query(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([]))
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=[])
            assert rows == []
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    def test_200_returns_rows(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([_advisor_row()]))
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert len(rows) == 1
        finally:
            client.close()

    def test_204_returns_empty(self, patch_token):
        transport = _StubTransport()
        transport.push(204)
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert rows == []
        finally:
            client.close()

    def test_401_raises_auth_failed(self, patch_token):
        transport = _StubTransport()
        transport.push(401)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.AUTH_FAILED
        finally:
            client.close()

    def test_403_raises_access_denied(self, patch_token):
        transport = _StubTransport()
        transport.push(403)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.ACCESS_DENIED
        finally:
            client.close()

    def test_429_carries_retry_after(self, patch_token):
        transport = _StubTransport()
        transport.push(429, headers={"Retry-After": "30"})
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.THROTTLED
            assert exc_info.value.retry_after == 30
        finally:
            client.close()

    def test_5xx_raises_upstream_error(self, patch_token):
        transport = _StubTransport()
        transport.push(503)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.UPSTREAM_ERROR
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_first_page_no_token(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([_advisor_row(rec_id="/r/r1")]))
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert len(rows) == 1
            assert len(transport.calls) == 1
        finally:
            client.close()

    def test_first_page_with_skip_token(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r1")], truncated=True, skip_token="cursor-2"
        ))
        transport.push(200, _page_body([_advisor_row(rec_id="/r/r2")]))
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert len(rows) == 2
            body2 = json.loads(transport.calls[1].content.decode("utf-8"))
            assert body2["options"]["$skipToken"] == "cursor-2"
        finally:
            client.close()

    def test_subscriptions_preserved_across_pages(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r1", sub="sub-aaaa")],
            truncated=True, skip_token="cursor-2",
        ))
        transport.push(200, _page_body([_advisor_row(rec_id="/r/r2", sub="sub-aaaa")]))
        client = _make_client(transport)
        try:
            client.query_active_cost_recommendations(
                subscription_ids=["sub-aaaa", "sub-bbbb"]
            )
            body1 = json.loads(transport.calls[0].content.decode("utf-8"))
            body2 = json.loads(transport.calls[1].content.decode("utf-8"))
            assert body1["subscriptions"] == ["sub-aaaa", "sub-bbbb"]
            assert body2["subscriptions"] == ["sub-aaaa", "sub-bbbb"]
            assert body1["query"] == body2["query"]
            assert body1["options"]["$top"] == body2["options"]["$top"]
        finally:
            client.close()

    def test_repeated_skip_token_protection(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r1")], truncated=True, skip_token="cursor-A"
        ))
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r2")], truncated=True, skip_token="cursor-A"
        ))
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.UPSTREAM_ERROR
        finally:
            client.close()

    def test_truncated_without_token_raises(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r1"), _advisor_row(rec_id="/r/r2")],
            truncated=True, skip_token=None, total_records=100,
        ))
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.PAGINATION_INCOMPLETE
        finally:
            client.close()

    def test_truncated_but_complete_accepted(self, patch_token):
        """Resource Graph can return resultTruncated=true AND
        count == totalRecords.  The client accepts this as the
        success boundary."""
        transport = _StubTransport()
        transport.push(200, _page_body(
            [_advisor_row(rec_id="/r/r1"), _advisor_row(rec_id="/r/r2")],
            truncated=True, skip_token=None, total_records=2,
        ))
        client = _make_client(transport)
        try:
            rows = client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert len(rows) == 2
        finally:
            client.close()

    def test_max_pages_guard(self, monkeypatch, patch_token):
        monkeypatch.setattr(advisor_mod, "MAX_PAGES", 3)
        transport = _StubTransport()
        for i in range(10):
            transport.push(200, _page_body(
                [_advisor_row(rec_id=f"/r/r{i}")],
                truncated=True, skip_token=f"cursor-{i}",
            ))
        client = AzureAdvisorClient(max_pages=3)
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureAdvisorErrorCategory.UPSTREAM_ERROR
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


class TestSanitisation:
    def test_no_token_in_exception(self, patch_token):
        transport = _StubTransport()
        transport.push(401)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureAdvisorError) as exc_info:
                client.query_active_cost_recommendations(
                    subscription_ids=["a54d076b-2650-4b4f-a28c-2deed2011f83"]
                )
            rendered = str(exc_info.value)
            assert "test-token" not in rendered
            assert "Bearer" not in rendered
            assert "a54d076b" in rendered
            assert "2650-4b4f-a28c-2deed2011f83" not in rendered
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------


def _rec(**kw):
    """Convenience wrapper around ``_advisor_row`` returning a normalised
    :class:`AzureAdvisorRecommendation` directly."""
    return normalize_recommendation_row(_advisor_row(**kw))


class TestNormalizeRow:
    def test_basic_normalisation(self):
        rec = _rec(
            rec_id="/subscriptions/sub-aaaa/resourcegroups/rg1/providers/Microsoft.Advisor/recommendations/r1",
            sub="sub-aaaa",
            rg="rg1",
            type_id="e10b1381",
            annual="1488",
            currency="USD",
            impact="High",
        )
        assert rec.recommendation_id == (
            "/subscriptions/sub-aaaa/resourcegroups/rg1/providers/Microsoft.Advisor/recommendations/r1"
        )
        assert rec.subscription_id == "sub-aaaa"
        assert rec.resource_group == "rg1"
        assert rec.category == "Cost"
        assert rec.impact == "High"
        assert rec.recommendation_status == "Active"
        assert rec.recommendation_type_id == "e10b1381"
        assert rec.subcategory == "UsageOptimization"
        assert rec.annual_savings == Decimal("1488")
        assert rec.savings_currency == "USD"
        assert rec.term == "P1Y"
        assert rec.lookback_period == "P30D"

    def test_missing_annual_savings_retained(self):
        rec = _rec(annual="", type_id="missing")
        assert rec.annual_savings is None
        assert rec.savings_currency == "USD"  # currency may still be set
        assert rec.recommendation_type_id == "missing"

    def test_malformed_annual_savings_is_none(self):
        rec = _rec(annual="not-a-number")
        assert rec.annual_savings is None

    def test_missing_savings_currency_retained(self):
        rec = _rec(currency="")
        assert rec.savings_currency is None
        assert rec.annual_savings == Decimal("100")  # value still parsed

    def test_title_uses_short_description_problem(self):
        rec = _rec()
        assert rec.title == (
            "Right-size or shutdown underutilized virtual machines"
        )
        assert rec.solution == "Stop or downsize the VM"

    def test_resource_id_uses_resource_metadata(self):
        rec = _rec(
            rec_id="/subscriptions/sub-aaaa/.../Advisor/recommendations/r1",
            target_rid="/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
        )
        assert rec.resource_id == (
            "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"
        )

    def test_resource_type_uses_impacted_field(self):
        rec = _rec()
        assert rec.resource_type == "Microsoft.Compute/virtualMachines"


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _snap(*, sub="sub-aaaa", recs=(), error=None, display_name="Sub"):
    return AzureSubscriptionAdvisorSnapshot(
        subscription_id=sub,
        display_name=display_name,
        status="ERROR" if error else ("SUCCESS" if recs else "NO_DATA"),
        recommendations=recs,
        error=error,
    )


class TestAggregatorStatus:
    def test_empty_rows_returns_no_data(self):
        rollup = build_advisor_summary([])
        assert rollup.status == "NO_DATA"
        assert rollup.savings_status == "EMPTY"
        assert rollup.total_recommendations == 0

    def test_success_with_data(self):
        recs = (_rec(annual="100", currency="USD"),)
        rollup = build_advisor_summary([_snap(sub="sub-aaaa", recs=recs)])
        assert rollup.status == "SUCCESS"
        assert rollup.savings_status == "OK"
        assert rollup.potential_annual_savings == Decimal("100")
        assert rollup.currency == "USD"

    def test_partial_success_with_failures(self):
        recs = (_rec(annual="100", currency="USD"),)
        failure = AzureAdvisorSubscriptionFailure(
            subscription_id="sub-bbbb", display_name="Sponsorship",
            category="THROTTLED", status_code=429, message="THROTTLED",
        )
        rollup = build_advisor_summary([
            _snap(sub="sub-aaaa", recs=recs),
            _snap(sub="sub-bbbb", error=failure),
        ])
        assert rollup.status == "PARTIAL_SUCCESS"


class TestAggregatorSavings:
    def test_uniform_currency_summed(self):
        recs = (
            _rec(rec_id="/r/u1", annual="100", currency="USD"),
            _rec(rec_id="/r/u2", annual="250", currency="USD"),
        )
        rollup = build_advisor_summary([_snap(recs=recs)])
        assert rollup.savings_status == "OK"
        assert rollup.currency == "USD"
        assert rollup.potential_annual_savings == Decimal("350")
        assert rollup.savings_by_currency == {"USD": Decimal("350")}
        assert rollup.recommendations_with_savings == 2
        assert rollup.recommendations_without_savings == 0

    def test_mixed_currency_returns_mixed_status_no_total(self):
        recs = (
            _rec(rec_id="/r/m1", annual="100", currency="USD"),
            _rec(rec_id="/r/m2", annual="200", currency="EUR"),
        )
        rollup = build_advisor_summary([_snap(recs=recs)])
        assert rollup.savings_status == "MIXED_CURRENCY"
        assert rollup.potential_annual_savings is None
        assert rollup.currency is None
        assert rollup.savings_by_currency == {
            "USD": Decimal("100"), "EUR": Decimal("200"),
        }
        assert rollup.recommendations_with_savings == 2

    def test_recommendations_without_savings_retained(self):
        recs = (
            _rec(rec_id="/r/w1", annual="100", currency="USD"),
            _rec(rec_id="/r/w2", annual="", currency=""),
        )
        rollup = build_advisor_summary([_snap(recs=recs)])
        assert rollup.savings_status == "OK"
        assert rollup.potential_annual_savings == Decimal("100")
        assert rollup.recommendations_with_savings == 1
        assert rollup.recommendations_without_savings == 1
        # Zero savings != missing savings: the recommendation
        # WITHOUT savings is still counted in total_recommendations
        # so the UI renders an "untitled" row.
        assert rollup.total_recommendations == 2

    def test_zero_savings_distinct_from_missing(self):
        recs = (
            _rec(rec_id="/r/z1", annual="0", currency="USD"),
            _rec(rec_id="/r/z2", annual="", currency=""),
        )
        rollup = build_advisor_summary([_snap(recs=recs)])
        assert rollup.savings_status == "OK"
        # 0 is a valid Decimal that counts as "with savings"
        assert rollup.recommendations_with_savings == 1
        assert rollup.recommendations_without_savings == 1
        assert rollup.potential_annual_savings == Decimal("0")


class TestAggregatorDedup:
    def test_duplicate_recommendation_id_collapsed(self):
        recs = (
            _rec(rec_id="/r/r1", sub="sub-aaaa", annual="100"),
            _rec(rec_id="/r/r1", sub="sub-aaaa", annual="100"),
        )
        rollup = build_advisor_summary([_snap(recs=recs)])
        assert rollup.total_recommendations == 1
        assert rollup.potential_annual_savings == Decimal("100")


class TestAggregatorInvariants:
    def test_invariants_hold_for_realistic_dual_subscription(self):
        # MCPP: 1 rec @ $100, Sponsorship: 1 rec @ $200
        mcpp_rec = _rec(
            rec_id="/r/m1", sub="m", annual="100", currency="USD",
            impact="High",
        )
        sponsor_rec = _rec(
            rec_id="/r/s1", sub="s", annual="200", currency="USD",
            impact="Medium",
        )
        rollup = build_advisor_summary([
            _snap(sub="m", recs=(mcpp_rec,), display_name="MCPP"),
            _snap(sub="s", recs=(sponsor_rec,), display_name="Sponsorship"),
        ])
        assert rollup.total_recommendations == 2
        assert rollup.potential_annual_savings == Decimal("300")
        assert sum(v for v in rollup.by_impact.values()) == 2
        assert sum(v for v in rollup.by_resource_type.values()) == 2
        assert sum(v for v in rollup.by_subscription.values()) == 2
        assert sum(v for v in rollup.by_resource_group.values()) == 2
        assert sum(v for v in rollup.by_type.values()) == 2

    def test_subscription_level_recommendation_lands_in_unassigned(self):
        # Recommendations with no resource group (e.g. subscription-
        # scoped recommendations) bucket under "Unassigned".
        rec = _rec(
            rec_id="/r/sub-scoped", sub="sub-aaaa", rg=None,
            target_rid="/subscriptions/sub-aaaa",  # sub-only ARM id
            annual="50", currency="USD",
        )
        rollup = build_advisor_summary([_snap(recs=(rec,))])
        assert "Unassigned" in rollup.by_resource_group
        assert rollup.by_resource_group["Unassigned"] == 1
        assert sum(rollup.by_resource_group.values()) == 1


class TestAggregatorNoRawPropertiesLeakage:
    def test_recommendation_does_not_carry_properties_or_extended_properties(self):
        rec = _rec(annual="100", currency="USD")
        # ``raw`` is a controlled metadata bag — no Azure
        # ``properties`` or ``extendedProperties`` blob.
        assert "properties" not in rec.raw
        assert "extendedProperties" not in rec.raw


class TestAdvisorRecToCloud:
    def test_provider_neutral_translation(self):
        rec = _rec(annual="100", currency="USD", impact="High")
        cloud_recs = agg.advisor_recommendations_to_cloud([rec])
        assert len(cloud_recs) == 1
        cr = cloud_recs[0]
        assert cr.provider == CloudProviderKind.AZURE
        assert cr.source == "AZURE_ADVISOR"
        assert cr.estimated_savings == Decimal("100")
        assert cr.currency == "USD"
        # Subscriptions + impact + subcategory propagate via
        # extensions so the route can build typed breakdowns.
        assert cr.extensions["impact"] == "High"
        assert cr.extensions["recommendation_status"] == "Active"
        assert cr.extensions["category"] == "Cost"
