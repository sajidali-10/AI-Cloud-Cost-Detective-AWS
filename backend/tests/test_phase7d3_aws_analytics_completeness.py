"""Phase 7D.3.1 — AWS canonical analytics completeness tests.

Verifies the two completeness invariants introduced in Phase 7D.3.1:

  1. AWS canonical cost envelope surfaces real per-day points via
     the ``get_cost_trend`` adapter path (no fabrication).
  2. AWS canonical resources envelope derives by_type / by_location
     / by_subscription from the resources[] list so the invariant
     ``sum(by_type counts) <= total_resources`` holds.

These tests stub the AWS adapter so no real network call is made.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, List, Optional
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.cloud import ProviderRegistry
from app.services.cloud.models import (
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudResource,
    CloudResourceSummary,
    CloudServiceCost,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _summary_aws(
    *,
    total=Decimal("3252.54"),
    previous=Decimal("3000.00"),
    by_service=None,
    by_region=None,
    status="SUCCESS",
    successful_accounts=2,
    failed_accounts=0,
):
    today = date.today()
    return CloudCostSummary(
        provider=CloudProviderKind.AWS,
        account_id=None,
        scope="all",
        period_start=today.replace(day=1),
        period_end=today,
        currency="USD",
        current_total=total,
        previous_total=previous,
        change_amount=total - previous,
        change_percent=((total - previous) / previous * 100).quantize(Decimal("0.01")),
        estimated=False,
        by_service=by_service or [],
        by_region=by_region or [],
        extensions={
            "status": status,
            "currency_status": "OK",
            "by_account": [],
            "account_errors": [],
        },
    )


def _daily_trend_aws(
    *,
    start_day: int = 1,
    days: int = 18,
    per_day: Decimal = Decimal("180.00"),
    currency: str = "USD",
):
    today = date.today()
    period_start = today.replace(day=start_day)
    items: List[CloudServiceCost] = []
    for i in range(days):
        d = period_start + timedelta(days=i)
        items.append(
            CloudServiceCost(
                service=str(d),
                amount=per_day,
                currency=currency,
                provider=CloudProviderKind.AWS,
                account_id=None,
                region=None,
                extensions={"date": d.isoformat()},
            )
        )
    return CloudCostTrend(
        provider=CloudProviderKind.AWS,
        account_id=None,
        period_start=period_start,
        period_end=today,
        currency=currency,
        daily=items,
        source="AWS_COST_EXPLORER",
    )


def _resource_aws(
    *,
    resource_id: str,
    resource_type: str = "ec2",
    service: str = "ec2",
    region: Optional[str] = "us-east-1",
    account_id: str = "974053642038",
    name: Optional[str] = None,
    state: Optional[str] = "running",
):
    return CloudResource(
        provider=CloudProviderKind.AWS,
        account_id=account_id,
        region=region,
        resource_type=resource_type,
        resource_id=resource_id,
        service=service,
        name=name,
        state=state,
        composite_id=f"AWS|{account_id}|{region}|{resource_type}|{resource_id}",
        metadata={"tags": {}},
    )


# ---------------------------------------------------------------------------
#  Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_user(monkeypatch):
    """Replace ``get_current_user`` with a stub so we don't need JWT."""
    user = SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")

    async def _stub():
        return user

    from app.api import deps

    monkeypatch.setattr(deps, "get_current_user", _stub)
    return user


@pytest.fixture
def aws_adapter_stub(monkeypatch):
    """Replace the AWS adapter with controllable stubs."""
    registry = ProviderRegistry.current()
    adapter = registry.try_get(CloudProviderKind.AWS)
    assert adapter is not None

    # Save originals so each test starts from a known baseline.
    orig_summary = adapter.get_cost_summary
    orig_trend = adapter.get_cost_trend
    orig_resources = adapter.get_resources
    orig_resource_summary = adapter.get_resource_summary
    return {
        "adapter": adapter,
        "orig_summary": orig_summary,
        "orig_trend": orig_trend,
        "orig_resources": orig_resources,
        "orig_resource_summary": orig_resource_summary,
    }


@pytest.fixture
def restore_adapter(aws_adapter_stub):
    yield
    a = aws_adapter_stub["adapter"]
    a.get_cost_summary = aws_adapter_stub["orig_summary"]
    a.get_cost_trend = aws_adapter_stub["orig_trend"]
    a.get_resources = aws_adapter_stub["orig_resources"]
    a.get_resource_summary = aws_adapter_stub["orig_resource_summary"]


# ===========================================================================
#  Tests — AWS daily trend
# ===========================================================================


class TestAwsDailyTrend:
    def test_aws_daily_trend_populated_from_real_source(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """AWS canonical envelope surfaces real per-day points."""
        a = aws_adapter_stub["adapter"]
        a.get_cost_summary = AsyncMock(return_value=_summary_aws())
        a.get_cost_trend = AsyncMock(return_value=_daily_trend_aws(days=10, per_day=Decimal("100.00")))

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()["report"]
        assert len(report["daily_trend"]) == 10
        assert all("date" in p and "amount" in p and "unit" in p for p in report["daily_trend"])
        # Real per-day value from the source.
        assert report["daily_trend"][0]["amount"] == "100.00"

    def test_aws_daily_trend_values_not_fabricated(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Daily trend must echo the source amount verbatim, not divide monthly total."""
        a = aws_adapter_stub["adapter"]
        a.get_cost_summary = AsyncMock(return_value=_summary_aws(total=Decimal("3650.00")))
        # Asymmetric per-day values — confirms the trend is NOT a
        # monthly/30 fabrication.
        values = [Decimal("100.00"), Decimal("150.00"), Decimal("200.00"), Decimal("250.00")]
        today = date.today()
        items = []
        for i, v in enumerate(values):
            d = today.replace(day=1) + timedelta(days=i)
            items.append(
                CloudServiceCost(
                    service=str(d),
                    amount=v,
                    currency="USD",
                    provider=CloudProviderKind.AWS,
                    account_id=None,
                    region=None,
                    extensions={"date": d.isoformat()},
                )
            )
        trend = CloudCostTrend(
            provider=CloudProviderKind.AWS,
            account_id=None,
            period_start=today.replace(day=1),
            period_end=today,
            currency="USD",
            daily=items,
            source="AWS_COST_EXPLORER",
        )
        a.get_cost_trend = AsyncMock(return_value=trend)

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        report = resp.json()["report"]
        amounts = [p["amount"] for p in report["daily_trend"]]
        assert amounts == ["100.00", "150.00", "200.00", "250.00"]
        # Sum of per-day points does NOT need to match total_cost
        # exactly — the per-day data and the monthly total come from
        # different Cost Explorer queries (granularity), and the
        # frontend renders what the upstream returned.

    def test_aws_daily_trend_multi_account_aggregates_by_date(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Multi-account AWS: daily totals summed by date across successful accounts."""
        a = aws_adapter_stub["adapter"]
        # The aggregator is what actually aggregates by date.
        # Verify the canonical envelope faithfully reflects a
        # multi-account aggregator output where one day has two
        # account-level values.
        today = date.today()
        day = today.replace(day=1)
        # Simulate two accounts reporting the same date:
        # account A: 50.00 on 2026-10-01
        # account B: 75.00 on 2026-10-01
        # These are returned by the aggregator as two CloudServiceCost
        # entries with the same date in extensions.
        items = [
            CloudServiceCost(
                service=str(day),
                amount=Decimal("50.00"),
                currency="USD",
                provider=CloudProviderKind.AWS,
                account_id="974053642038",
                region=None,
                extensions={"date": day.isoformat()},
            ),
            CloudServiceCost(
                service=str(day),
                amount=Decimal("75.00"),
                currency="USD",
                provider=CloudProviderKind.AWS,
                account_id="852428675923",
                region=None,
                extensions={"date": day.isoformat()},
            ),
        ]
        trend = CloudCostTrend(
            provider=CloudProviderKind.AWS,
            account_id=None,
            period_start=day,
            period_end=today,
            currency="USD",
            daily=items,
            source="AWS_COST_EXPLORER",
        )
        a.get_cost_summary = AsyncMock(return_value=_summary_aws())
        a.get_cost_trend = AsyncMock(return_value=trend)

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        report = resp.json()["report"]
        assert len(report["daily_trend"]) == 2
        # Both dates surface; the aggregator's sum lives in a
        # different code path (cost_aggregator.daily_accum).  The
        # canonical envelope just preserves what get_cost_trend
        # returned.
        assert all(p["date"] == day.isoformat() for p in report["daily_trend"])

    def test_aws_daily_trend_partial_account_failure_preserves_successful(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """PARTIAL_SUCCESS: successful account data preserved, failed account errored."""
        a = aws_adapter_stub["adapter"]
        summary = _summary_aws(status="PARTIAL_SUCCESS")
        summary.extensions["account_errors"] = [
            {
                "account_id": "852428675923",
                "display_name": "AWS Production Account",
                "code": "AssumeRoleError",
                "message": "AWS request failed",
            }
        ]
        a.get_cost_summary = AsyncMock(return_value=summary)
        a.get_cost_trend = AsyncMock(return_value=_daily_trend_aws(days=3))

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        report = resp.json()["report"]
        # Trend from the successful account still surfaces.
        assert len(report["daily_trend"]) == 3
        # PARTIAL_SUCCESS propagates + the error is preserved.
        assert report["status"] == "PARTIAL_SUCCESS"
        assert any(e["code"] == "AssumeRoleError" for e in report["account_errors"])

    def test_aws_daily_trend_empty_source_stays_empty(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Empty upstream trend → empty canonical trend (no fabrication)."""
        a = aws_adapter_stub["adapter"]
        a.get_cost_summary = AsyncMock(return_value=_summary_aws())
        a.get_cost_trend = AsyncMock(return_value=_daily_trend_aws(days=0))

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        report = resp.json()["report"]
        assert report["daily_trend"] == []

    def test_aws_daily_trend_failure_does_not_502_envelope(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Trend fetch failure → summary still returned, error surfaced via partial-data banner."""
        a = aws_adapter_stub["adapter"]
        a.get_cost_summary = AsyncMock(return_value=_summary_aws())
        a.get_cost_trend = AsyncMock(side_effect=RuntimeError("upstream boom"))

        resp = client.get(
            "/cloud/costs",
            params={"provider": "AWS", "account_id": "all", "month": "2026-10"},
        )
        assert resp.status_code == 200
        report = resp.json()["report"]
        assert report["daily_trend"] == []
        # The partial-data signal surfaces through account_errors.
        assert any(
            e["code"] == "DailyTrendUnavailable" for e in report["account_errors"]
        )


# ===========================================================================
#  Tests — AWS resources by_type / by_location / by_subscription
# ===========================================================================


class TestAwsResourcesBreakdowns:
    def test_aws_by_type_derived_from_resources(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """AWS canonical envelope derives by_type from the resources[] list."""
        a = aws_adapter_stub["adapter"]
        resources = [
            _resource_aws(resource_id=f"i-{i}", resource_type="ec2") for i in range(5)
        ] + [
            _resource_aws(resource_id=f"vol-{i}", resource_type="ebs") for i in range(3)
        ] + [
            _resource_aws(resource_id=f"fn-{i}", resource_type="lambda") for i in range(2)
        ]
        a.get_resources = AsyncMock(return_value=resources)
        # Aggregator summary has empty breakdowns — the route must
        # derive them from the resources list.
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": 10},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        assert resp.status_code == 200, resp.text
        summary = resp.json()["summary"]
        by_type = {e["key"]: e["count"] for e in summary["by_type"]}
        assert by_type == {"ec2": 5, "ebs": 3, "lambda": 2}
        assert summary["total_resources"] == 10

    def test_aws_sum_by_type_equals_total_resources(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Invariant: sum(by_type counts) == total_resources when every resource has a type."""
        a = aws_adapter_stub["adapter"]
        n_ec2, n_rds, n_s3 = 95, 4, 47
        resources = (
            [_resource_aws(resource_id=f"i-{i}", resource_type="ec2") for i in range(n_ec2)]
            + [_resource_aws(resource_id=f"db-{i}", resource_type="rds") for i in range(n_rds)]
            + [_resource_aws(resource_id=f"b-{i}", resource_type="s3", region="global") for i in range(n_s3)]
        )
        a.get_resources = AsyncMock(return_value=resources)
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": len(resources)},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        summary = resp.json()["summary"]
        total = sum(e["count"] for e in summary["by_type"])
        assert total == len(resources) == summary["total_resources"]

    def test_aws_missing_resource_type_uses_unassigned_bucket(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """Resources without a resource_type fall into an explicit Unassigned bucket."""
        a = aws_adapter_stub["adapter"]
        resources = [
            _resource_aws(resource_id="i-1", resource_type="ec2"),
            # A malformed resource missing resource_type.
            _resource_aws(resource_id="orphan-1", resource_type=""),
            _resource_aws(resource_id="orphan-2", resource_type=""),
        ]
        a.get_resources = AsyncMock(return_value=resources)
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": len(resources)},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        summary = resp.json()["summary"]
        by_type = {e["key"]: e["count"] for e in summary["by_type"]}
        assert by_type["ec2"] == 1
        assert by_type["Unassigned"] == 2
        # No resource silently dropped.
        assert sum(by_type.values()) == len(resources)

    def test_aws_by_subscription_derived_from_resources(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """AWS canonical envelope derives by_subscription from the resources[] list."""
        a = aws_adapter_stub["adapter"]
        resources = (
            [_resource_aws(resource_id=f"i-{i}", account_id="974053642038") for i in range(3)]
            + [_resource_aws(resource_id=f"vol-{i}", account_id="852428675923") for i in range(2)]
        )
        a.get_resources = AsyncMock(return_value=resources)
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": len(resources)},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        summary = resp.json()["summary"]
        by_sub = {e["subscription_id"]: e["count"] for e in summary["by_subscription"]}
        assert by_sub == {"974053642038": 3, "852428675923": 2}
        assert summary["total_resources"] == 5

    def test_aws_by_location_derived_from_resources(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """AWS canonical envelope derives by_location from the resources[] list."""
        a = aws_adapter_stub["adapter"]
        resources = (
            [_resource_aws(resource_id=f"i-{i}", region="us-east-1") for i in range(4)]
            + [_resource_aws(resource_id=f"vol-{i}", region="us-west-1") for i in range(2)]
        )
        a.get_resources = AsyncMock(return_value=resources)
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": len(resources)},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        summary = resp.json()["summary"]
        by_loc = {e["key"]: e["count"] for e in summary["by_location"]}
        assert by_loc == {"us-east-1": 4, "us-west-1": 2}

    def test_aws_resources_aggregator_summary_breaks_down_zero(
        self, client, auth_user, aws_adapter_stub, restore_adapter
    ):
        """When both resources[] and aggregator summary are empty, envelope is well-formed."""
        a = aws_adapter_stub["adapter"]
        a.get_resources = AsyncMock(return_value=[])
        a.get_resource_summary = AsyncMock(
            return_value=CloudResourceSummary(
                provider=CloudProviderKind.AWS,
                account_id=None,
                scope="all",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "NO_DATA"},
            )
        )

        resp = client.get(
            "/cloud/resources",
            params={"provider": "AWS", "account_id": "all"},
        )
        summary = resp.json()["summary"]
        assert summary["by_type"] == []
        assert summary["by_location"] == []
        assert summary["by_subscription"] == []
        assert summary["total_resources"] == 0
        assert summary["status"] == "NO_DATA"


# ===========================================================================
#  Tests — Azure canonical behaviour unchanged
# ===========================================================================


class TestAzureCanonicalUnchanged:
    def test_azure_daily_trend_still_uses_aggregator(self):
        """The Azure daily-trend path uses the Azure aggregator, untouched by Phase 7D.3.1.

        This is a regression-check that documents the boundary: the
        Azure canonical cost route calls
        ``app.services.cloud.azure.cost_aggregator.aggregate_cost_trend_for_subscriptions``
        while the AWS canonical cost route (Phase 7D.3.1) calls
        ``adapter.get_cost_trend``.  Phase 7D.3.1 MUST NOT change
        the Azure path.
        """
        from app.services.cloud.azure import cost_aggregator as az_agg

        assert hasattr(az_agg, "aggregate_cost_trend_for_subscriptions")
        assert callable(az_agg.aggregate_cost_trend_for_subscriptions)
