"""Phase 7C.2B — provider-neutral Cloud cost route tests.

Exercises the canonical ``GET /cloud/costs`` endpoint via
FastAPI's TestClient.  Stubs the provider adapter so no real
network call is made; the assertions focus on envelope shape,
cache-key isolation, and error mapping.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.models import AppUser, CloudAccount
from app.main import app
from app.services.cloud import ProviderRegistry
from app.services.cloud.models import (
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudServiceCost,
)


def _summary(
    *,
    total=Decimal("10.00"),
    previous=Decimal("8.00"),
    currency="USD",
    by_service=None,
    by_region=None,
    status="SUCCESS",
):
    today = date.today()
    return CloudCostSummary(
        provider=CloudProviderKind.AZURE,
        account_id=None,
        scope="all",
        period_start=date(today.year, today.month, 1),
        period_end=today,
        currency=currency,
        current_total=total,
        previous_total=previous,
        change_amount=total - previous,
        change_percent=Decimal("25.00"),
        estimated=False,
        by_service=by_service or [],
        by_region=by_region or [],
        extensions={
            "status": status,
            "currency_status": "OK",
            "by_subscription": [],
            "subscription_errors": [],
        },
    )


def _trend(*, daily=None):
    today = date.today()
    return CloudCostTrend(
        provider=CloudProviderKind.AZURE,
        account_id=None,
        period_start=date(today.year, today.month, 1),
        period_end=today,
        currency="USD",
        daily=daily or [],
        source="AZURE_COST_MANAGEMENT",
    )


@pytest.fixture
def auth_user(monkeypatch):
    """Replace ``get_current_user`` with a stub so we don't need JWT."""
    from types import SimpleNamespace

    user = SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")

    async def _stub():
        return user

    from app.api import deps

    monkeypatch.setattr(deps, "get_current_user", _stub)
    return user


@pytest.fixture
def azure_adapter_stub(monkeypatch):
    """Replace the registered Azure adapter with a stub."""
    registry = ProviderRegistry.current()
    adapter = registry.try_get(CloudProviderKind.AZURE)
    assert adapter is not None

    # Capture the original method so we can restore it after the
    # test.  We deliberately avoid monkeypatch.setattr on the
    # class so other tests stay isolated.
    original_summary = adapter.get_cost_summary
    original_trend = adapter.get_cost_trend

    summary_call_log: list = []
    trend_call_log: list = []

    async def fake_summary(**kwargs):
        summary_call_log.append(kwargs)
        return _summary()

    async def fake_trend(**kwargs):
        trend_call_log.append(kwargs)
        return _trend()

    adapter.get_cost_summary = fake_summary
    adapter.get_cost_trend = fake_trend

    yield {
        "adapter": adapter,
        "summary_log": summary_call_log,
        "trend_log": trend_call_log,
    }

    adapter.get_cost_summary = original_summary
    adapter.get_cost_trend = original_trend


@pytest.fixture
def cloud_rows(monkeypatch):
    """Replace the cloud DB query with two stub Azure rows.

    The route's ``_resolve_rows`` calls
    :func:`app.services.aws.account_provider.iter_enabled_accounts`.
    We stub that helper directly via the route module's binding
    so the patch is visible.
    """
    from app.api import cloud_costs as cloud_costs_module

    class _Row:
        def __init__(self, account_id, display_name):
            self.account_id = account_id
            self.display_name = display_name
            self.provider = "AZURE"
            self.enabled = True

    rows = [
        _Row("sub-aaaa-aaaa-aaaa", "MCPP Subscription"),
        _Row("sub-bbbb-bbbb-bbbb", "Microsoft Azure Sponsorship"),
    ]

    monkeypatch.setattr(
        cloud_costs_module, "iter_enabled_accounts",
        lambda db, provider="AWS": rows,
    )
    return rows


@pytest.fixture
def single_row(monkeypatch):
    """Replace iter_enabled_accounts with a single subscription."""
    from app.api import cloud_costs as cloud_costs_module

    class _Row:
        def __init__(self, account_id, display_name):
            self.account_id = account_id
            self.display_name = display_name
            self.provider = "AZURE"
            self.enabled = True

    rows = [_Row("sub-aaaa-aaaa-aaaa", "MCPP Subscription")]
    monkeypatch.setattr(
        cloud_costs_module, "iter_enabled_accounts",
        lambda db, provider="AWS": rows,
    )
    return rows


@pytest.fixture
def client():
    return TestClient(app)


class TestCloudCostsRoute:
    def test_azure_all_subscriptions(
        self, client, auth_user, azure_adapter_stub, cloud_rows
    ):
        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        # The route mounts under /cloud/costs after nginx strips
        # /api/, but in TestClient the full path is required.
        assert response.status_code == 200
        body = response.json()
        report = body["report"]
        assert report["provider"] == "AZURE"
        assert report["scope"] == "all"
        assert report["currency"] == "USD"
        assert Decimal(report["total_cost"]) == Decimal("10.00")

    def test_unknown_provider_returns_400(self, client, auth_user):
        response = client.get(
            "/cloud/costs",
            params={"provider": "NOPE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "UnknownProvider"

    def test_invalid_lookback_returns_422(self, client, auth_user):
        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "45"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "InvalidLookbackDays"

    def test_account_specific_uses_sub_scope(
        self, client, auth_user, azure_adapter_stub, single_row, monkeypatch
    ):
        # The route module already has a local binding to the
        # original function, so we patch the *route's* binding
        # rather than the source module.
        from app.api import cloud_costs

        def fake_resolve(db, selection):
            return single_row[0]

        monkeypatch.setattr(
            cloud_costs, "resolve_account_or_404", fake_resolve
        )
        response = client.get(
            "/cloud/costs",
            params={
                "provider": "AZURE",
                "days": "30",
                "account_id": "sub-aaaa-aaaa-aaaa",
            },
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 200
        body = response.json()
        report = body["report"]
        assert report["scope_kind"] == "single"
        assert report["account_id"] == "sub-aaaa-aaaa-aaaa"

    def test_month_parameter_accepted(self, client, auth_user, azure_adapter_stub, cloud_rows):
        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "month": "2026-09"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 200

    def test_cache_envelope_present(self, client, auth_user, azure_adapter_stub, cloud_rows):
        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        body = response.json()
        assert "cache_status" in body
        assert "cached_at" in body
        assert "expires_at" in body


# ---------------------------------------------------------------------------
# Error mapping — simulate an Azure upstream error.
# ---------------------------------------------------------------------------


class TestAzureErrorMapping:
    def test_auth_failed_maps_to_401(
        self, client, auth_user, azure_adapter_stub, cloud_rows, monkeypatch
    ):
        from app.services.cloud.azure.costs import (
            AzureCostError,
            AzureCostErrorCategory,
        )

        async def fake_summary(**kwargs):
            raise AzureCostError(
                category=AzureCostErrorCategory.AUTH_FAILED,
                subscription_id="sub-aaaa-aaaa-aaaa",
                status_code=401,
            )

        azure_adapter_stub["adapter"].get_cost_summary = fake_summary

        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["error_code"] == "AUTH_FAILED"
        assert body["category"] == "AUTH_FAILED"

    def test_throttled_maps_to_429_with_retry_after(
        self, client, auth_user, azure_adapter_stub, cloud_rows
    ):
        from app.services.cloud.azure.costs import (
            AzureCostError,
            AzureCostErrorCategory,
        )

        async def fake_summary(**kwargs):
            raise AzureCostError(
                category=AzureCostErrorCategory.THROTTLED,
                subscription_id="sub-aaaa-aaaa-aaaa",
                status_code=429,
                retry_after=42,
            )

        azure_adapter_stub["adapter"].get_cost_summary = fake_summary

        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "42"

    def test_upstream_error_maps_to_502(
        self, client, auth_user, azure_adapter_stub, cloud_rows
    ):
        from app.services.cloud.azure.costs import (
            AzureCostError,
            AzureCostErrorCategory,
        )

        async def fake_summary(**kwargs):
            raise AzureCostError(
                category=AzureCostErrorCategory.UPSTREAM_ERROR,
                subscription_id="sub-aaaa-aaaa-aaaa",
                status_code=503,
            )

        azure_adapter_stub["adapter"].get_cost_summary = fake_summary

        response = client.get(
            "/cloud/costs",
            params={"provider": "AZURE", "days": "30"},
            headers={"Authorization": "Bearer test-token-not-real"},
        )
        assert response.status_code == 502
        assert response.json()["error_code"] == "UPSTREAM_ERROR"
