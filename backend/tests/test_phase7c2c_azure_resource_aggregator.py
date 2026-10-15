"""Phase 7C.2C — Azure resource aggregator tests.

Covers:

* Single-subscription fan-out.
* Multi-subscription (tenant) query.
* Per-subscription attribution.
* Failure isolation.
* Aggregations by type / location / resource group / subscription.
* Zero-resource subscription is SUCCESS not NO_DATA.
* NO_DATA when every subscription is empty AND there are no failures.
* Mixed empty / populated — preserves attribution.
* Tags normalisation.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

from app.services.cloud.azure import resource_aggregator as agg
from app.services.cloud.azure.resource_aggregator import (
    AzureResourceGraphSubscriptionFailure,
    AzureSubscriptionResourceSnapshot,
    aggregate_resources_for_subscriptions,
    build_resource_summary,
    normalize_row,
)
from app.services.cloud.models import CloudProviderKind, CloudResource


def _row(account_id: str, display_name: str):
    class _StubRow:
        pass

    row = _StubRow()
    row.account_id = account_id
    row.display_name = display_name
    row.provider = "AZURE"
    row.enabled = True
    return row


def _resource(
    resource_id: str,
    *,
    resource_type: str = "microsoft.compute/virtualmachines",
    location: Optional[str] = "eastus",
    name: Optional[str] = None,
    resource_group: Optional[str] = "rg1",
    subscription_id: str = "sub-aaaa",
    tags: Optional[Dict[str, Any]] = None,
) -> CloudResource:
    return CloudResource(
        provider=CloudProviderKind.AZURE,
        account_id=subscription_id,
        region=location,
        resource_type=resource_type,
        resource_id=resource_id,
        service=resource_type,
        name=name or resource_id.rsplit("/", 1)[-1],
        state=None,
        composite_id=f"AZURE|{resource_id}",
        metadata={
            "resource_group": resource_group,
            **({"tags": dict(tags)} if tags else {}),
        },
    )


# ---------------------------------------------------------------------------
# normalize_row
# ---------------------------------------------------------------------------


class TestNormalizeRow:
    def test_basic_normalisation(self):
        rg_row = {
            "id": "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
            "name": "vm1",
            "type": "microsoft.compute/virtualmachines",
            "location": "eastus",
            "resourceGroup": "rg1",
            "subscriptionId": "sub-aaaa",
            "tags": {"env": "prod", "owner": "alice"},
        }
        r = normalize_row(rg_row)
        assert r.provider == CloudProviderKind.AZURE
        assert r.resource_id == rg_row["id"]
        assert r.resource_type == "microsoft.compute/virtualmachines"
        assert r.name == "vm1"
        assert r.region == "eastus"
        assert r.account_id == "sub-aaaa"
        assert r.metadata["resource_group"] == "rg1"
        assert r.metadata["tags"]["env"] == "prod"
        assert r.metadata["tags"]["owner"] == "alice"

    def test_missing_fields_handled(self):
        rg_row = {
            "id": "/r/1",
            "name": "r1",
            "type": "x",
        }
        r = normalize_row(rg_row)
        assert r.region is None
        assert r.account_id == ""
        assert r.metadata == {}

    def test_non_string_tags_coerced(self):
        rg_row = {
            "id": "/r/1",
            "name": "r1",
            "type": "x",
            "tags": {"cost_center": 42, "is_production": True},
        }
        r = normalize_row(rg_row)
        assert r.metadata["tags"]["cost_center"] == "42"
        assert r.metadata["tags"]["is_production"] == "True"

    def test_composite_id_is_provider_prefixed(self):
        r = normalize_row({"id": "/abc", "name": "a", "type": "t"})
        assert r.composite_id == "AZURE|/abc"


# ---------------------------------------------------------------------------
# build_resource_summary
# ---------------------------------------------------------------------------


class TestBuildResourceSummary:
    def test_zero_resources_returns_no_data_when_everything_empty(self):
        # Two snapshots, both empty, no failures -> NO_DATA per
        # the "genuinely empty union" rule.
        s1 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="NO_DATA", resources=()
        )
        s2 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub2", display_name="S2", status="NO_DATA", resources=()
        )
        summary = build_resource_summary([s1, s2], rows=[_row("sub1", "S1"), _row("sub2", "S2")])
        assert summary.extensions["status"] == "NO_DATA"
        assert summary.extensions["total_resources"] == 0

    def test_zero_resources_with_failures_returns_partial(self):
        # When one subscription failed, the status is PARTIAL_SUCCESS.
        s1 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="SUCCESS", resources=()
        )
        failure = AzureResourceGraphSubscriptionFailure(
            subscription_id="sub2", display_name="S2",
            category="ACCESS_DENIED", status_code=403, message="ACCESS_DENIED"
        )
        s2 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub2", display_name="S2", status="ERROR", resources=(),
            error=failure,
        )
        summary = build_resource_summary([s1, s2], rows=[_row("sub1", "S1"), _row("sub2", "S2")])
        assert summary.extensions["status"] == "PARTIAL_SUCCESS"
        # Single-subscription-with-no-data is NOT an error.
        assert summary.extensions["total_resources"] == 0

    def test_aggregations_correct(self):
        s1 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1",
            display_name="MCPP",
            status="SUCCESS",
            resources=(
                _resource("/sub1/r/1", resource_type="microsoft.compute/virtualmachines", location="eastus", resource_group="rg1"),
                _resource("/sub1/r/2", resource_type="microsoft.compute/virtualmachines", location="eastus", resource_group="rg1"),
                _resource("/sub1/r/3", resource_type="microsoft.storage/storageaccounts", location="eastus", resource_group="rg2"),
            ),
        )
        s2 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub2",
            display_name="Sponsorship",
            status="SUCCESS",
            resources=(
                _resource("/sub2/r/1", resource_type="microsoft.compute/virtualmachines", location="westus2", resource_group="rg3", subscription_id="sub2"),
            ),
        )
        summary = build_resource_summary([s1, s2], rows=[_row("sub1", "MCPP"), _row("sub2", "Sponsorship")])
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.extensions["total_resources"] == 4
        # by_type
        by_type = {item["key"]: item["count"] for item in summary.extensions["by_type"]}
        assert by_type["microsoft.compute/virtualmachines"] == 3
        assert by_type["microsoft.storage/storageaccounts"] == 1
        # by_location
        by_loc = {item["key"]: item["count"] for item in summary.extensions["by_location"]}
        assert by_loc["eastus"] == 3
        assert by_loc["westus2"] == 1
        # by_resource_group
        by_rg = {item["key"]: item["count"] for item in summary.extensions["by_resource_group"]}
        assert by_rg["rg1"] == 2
        assert by_rg["rg2"] == 1
        assert by_rg["rg3"] == 1
        # by_subscription
        by_sub = {item["subscription_id"]: item["count"] for item in summary.extensions["by_subscription"]}
        assert by_sub["sub1"] == 3
        assert by_sub["sub2"] == 1

    def test_provider_neutral_no_azure_metadata_leaked(self):
        s = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1",
            display_name="S1",
            status="SUCCESS",
            resources=(_resource("/sub1/r/1"),),
        )
        summary = build_resource_summary([s], rows=[_row("sub1", "S1")])
        # The summary dataclass itself never carries raw Azure
        # properties or tags blob.
        import json
        dump = json.loads(json.dumps(summary.extensions, default=str))
        for forbidden in ("properties", "sku", "identity", "plan"):
            assert forbidden not in dump

    def test_single_subscription_scope_uses_sub_prefix(self):
        s = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="SUCCESS",
            resources=(_resource("/r/1"),),
        )
        summary = build_resource_summary([s], rows=[_row("sub1", "S1")])
        assert summary.scope == "sub:sub1"
        assert summary.account_id == "sub1"

    def test_multi_subscription_scope_is_all(self):
        s1 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="SUCCESS",
            resources=(_resource("/r/1", subscription_id="sub1"),),
        )
        s2 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub2", display_name="S2", status="SUCCESS",
            resources=(_resource("/r/2", subscription_id="sub2"),),
        )
        summary = build_resource_summary(
            [s1, s2], rows=[_row("sub1", "S1"), _row("sub2", "S2")]
        )
        assert summary.scope == "all"
        assert summary.account_id is None


# ---------------------------------------------------------------------------
# Aggregator dispatch (with stubbed fetch)
# ---------------------------------------------------------------------------


class TestAggregatorDispatch:
    @pytest.mark.asyncio
    async def test_single_subscription(self, monkeypatch):
        snap = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="MCPP", status="SUCCESS",
            resources=(_resource("/r/1", subscription_id="sub1"), _resource("/r/2", subscription_id="sub1")),
        )
        async def fake_fetch(rows, **_kwargs):
            return [snap]
        monkeypatch.setattr(agg, "fetch_subscription_resource_snapshots", fake_fetch)
        rows = [_row("sub1", "MCPP")]
        summary, flat, failures = await aggregate_resources_for_subscriptions(rows=rows)
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.extensions["total_resources"] == 2
        assert len(flat) == 2
        assert not failures

    @pytest.mark.asyncio
    async def test_multi_subscription_uses_tenant_query(self, monkeypatch):
        # Confirm the multi-subscription path delegates to the
        # tenant-mode fetcher, not the per-row fetcher.
        async def fail_per_row(*_args, **_kwargs):
            raise AssertionError("per-row fetch should not be called for multi")
        async def fake_tenant(rows, **_kwargs):
            return [
                AzureSubscriptionResourceSnapshot(
                    subscription_id="sub1", display_name="S1", status="SUCCESS",
                    resources=(_resource("/sub1/r/1", subscription_id="sub1"),),
                ),
                AzureSubscriptionResourceSnapshot(
                    subscription_id="sub2", display_name="S2", status="SUCCESS",
                    resources=(_resource("/sub2/r/1", subscription_id="sub2"),),
                ),
            ]
        monkeypatch.setattr(agg, "fetch_subscription_resource_snapshots", fail_per_row)
        monkeypatch.setattr(agg, "_fetch_tenant_resources", fake_tenant)
        rows = [_row("sub1", "S1"), _row("sub2", "S2")]
        summary, flat, _ = await aggregate_resources_for_subscriptions(rows=rows)
        assert summary.extensions["status"] == "SUCCESS"
        assert summary.extensions["total_resources"] == 2
        assert len(flat) == 2

    @pytest.mark.asyncio
    async def test_empty_rows_returns_no_data_envelope(self):
        summary, flat, failures = await aggregate_resources_for_subscriptions(rows=[])
        assert summary.extensions["status"] == "NO_DATA"
        assert flat == []
        assert failures == []

    @pytest.mark.asyncio
    async def test_partial_failure_preserves_successful_data(self, monkeypatch):
        success = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="MCPP", status="SUCCESS",
            resources=(_resource("/sub1/r/1", subscription_id="sub1"),),
        )
        failure = AzureResourceGraphSubscriptionFailure(
            subscription_id="sub2", display_name="Sponsorship",
            category="THROTTLED", status_code=429, message="THROTTLED"
        )
        snap_with_error = AzureSubscriptionResourceSnapshot(
            subscription_id="sub2", display_name="Sponsorship", status="ERROR",
            resources=(), error=failure,
        )
        async def fake_tenant(rows, **_kwargs):
            return [success, snap_with_error]
        monkeypatch.setattr(agg, "_fetch_tenant_resources", fake_tenant)
        rows = [_row("sub1", "MCPP"), _row("sub2", "Sponsorship")]
        summary, flat, failures = await aggregate_resources_for_subscriptions(rows=rows)
        assert summary.extensions["status"] == "PARTIAL_SUCCESS"
        assert len(flat) == 1
        assert flat[0].account_id == "sub1"
        assert len(failures) == 1
        assert failures[0].category == "THROTTLED"

    @pytest.mark.asyncio
    async def test_no_duplicate_resources_in_flat_list(self, monkeypatch):
        # Two snapshots with overlapping resource ids must NOT
        # produce duplicates in the flat list.
        r = _resource("/sub1/r/1", subscription_id="sub1")
        s1 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="SUCCESS", resources=(r,)
        )
        s2 = AzureSubscriptionResourceSnapshot(
            subscription_id="sub1", display_name="S1", status="SUCCESS", resources=(r,)
        )
        async def fake_fetch(rows, **_kwargs):
            return [s1, s2]
        monkeypatch.setattr(agg, "fetch_subscription_resource_snapshots", fake_fetch)
        rows = [_row("sub1", "S1")]
        _summary, flat, _ = await aggregate_resources_for_subscriptions(rows=rows)
        ids = [r.composite_id for r in flat]
        assert len(ids) == 1
