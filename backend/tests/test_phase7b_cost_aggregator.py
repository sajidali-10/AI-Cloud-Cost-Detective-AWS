"""Phase 7B — multi-account Cost Explorer aggregator tests.

These tests verify the deterministic math (no averaging), the
sanitized per-account error envelope, and the partial-success
behaviour.  AWS calls are mocked at the Cost Explorer client layer
so we do not need a live AWS account.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from app.db.models import CloudAccount
from app.services.aws.cost_aggregator import aggregate_cost_report_for_context
from app.services.aws.cost_explorer import CostPeriod


# ---------------------------------------------------------------------------
# Fakes — bypass the Cost Explorer client entirely.
#
# We patch the four retrievers that the aggregator calls:
#   - get_total_and_previous  → returns (current, previous) directly
#   - get_daily_trend         → returns [(day, amount, unit), ...]
#   - get_by_service          → returns [(name, amount, unit), ...]
#   - get_by_region           → returns [(name, amount, unit), ...]
#
# This makes the tests completely independent of the Boto3 Cost
# Explorer client shape.
# ---------------------------------------------------------------------------


def _row(account_id: str, display_name: str = "X") -> CloudAccount:
    return CloudAccount(
        provider="AWS",
        account_id=account_id,
        display_name=display_name,
        role_arn=f"arn:aws:iam::{account_id}:role/CostDetectiveReadOnly",
        external_id=None,
        enabled=True,
        is_management=False,
        preferred_region="us-east-1",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _period(start: date, end: date) -> CostPeriod:
    return CostPeriod(start=start, end=end, days=(end - start).days)


def _patch_aggregator_retrievers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    per_account: Dict[str, Dict[str, Any]],
) -> None:
    """Patch the aggregator's per-account fetch helpers.

    The aggregator's ``_fetch_account_totals`` and
    ``_fetch_account_breakdowns`` are bound into its own module
    namespace by the ``from app.services.aws.cost_explorer import``
    statement.  Replacing those bound names bypasses any Boto3
    client construction entirely, which makes the tests fully
    deterministic.
    """

    async def _fake_totals(*, row, period, previous, region):
        payload = per_account.get(row.account_id, {})
        return (
            payload.get("current_total", Decimal("0")),
            payload.get("previous_total", Decimal("0")),
        )

    async def _fake_breakdowns(*, row, period, region):
        payload = per_account.get(row.account_id, {})
        return (
            payload.get("daily", []),
            payload.get("by_service", []),
            payload.get("by_region", []),
        )

    monkeypatch.setattr(
        "app.services.aws.cost_aggregator._fetch_account_totals",
        _fake_totals,
    )
    monkeypatch.setattr(
        "app.services.aws.cost_aggregator._fetch_account_breakdowns",
        _fake_breakdowns,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregator_sums_totals_across_accounts(monkeypatch):
    period = _period(date(2026, 9, 1), date(2026, 10, 1))
    previous = _period(date(2026, 8, 2), date(2026, 9, 1))
    _patch_aggregator_retrievers(
        monkeypatch,
        per_account={
            "111111111111": {
                "current_total": Decimal("100.00"),
                "previous_total": Decimal("80.00"),
                "daily": [
                    (date(2026, 9, 1), Decimal("50.00"), "USD"),
                    (date(2026, 9, 2), Decimal("50.00"), "USD"),
                ],
                "by_service": [("Amazon EC2", Decimal("80.00"), "USD"), ("AWS Lambda", Decimal("20.00"), "USD")],
                "by_region": [("us-east-1", Decimal("100.00"), "USD")],
            },
            "222222222222": {
                "current_total": Decimal("40.00"),
                "previous_total": Decimal("20.00"),
                "daily": [
                    (date(2026, 9, 1), Decimal("20.00"), "USD"),
                    (date(2026, 9, 2), Decimal("20.00"), "USD"),
                ],
                "by_service": [("Amazon EC2", Decimal("30.00"), "USD"), ("Amazon S3", Decimal("10.00"), "USD")],
                "by_region": [("us-west-2", Decimal("40.00"), "USD")],
            },
        },
    )
    rows = [_row("111111111111", "A"), _row("222222222222", "B")]
    report = await aggregate_cost_report_for_context(
        rows=rows, period=period, previous=previous, region="us-east-1"
    )
    assert report.status == "SUCCESS"
    assert report.total_cost == Decimal("140.00")
    assert report.previous_period_cost == Decimal("100.00")
    # EC2 must appear once with the summed total across accounts.
    ec2_total = next(amt for name, amt, _ in report.by_service if name == "Amazon EC2")
    assert ec2_total == Decimal("110.00")
    # Daily totals aligned by date.
    daily_totals = {d: amt for d, amt, _ in report.daily}
    assert daily_totals[date(2026, 9, 1)] == Decimal("70.00")
    assert daily_totals[date(2026, 9, 2)] == Decimal("70.00")


@pytest.mark.asyncio
async def test_aggregator_partial_success_when_account_denied(monkeypatch):
    period = _period(date(2026, 9, 1), date(2026, 10, 1))
    previous = _period(date(2026, 8, 2), date(2026, 9, 1))

    async def _fake_totals(*, row, period, previous, region):
        from botocore.exceptions import ClientError
        if row.account_id == "222222222222":
            return (Decimal("10.00"), Decimal("5.00"))
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetCostAndUsage"
        )

    async def _fake_breakdowns(*, row, period, region):
        return (
            [(date(2026, 9, 1), Decimal("10.00"), "USD")],
            [("Amazon EC2", Decimal("10.00"), "USD")],
            [("us-east-1", Decimal("10.00"), "USD")],
        )

    monkeypatch.setattr(
        "app.services.aws.cost_aggregator._fetch_account_totals",
        _fake_totals,
    )
    monkeypatch.setattr(
        "app.services.aws.cost_aggregator._fetch_account_breakdowns",
        _fake_breakdowns,
    )

    rows = [_row("111111111111", "Denied"), _row("222222222222", "OK")]
    report = await aggregate_cost_report_for_context(
        rows=rows, period=period, previous=previous, region="us-east-1"
    )
    assert report.status == "PARTIAL_SUCCESS"
    assert report.total_cost == Decimal("10.00")
    assert len(report.account_errors) == 1
    err = report.account_errors[0]
    assert err.account_id == "111111111111"
    assert err.code == "AccessDenied"
    # Sanitized message — never the raw AWS exception text.
    assert "AWS denied access" in err.message


@pytest.mark.asyncio
async def test_aggregator_empty_rows_is_success(monkeypatch):
    period = _period(date(2026, 9, 1), date(2026, 10, 1))
    previous = _period(date(2026, 8, 2), date(2026, 9, 1))
    report = await aggregate_cost_report_for_context(
        rows=[], period=period, previous=previous, region="us-east-1"
    )
    assert report.status == "SUCCESS"
    assert report.total_cost == Decimal("0")


@pytest.mark.asyncio
async def test_aggregator_does_not_average_totals(monkeypatch):
    """Critical invariant: aggregation is sum, not average."""
    period = _period(date(2026, 9, 1), date(2026, 10, 1))
    previous = _period(date(2026, 8, 2), date(2026, 9, 1))
    _patch_aggregator_retrievers(
        monkeypatch,
        per_account={
            "111111111111": {
                "current_total": Decimal("100.00"),
                "previous_total": Decimal("0.00"),
                "daily": [],
                "by_service": [],
                "by_region": [],
            },
            "222222222222": {
                "current_total": Decimal("200.00"),
                "previous_total": Decimal("0.00"),
                "daily": [],
                "by_service": [],
                "by_region": [],
            },
        },
    )
    rows = [_row("111111111111"), _row("222222222222")]
    report = await aggregate_cost_report_for_context(
        rows=rows, period=period, previous=previous, region="us-east-1"
    )
    assert report.total_cost == Decimal("300.00"), (
        "aggregator must SUM, not average — 100 + 200 = 300"
    )
