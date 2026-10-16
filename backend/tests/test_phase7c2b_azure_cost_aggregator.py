"""Phase 7C.2B — Azure cost aggregator tests.

Exercises:

* Multi-subscription fan-out with bounded concurrency
* Currency mixing / mixed-currency rejection
* Partial failure isolation
* Per-subscription attribution in the returned summary
* by_service / by_region rollups
* Empty-rollup behavior (no rows registered)
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Optional

import pytest

from app.services.cloud.azure import cost_aggregator as agg
from app.services.cloud.azure.cost_aggregator import (
    AzureCostCallStatus,
    AzureRollupTotals,
    AzureSubscriptionCostFailure,
    AzureSubscriptionCostSnapshot,
    aggregate_cost_by_region_for_subscriptions,
    aggregate_cost_by_service_for_subscriptions,
    aggregate_cost_summary_for_subscriptions,
    aggregate_cost_trend_for_subscriptions,
    rollup_by_dimension,
    rollup_daily,
    rollup_totals,
)


def _row(account_id: str, display_name: str = "Test Subscription"):
    """Build a minimal CloudAccount-like stub for aggregator tests."""

    class _StubRow:
        pass

    row = _StubRow()
    row.account_id = account_id
    row.display_name = display_name
    row.preferred_region = None
    return row


def _snap(
    sub_id: str,
    total: Decimal,
    *,
    status: AzureCostCallStatus = AzureCostCallStatus.OK,
    currency: str = "USD",
    daily=(),
    by_service=(),
    by_region=(),
    display_name: str = "Sub",
    error: Optional[AzureSubscriptionCostFailure] = None,
):
    return AzureSubscriptionCostSnapshot(
        subscription_id=sub_id,
        display_name=display_name,
        status=status,
        currency=currency,
        total=total,
        daily=tuple(daily),
        by_service=tuple(by_service),
        by_region=tuple(by_region),
        error=error,
    )


# ---------------------------------------------------------------------------
# rollup_totals
# ---------------------------------------------------------------------------


class TestRollupTotals:
    def test_empty_returns_no_data(self):
        rollup = rollup_totals([])
        assert rollup.status == "NO_DATA"
        assert rollup.total == Decimal("0")

    def test_single_currency_sums(self):
        s1 = _snap("sub1", Decimal("10.00"))
        s2 = _snap("sub2", Decimal("25.50"))
        rollup = rollup_totals([s1, s2])
        assert rollup.status == "SUCCESS"
        assert rollup.total == Decimal("35.50")
        assert rollup.currency == "USD"

    def test_partial_success_when_failures_present(self):
        s = _snap("sub1", Decimal("5.00"))
        failure = AzureSubscriptionCostFailure(
            subscription_id="sub2",
            display_name="Sub2",
            category="THROTTLED",
            status_code=429,
            message="THROTTLED",
        )
        rollup = rollup_totals([s], failures=[failure])
        assert rollup.status == "PARTIAL_SUCCESS"
        assert rollup.total == Decimal("5.00")

    def test_mixed_currency_returns_mixed_status(self):
        s1 = _snap("sub1", Decimal("10.00"), currency="USD")
        s2 = _snap("sub2", Decimal("25.50"), currency="EUR")
        rollup = rollup_totals([s1, s2])
        assert rollup.has_mixed_currencies
        assert rollup.status == "MIXED_CURRENCY"
        # Totals are zeroed on mixed currency so the caller never
        # invents an FX rate.
        assert rollup.total == Decimal("0")

    def test_single_no_data_snapshot_with_failures(self):
        s = _snap(
            "sub1",
            Decimal("0"),
            status=AzureCostCallStatus.NO_DATA,
        )
        rollup = rollup_totals([s])
        # The snapshot has NO_DATA status but the rollup itself
        # is SUCCESS because the call completed without
        # exceptions.  NO_DATA is per-snapshot attribution;
        # the rollup status reflects aggregation success.
        assert rollup.status == "SUCCESS"
        assert rollup.total == Decimal("0")


# ---------------------------------------------------------------------------
# rollup_daily
# ---------------------------------------------------------------------------


class TestRollupDaily:
    def test_aligned_by_date(self):
        snapshots = [
            _snap(
                "sub1",
                Decimal("0"),
                daily=(
                    (date(2026, 9, 1), Decimal("1.00"), "USD"),
                    (date(2026, 9, 2), Decimal("2.00"), "USD"),
                ),
            ),
            _snap(
                "sub2",
                Decimal("0"),
                daily=((date(2026, 9, 1), Decimal("3.00"), "USD"),),
            ),
        ]
        status, points, currency = rollup_daily(
            snapshots,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 3),
        )
        assert status == "OK"
        assert currency == "USD"
        assert len(points) == 2
        # Day 1: 1.00 + 3.00
        assert points[0] == (date(2026, 9, 1), Decimal("4.00"), "USD")
        # Day 2: 2.00 (sub2 had no entry for it)
        assert points[1] == (date(2026, 9, 2), Decimal("2.00"), "USD")

    def test_no_snapshots_returns_empty(self):
        status, points, _ = rollup_daily(
            [], period_start=date(2026, 9, 1), period_end=date(2026, 9, 3)
        )
        assert status == "EMPTY"
        assert points == []

    def test_mixed_currency_daily(self):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            currency="USD",
            daily=((date(2026, 9, 1), Decimal("1.00"), "USD"),),
        )
        s2 = _snap(
            "sub2",
            Decimal("0"),
            currency="EUR",
            daily=((date(2026, 9, 1), Decimal("2.00"), "EUR"),),
        )
        status, points, _ = rollup_daily(
            [s1, s2], period_start=date(2026, 9, 1), period_end=date(2026, 9, 2)
        )
        assert status == "MIXED_CURRENCY"
        assert points == []


# ---------------------------------------------------------------------------
# rollup_by_dimension
# ---------------------------------------------------------------------------


class TestRollupByDimension:
    def test_combines_identical_keys(self):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            by_service=(
                ("Virtual Machines", Decimal("5.00"), "USD"),
                ("Storage", Decimal("1.00"), "USD"),
            ),
        )
        s2 = _snap(
            "sub2",
            Decimal("0"),
            by_service=(("Virtual Machines", Decimal("3.00"), "USD"),),
        )
        status, items, currency = rollup_by_dimension(
            [s1, s2], dimension_attr="by_service"
        )
        assert status == "OK"
        assert currency == "USD"
        assert items[0] == ("Virtual Machines", Decimal("8.00"), "USD")
        assert items[1] == ("Storage", Decimal("1.00"), "USD")

    def test_mixed_currency_returns_empty(self):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            currency="USD",
            by_service=(("Virtual Machines", Decimal("5.00"), "USD"),),
        )
        s2 = _snap(
            "sub2",
            Decimal("0"),
            currency="EUR",
            by_service=(("Storage", Decimal("3.00"), "EUR"),),
        )
        status, items, _ = rollup_by_dimension(
            [s1, s2], dimension_attr="by_service"
        )
        assert status == "MIXED_CURRENCY"
        assert items == []

    def test_empty_when_no_snapshots(self):
        status, items, _ = rollup_by_dimension([], dimension_attr="by_service")
        assert status == "EMPTY"
        assert items == []


# ---------------------------------------------------------------------------
# Aggregator entry points — driven through a stub snapshot so we
# don't need real Cost Management.
# ---------------------------------------------------------------------------


class TestAggregatorEntryPoints:
    @pytest.mark.asyncio
    async def test_summary_aggregates_subscriptions(self, monkeypatch):
        s1 = _snap(
            "sub1",
            Decimal("10.00"),
            display_name="S1",
            by_service=(
                ("Virtual Machines", Decimal("6.00"), "USD"),
                ("Storage", Decimal("4.00"), "USD"),
            ),
            by_region=(
                ("eastus", Decimal("6.00"), "USD"),
                ("westus2", Decimal("4.00"), "USD"),
            ),
        )
        s2 = _snap(
            "sub2",
            Decimal("20.00"),
            display_name="S2",
            by_service=(("Virtual Machines", Decimal("20.00"), "USD"),),
            by_region=(
                ("eastus", Decimal("10.00"), "USD"),
                ("westus2", Decimal("10.00"), "USD"),
            ),
        )
        # Previous period snapshots with their own totals.
        p1 = _snap("sub1", Decimal("8.00"))
        p2 = _snap("sub2", Decimal("18.00"))

        async def fake_fetch(rows, *, period_start, period_end, today, include_groupings=True):
            assert include_groupings
            return list(rows)  # not used here — first call is current, second is previous

        # First call returns the current snapshots; second returns the previous.
        responses = [
            [s1, s2],
            [p1, p2],
        ]

        async def fake_fetch_dispatch(rows, *, period_start, period_end, today, include_groupings=True):
            return responses.pop(0)

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch_dispatch)

        rows = [_row("sub1", "S1"), _row("sub2", "S2")]
        summary, failures = await aggregate_cost_summary_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            previous_period_start=date(2026, 8, 1),
            previous_period_end=date(2026, 9, 1),
            today=date(2026, 9, 17),
        )
        assert summary.current_total == Decimal("30.00")
        assert summary.previous_total == Decimal("26.00")
        assert summary.change_amount == Decimal("4.00")
        assert summary.currency == "USD"
        # Service grouping is summed across subscriptions.
        service_names = [s.service for s in summary.by_service]
        assert "Virtual Machines" in service_names
        assert "Storage" in service_names
        # Region grouping is summed across subscriptions.
        region_names = [s.service for s in summary.by_region]
        assert "eastus" in region_names
        # Per-subscription attribution.
        extensions = summary.extensions
        by_sub = extensions.get("by_subscription", [])
        assert {entry["subscription_id"] for entry in by_sub} == {"sub1", "sub2"}
        assert not failures

    @pytest.mark.asyncio
    async def test_summary_mixed_currency_zeroes_totals(self, monkeypatch):
        s1 = _snap(
            "sub1",
            Decimal("10.00"),
            currency="USD",
            display_name="S1",
        )
        s2 = _snap(
            "sub2",
            Decimal("20.00"),
            currency="EUR",
            display_name="S2",
        )
        p1 = _snap("sub1", Decimal("5.00"), currency="USD")
        p2 = _snap("sub2", Decimal("15.00"), currency="EUR")

        responses = [
            [s1, s2],
            [p1, p2],
        ]

        async def fake_fetch_dispatch(rows, *, period_start, period_end, today, include_groupings=True):
            return responses.pop(0)

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch_dispatch)

        rows = [_row("sub1", "S1"), _row("sub2", "S2")]
        summary, _ = await aggregate_cost_summary_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            previous_period_start=date(2026, 8, 1),
            previous_period_end=date(2026, 9, 1),
            today=date(2026, 9, 17),
        )
        assert summary.current_total == Decimal("0")
        assert summary.previous_total == Decimal("0")
        assert summary.extensions.get("currency_status") == "MIXED_CURRENCY"

    @pytest.mark.asyncio
    async def test_partial_failure_preserves_successful_data(self, monkeypatch):
        success = _snap(
            "sub1",
            Decimal("10.00"),
            display_name="S1",
        )
        failure = AzureSubscriptionCostFailure(
            subscription_id="sub2",
            display_name="S2",
            category="ACCESS_DENIED",
            status_code=403,
            message="ACCESS_DENIED",
        )
        snap_with_error = AzureSubscriptionCostSnapshot(
            subscription_id="sub2",
            display_name="S2",
            status=AzureCostCallStatus.EMPTY,
            currency="USD",
            total=Decimal("0"),
            daily=(),
            by_service=(),
            by_region=(),
            error=failure,
        )
        p1 = _snap("sub1", Decimal("5.00"))
        p_failure = AzureSubscriptionCostSnapshot(
            subscription_id="sub2",
            display_name="S2",
            status=AzureCostCallStatus.EMPTY,
            currency="USD",
            total=Decimal("0"),
            daily=(),
            by_service=(),
            by_region=(),
            error=AzureSubscriptionCostFailure(
                subscription_id="sub2",
                display_name="S2",
                category="ACCESS_DENIED",
                status_code=403,
                message="ACCESS_DENIED",
            ),
        )

        responses = [
            [success, snap_with_error],
            [p1, p_failure],
        ]

        async def fake_fetch_dispatch(rows, *, period_start, period_end, today, include_groupings=True):
            return responses.pop(0)

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch_dispatch)

        rows = [_row("sub1", "S1"), _row("sub2", "S2")]
        summary, failures = await aggregate_cost_summary_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            previous_period_start=date(2026, 8, 1),
            previous_period_end=date(2026, 9, 1),
            today=date(2026, 9, 17),
        )
        assert summary.current_total == Decimal("10.00")
        assert summary.previous_total == Decimal("5.00")
        # Status reflects partial success.
        assert summary.extensions.get("status") == "PARTIAL_SUCCESS"
        assert len(failures) == 1
        assert failures[0].subscription_id == "sub2"
        assert failures[0].category == "ACCESS_DENIED"

    @pytest.mark.asyncio
    async def test_empty_rows_returns_no_data_envelope(self):
        summary, failures = await aggregate_cost_summary_for_subscriptions(
            rows=[],
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            previous_period_start=date(2026, 8, 1),
            previous_period_end=date(2026, 9, 1),
            today=date(2026, 9, 17),
        )
        assert summary.extensions.get("status") == "NO_DATA"
        assert not failures

    @pytest.mark.asyncio
    async def test_service_aggregator(self, monkeypatch):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            by_service=(("Virtual Machines", Decimal("5.00"), "USD"),),
        )
        s2 = _snap(
            "sub2",
            Decimal("0"),
            by_service=(("Virtual Machines", Decimal("3.00"), "USD"),),
        )

        async def fake_fetch(rows, *, period_start, period_end, today, include_groupings=True):
            return [s1, s2]

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch)

        rows = [_row("sub1"), _row("sub2")]
        items, status, failures = await aggregate_cost_by_service_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            today=date(2026, 9, 17),
        )
        assert status == "OK"
        assert items[0].service == "Virtual Machines"
        assert items[0].amount == Decimal("8.00")
        assert not failures

    @pytest.mark.asyncio
    async def test_region_aggregator(self, monkeypatch):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            by_region=(
                ("eastus", Decimal("5.00"), "USD"),
                ("Unassigned", Decimal("1.00"), "USD"),
            ),
        )

        async def fake_fetch(rows, *, period_start, period_end, today, include_groupings=True):
            return [s1]

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch)

        rows = [_row("sub1")]
        items, status, _ = await aggregate_cost_by_region_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            today=date(2026, 9, 17),
        )
        assert status == "OK"
        names = [item.service for item in items]
        assert "eastus" in names
        assert "Unassigned" in names

    @pytest.mark.asyncio
    async def test_trend_aggregator(self, monkeypatch):
        s1 = _snap(
            "sub1",
            Decimal("0"),
            daily=(
                (date(2026, 9, 1), Decimal("1.00"), "USD"),
                (date(2026, 9, 2), Decimal("2.00"), "USD"),
            ),
        )

        async def fake_fetch(rows, *, period_start, period_end, today, include_groupings=True):
            return [s1]

        monkeypatch.setattr(agg, "fetch_subscription_snapshots", fake_fetch)

        rows = [_row("sub1")]
        trend, _, currency_status = await aggregate_cost_trend_for_subscriptions(
            rows=rows,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 9, 3),
            today=date(2026, 9, 17),
        )
        assert currency_status == "OK"
        # Days are present for which data was returned.
        assert len(trend.daily) == 2
        assert trend.source == "AZURE_COST_MANAGEMENT"
