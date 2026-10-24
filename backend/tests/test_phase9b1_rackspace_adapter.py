"""Phase 9B.1 — Rackspace adapter, registry, and bootstrap tests.

Covers :mod:`app.services.cloud.rackspace.adapter`:

* registry registration (``require(RACKSPACE)`` resolves)
* capability reporting (configured vs not-configured)
* account identity model (tenant id only — the RAN never appears)
* canonical cost summary (actual source period, true-zero success,
  previous-period unavailability, unsupported dimensions omitted)
* period enforcement (PERIOD_UNSUPPORTED propagates)
* read-only surface (resources / recommendations not implemented)
* account bootstrap gating (no rows without configuration; the row
  never carries the RAN or any credential)
* secret redaction (no API key / token / RAN in logs — the log
  stream is captured and scanned)

All Rackspace HTTP is stubbed; no real credentials are used.
"""

from __future__ import annotations

import io
import logging
from datetime import date
from decimal import Decimal
from typing import Any, List, Optional

import pytest

from app.services.cloud.models import CloudProviderKind
from app.services.cloud.registry import ProviderRegistry
from app.services.cloud.rackspace import adapter as rackspace_adapter_module
from app.services.cloud.rackspace.adapter import (
    RackspaceCloudProvider,
    _bootstrap_rackspace_account,
)
from app.services.cloud.rackspace.costs import (
    RackspaceCostError,
    RackspaceCostErrorCategory,
    RackspaceCostSnapshot,
    RackspaceServiceCost,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snapshot(
    *,
    total: str = "1250.50",
    period_start: date = date(2026, 10, 1),
    period_end: date = date(2026, 10, 31),
    currency: str = "USD",
) -> RackspaceCostSnapshot:
    return RackspaceCostSnapshot(
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        total=Decimal(total),
        by_service=(
            RackspaceServiceCost("Cloud Servers", Decimal("1000.00")),
            RackspaceServiceCost("Cloud Files", Decimal(total).quantize(Decimal("0.01")) - Decimal("1000.00")),
        ) if Decimal(total) != 0 else (),
        estimated=True,
        record_count=2,
        body_total=None,
        warnings=("uncategorized_charge_records=0",),
        extensions={"source": "RACKSPACE_BILLING"},
    )


class _Row:
    """Stub registry row."""

    def __init__(self, account_id: str = "8675309", display_name: str = "Rackspace Cloud") -> None:
        self.account_id = account_id
        self.display_name = display_name
        self.provider = "RACKSPACE"
        self.enabled = True


def _configured(monkeypatch, *, billing: bool = True) -> None:
    """Force the adapter's configuration probes."""

    monkeypatch.setattr(
        rackspace_adapter_module, "get_config_status",
        lambda: type("S", (), {"configured": True, "missing": []})(),
    )
    monkeypatch.setattr(
        rackspace_adapter_module, "billing_account_configured", lambda: billing
    )


# ---------------------------------------------------------------------------
# Registry + capabilities
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_rackspace_adapter_registered(self):
        registry = ProviderRegistry.current()
        assert registry.is_registered(CloudProviderKind.RACKSPACE)

    def test_require_resolves_adapter(self):
        registry = ProviderRegistry.current()
        adapter = registry.require(CloudProviderKind.RACKSPACE)
        assert isinstance(adapter, RackspaceCloudProvider)
        assert adapter.provider_kind == CloudProviderKind.RACKSPACE
        assert adapter.provider_name == "RACKSPACE"


class TestCapabilities:
    def test_not_configured_when_env_missing(self):
        adapter = RackspaceCloudProvider()
        status = adapter.get_capabilities()
        assert not status.has(__import__(
            "app.services.cloud.base", fromlist=["CloudCapability"]
        ).CloudCapability.COSTS)
        caps = dict(status.capabilities)
        from app.services.cloud.base import CloudCapability, CloudCapabilityState

        assert caps[CloudCapability.COSTS] == CloudCapabilityState.NOT_CONFIGURED
        assert caps[CloudCapability.AUTHENTICATION] == CloudCapabilityState.NOT_CONFIGURED
        assert caps[CloudCapability.RESOURCES] == CloudCapabilityState.NOT_CONFIGURED
        assert caps[CloudCapability.OPTIMIZATION] == CloudCapabilityState.UNSUPPORTED

    def test_configured_reports_costs_supported(self, monkeypatch):
        _configured(monkeypatch)
        adapter = RackspaceCloudProvider()
        status = adapter.get_capabilities()
        from app.services.cloud.base import CloudCapability, CloudCapabilityState

        assert status.has(CloudCapability.COSTS)
        assert status.has(CloudCapability.BILLING)
        assert status.has(CloudCapability.AUTHENTICATION)
        # Phase 9B.2 — resources are now a supported capability when
        # the Identity credentials are configured.
        assert caps_state(status, CloudCapability.RESOURCES) == (
            CloudCapabilityState.SUPPORTED
        )
        assert caps_state(status, CloudCapability.OPTIMIZATION) == (
            CloudCapabilityState.UNSUPPORTED
        )

    def test_regions_listed(self):
        adapter = RackspaceCloudProvider()
        regions = adapter.list_regions()
        assert regions
        assert all(r.provider == CloudProviderKind.RACKSPACE for r in regions)


def caps_state(status, capability):
    from app.services.cloud.base import CloudCapabilityState

    return status.capabilities.get(capability, CloudCapabilityState.UNSUPPORTED)


# ---------------------------------------------------------------------------
# Account identity model
# ---------------------------------------------------------------------------


class TestAccountIdentity:
    def test_provider_account_carries_tenant_not_ran(self):
        adapter = RackspaceCloudProvider()
        account = adapter.get_provider_account(_Row())  # type: ignore[arg-type]
        assert account.provider == CloudProviderKind.RACKSPACE
        assert account.account_id == "8675309"
        # The RAN is protected configuration — never on the account
        # surface (``external_ref`` stays None).
        assert account.external_ref is None
        assert "RACKSPACE_API_KEY" in account.metadata["credential_strategy"]
        assert "ran" not in str(account.metadata).lower()


# ---------------------------------------------------------------------------
# Canonical cost summary
# ---------------------------------------------------------------------------


class TestCostSummary:
    @pytest.fixture
    def adapter(self, monkeypatch):
        a = RackspaceCloudProvider()
        snap = _snapshot()

        async def fake_fetch(client):
            return snap

        monkeypatch.setattr(rackspace_adapter_module, "fetch_cost_snapshot", fake_fetch)
        return a

    @pytest.mark.asyncio
    async def test_summary_shape(self, adapter):
        summary = await adapter.get_cost_summary(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            previous_period_start=date(2026, 9, 1),
            previous_period_end=date(2026, 10, 1),
        )
        assert summary.provider == CloudProviderKind.RACKSPACE
        # The ACTUAL source period — not the requested window.
        assert summary.period_start == date(2026, 10, 1)
        assert summary.period_end == date(2026, 10, 31)
        assert summary.currency == "USD"
        assert summary.current_total == Decimal("1250.50")
        # Previous period is deliberately unavailable (never fabricated).
        assert summary.previous_total is None
        assert summary.change_amount is None
        assert summary.change_percent is None
        assert summary.estimated is True
        # Unsupported dimensions stay EMPTY — never derived from totals.
        assert summary.by_region == []
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.extensions["previous_period_unavailable"] is True
        assert "daily_trend" in summary.extensions["unsupported_dimensions"]
        assert "region_breakdown" in summary.extensions["unsupported_dimensions"]
        assert "previous_period" in summary.extensions["unsupported_dimensions"]
        # Account attribution uses the safe tenant identity.
        by_account = summary.extensions["by_account"]
        assert by_account[0]["account_id"] == "8675309"
        assert by_account[0]["amount"] == "1250.50"

    @pytest.mark.asyncio
    async def test_true_zero_is_success(self, monkeypatch):
        adapter = RackspaceCloudProvider()
        zero = RackspaceCostSnapshot(
            currency="USD",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            total=Decimal("0.00"),
            by_service=(),
            estimated=True,
            record_count=0,
            body_total=Decimal("0.00"),
            warnings=(),
            extensions={},
        )

        async def fake_fetch(client):
            return zero

        monkeypatch.setattr(rackspace_adapter_module, "fetch_cost_snapshot", fake_fetch)
        summary = await adapter.get_cost_summary(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            previous_period_start=date(2026, 9, 1),
            previous_period_end=date(2026, 10, 1),
        )
        assert summary.current_total == Decimal("0.00")
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.by_service == []

    @pytest.mark.asyncio
    async def test_non_overlapping_period_raises(self, adapter):
        with pytest.raises(RackspaceCostError) as excinfo:
            await adapter.get_cost_summary(
                accounts=[_Row()],  # type: ignore[arg-type]
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                previous_period_start=date(2025, 12, 1),
                previous_period_end=date(2026, 1, 1),
            )
        assert excinfo.value.category == RackspaceCostErrorCategory.PERIOD_UNSUPPORTED

    @pytest.mark.asyncio
    async def test_partial_overlap_served_with_source_period(self, adapter):
        summary = await adapter.get_cost_summary(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 9, 20),
            period_end=date(2026, 10, 20),
            previous_period_start=date(2026, 8, 20),
            previous_period_end=date(2026, 9, 20),
        )
        assert summary.period_start == date(2026, 10, 1)
        assert summary.period_end == date(2026, 10, 31)
        assert summary.extensions["period_adjusted"] is True

    @pytest.mark.asyncio
    async def test_empty_accounts_raise_not_configured(self):
        adapter = RackspaceCloudProvider()
        with pytest.raises(RackspaceCostError) as excinfo:
            await adapter.get_cost_summary(
                accounts=[],
                period_start=date(2026, 10, 1),
                period_end=date(2026, 10, 31),
                previous_period_start=date(2026, 9, 1),
                previous_period_end=date(2026, 10, 1),
            )
        assert excinfo.value.category == RackspaceCostErrorCategory.NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_trend_is_empty(self, adapter):
        trend = await adapter.get_cost_trend(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
        )
        assert trend.daily == []
        assert trend.provider == CloudProviderKind.RACKSPACE

    @pytest.mark.asyncio
    async def test_cost_by_service(self, adapter):
        items = await adapter.get_cost_by_service(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
        )
        services = {i.service: i.amount for i in items}
        assert services["Cloud Servers"] == Decimal("1000.00")

    @pytest.mark.asyncio
    async def test_breakdown_reconciles_with_total(self, adapter):
        summary = await adapter.get_cost_summary(
            accounts=[_Row()],  # type: ignore[arg-type]
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            previous_period_start=date(2026, 9, 1),
            previous_period_end=date(2026, 10, 1),
        )
        breakdown = sum((s.amount for s in summary.by_service), Decimal("0"))
        assert breakdown == summary.current_total


# ---------------------------------------------------------------------------
# Read-only surface
# ---------------------------------------------------------------------------


class TestReadOnlySurface:
    @pytest.mark.asyncio
    async def test_resources_delegate_to_canonical_aggregation(self, monkeypatch):
        # Phase 9B.2 — get_resources now delegates to the canonical
        # aggregation instead of raising NotImplementedError.
        from app.services.cloud.models import CloudResource

        resource = CloudResource(
            provider=CloudProviderKind.RACKSPACE,
            account_id="8675309",
            region="DFW",
            resource_type="SERVER",
            resource_id="srv-1",
            service="Cloud Servers",
            name="web-1",
            state="ACTIVE",
            composite_id="RACKSPACE|8675309|DFW|SERVER|srv-1",
            metadata={},
        )

        async def fake_aggregate(*, rows, inventory_client=None):
            from app.services.cloud.models import CloudResourceSummary

            summary = CloudResourceSummary(
                provider=CloudProviderKind.RACKSPACE,
                account_id="8675309",
                scope="acct:8675309",
                region=None,
                services={},
                totals_by_account=[],
                extensions={"status": "SUCCESS", "total_resources": 1},
            )
            return summary, [resource], []

        monkeypatch.setattr(
            rackspace_adapter_module, "aggregate_rackspace_resources", fake_aggregate
        )
        adapter = RackspaceCloudProvider()
        result = await adapter.get_resources(accounts=[_Row()])  # type: ignore[arg-type]
        assert result == [resource]
        summary = await adapter.get_resource_summary(accounts=[_Row()])  # type: ignore[arg-type]
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.extensions["total_resources"] == 1

    @pytest.mark.asyncio
    async def test_recommendations_not_implemented(self):
        adapter = RackspaceCloudProvider()
        with pytest.raises(NotImplementedError):
            await adapter.get_recommendations(accounts=[_Row()])  # type: ignore[arg-type]

    def test_transport_exposes_no_product_write_verbs(self):
        from app.services.rackspace.client import RackspaceHttpClient

        for verb in ("put", "patch", "delete", "put_json", "delete_json"):
            assert not hasattr(RackspaceHttpClient, verb), (
                f"RackspaceHttpClient must not expose {verb!r}"
            )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


class _FakeQueryResult:
    def __init__(self, row: Optional[Any] = None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Optional[Any]:
        return self._row


class _FakeDb:
    """Minimal DB stub: records inserts, returns a canned lookup row."""

    def __init__(self, existing: Optional[Any] = None) -> None:
        self.existing = existing
        self.added: List[Any] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, *_args, **_kwargs) -> _FakeQueryResult:
        return _FakeQueryResult(self.existing)

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class TestBootstrap:
    def test_skips_when_not_configured(self, monkeypatch):
        monkeypatch.setattr(
            rackspace_adapter_module, "get_config_status",
            lambda: type("S", (), {"configured": False, "missing": ["RACKSPACE_USERNAME"]})(),
        )
        db = _FakeDb()
        assert _bootstrap_rackspace_account(db) == 0
        assert db.added == []

    def test_skips_when_ran_missing(self, monkeypatch):
        _configured(monkeypatch, billing=False)
        db = _FakeDb()
        assert _bootstrap_rackspace_account(db) == 0
        assert db.added == []

    def test_inserts_row_when_configured(self, monkeypatch):
        _configured(monkeypatch, billing=True)
        monkeypatch.setattr(
            rackspace_adapter_module, "get_config_status_and_account_id",
            lambda: "8675309",
        )
        monkeypatch.setenv("RACKSPACE_ACCOUNT_DISPLAY_NAME", "My Rackspace Tenant")
        db = _FakeDb()
        assert _bootstrap_rackspace_account(db) == 1
        assert len(db.added) == 1
        row = db.added[0]
        assert row.provider == "RACKSPACE"
        assert row.account_id == "8675309"
        assert row.display_name == "My Rackspace Tenant"
        assert row.enabled is True
        assert row.is_management is False
        # No RAN, no credentials in the persisted metadata.
        metadata = row.provider_metadata
        assert metadata["bootstrap_source"] == "phase9b1"
        assert metadata["credential_strategy"] == "RACKSPACE_API_KEY"
        assert "RACKSPACE_BILLING_ACCOUNT" not in metadata
        assert "ran" not in str(metadata).lower()

    def test_idempotent_when_row_exists(self, monkeypatch):
        _configured(monkeypatch, billing=True)
        monkeypatch.setattr(
            rackspace_adapter_module, "get_config_status_and_account_id",
            lambda: "8675309",
        )
        db = _FakeDb(existing=_Row())
        assert _bootstrap_rackspace_account(db) == 0
        assert db.added == []

    def test_uses_safe_default_display_name(self, monkeypatch):
        import os

        _configured(monkeypatch, billing=True)
        monkeypatch.delenv("RACKSPACE_ACCOUNT_DISPLAY_NAME", raising=False)
        monkeypatch.setattr(
            rackspace_adapter_module, "get_config_status_and_account_id",
            lambda: "8675309",
        )
        db = _FakeDb()
        _bootstrap_rackspace_account(db)
        assert db.added[0].display_name == "Rackspace Cloud"


# ---------------------------------------------------------------------------
# Secret redaction — the adapter log stream must stay clean
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_no_secrets_in_adapter_logs(self, monkeypatch, caplog):
        _configured(monkeypatch, billing=False)
        adapter = RackspaceCloudProvider()
        with caplog.at_level(logging.INFO, logger="cost-detective.cloud.rackspace"):
            adapter.get_capabilities()
        stream = "\n".join(r.getMessage() for r in caplog.records)
        for secret in (
            "test-api-key",
            "RACKSPACE_API_KEY=",
            "X-Auth-Token:",
            "020-123456",  # a RAN-shaped value
        ):
            assert secret not in stream

    def test_bootstrap_log_carries_no_account_identifier(self, monkeypatch, caplog):
        _configured(monkeypatch, billing=True)
        monkeypatch.setattr(
            rackspace_adapter_module, "get_config_status_and_account_id",
            lambda: "8675309",
        )
        monkeypatch.setenv("RACKSPACE_ACCOUNT_DISPLAY_NAME", "Display Only")
        db = _FakeDb()
        with caplog.at_level(logging.INFO, logger="cost-detective.cloud.rackspace"):
            _bootstrap_rackspace_account(db)
        stream = "\n".join(r.getMessage() for r in caplog.records)
        assert "8675309" not in stream
        assert "Display Only" in stream  # display label is safe
