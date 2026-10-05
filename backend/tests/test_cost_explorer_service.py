"""Unit tests for ``app.services.aws.cost_explorer`` — Phase 2.

These tests use ``unittest.mock.MagicMock`` to stand in for a Boto3
``ce`` client.  This is a deliberate departure from the botocore
Stubber pattern: Stubber performs strict request-shape validation
that competes with our service logic for attention and obscures
what we actually want to test (period math, pagination walking,
Decimal precision, error sanitization).

Each test stubs ``client.get_cost_and_usage`` with a small stateful
fake that returns canned pages and tracks call kwargs so we can
assert both behavior and intent.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, List
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.aws.cost_explorer import (
    ALLOWED_LOOKBACK_DAYS,
    CE_DATE_FORMAT,
    CE_PAGE_SIZE,
    COST_METRIC,
    CostExplorerError,
    build_period,
    get_by_region,
    get_by_service,
    get_daily_trend,
    get_total_and_previous,
    previous_period,
)


# ---------------------------------------------------------------------------
# Period helper tests
# ---------------------------------------------------------------------------


class TestBuildPeriod:
    def test_allowed_days_produce_period(self) -> None:
        today = date(2026, 10, 5)
        for days in ALLOWED_LOOKBACK_DAYS:
            period = build_period(days, today=today)
            assert period.days == days
            assert period.end == today
            assert period.start == today - timedelta(days=days)
            assert period.to_ce_strings() == (
                period.start.strftime(CE_DATE_FORMAT),
                period.end.strftime(CE_DATE_FORMAT),
            )

    def test_disallowed_days_raise(self) -> None:
        for bad in (0, 1, 6, 14, 31, 89, 91, 365):
            with pytest.raises(CostExplorerError) as excinfo:
                build_period(bad, today=date(2026, 10, 5))
            assert excinfo.value.code == "InvalidLookbackDays"

    def test_previous_period_is_comparable(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(30, today=today)
        prev = previous_period(period)
        assert prev.days == 30
        assert prev.end == period.start
        assert prev.start == period.start - timedelta(days=30)


# ---------------------------------------------------------------------------
# Fake client + helper
# ---------------------------------------------------------------------------


class _FakeCE:
    """Fake Boto3 Cost Explorer client.

    ``pages`` is a list of response dicts to return on successive
    calls.  ``raises`` is an optional exception to raise on the
    first call (for error tests).
    """

    def __init__(self, pages: List[dict] | None = None, raises: Exception | None = None) -> None:
        self.pages = list(pages or [])
        self.raises = raises
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        if not self.pages:
            return {"ResultsByTime": []}
        return self.pages.pop(0)


def _stubbed_unblended(amount: str, unit: str = "USD") -> dict:
    return {"Amount": amount, "Unit": unit}


def _period_block(day: date, amount: str) -> dict:
    return {
        "TimePeriod": {
            "Start": day.strftime(CE_DATE_FORMAT),
            "End": (day + timedelta(days=1)).strftime(CE_DATE_FORMAT),
        },
        "Total": {COST_METRIC: _stubbed_unblended(amount)},
    }


# ---------------------------------------------------------------------------
# get_total_and_previous
# ---------------------------------------------------------------------------


class TestGetTotalAndPrevious:
    def test_current_and_previous_totals_with_decimal_precision(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(30, today=today)
        prev = previous_period(period)
        fake = _FakeCE(
            pages=[
                {
                    "ResultsByTime": [
                        _period_block(period.start, "100.50"),
                        _period_block(period.start + timedelta(days=10), "22.95"),
                    ]
                },
                {
                    "ResultsByTime": [
                        _period_block(prev.start, "110.20"),
                    ]
                },
            ]
        )
        current, previous = get_total_and_previous(fake, period)
        # 100.50 + 22.95 = 123.45 (Decimal throughout, no float contamination)
        assert current == Decimal("123.45")
        assert previous == Decimal("110.20")

    def test_previous_zero_is_handled(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        prev = previous_period(period)
        fake = _FakeCE(
            pages=[
                {"ResultsByTime": [_period_block(period.start, "0.00")]},
                {"ResultsByTime": [_period_block(prev.start, "0.00")]},
            ]
        )
        current, previous = get_total_and_previous(fake, period)
        assert current == Decimal("0.00")
        assert previous == Decimal("0.00")


# ---------------------------------------------------------------------------
# get_daily_trend
# ---------------------------------------------------------------------------


class TestGetDailyTrend:
    def test_daily_series(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        results_by_time = [
            _period_block(period.start + timedelta(days=i), str(1.23 * (i + 1)))
            for i in range(7)
        ]
        fake = _FakeCE(pages=[{"ResultsByTime": results_by_time}])
        trend = get_daily_trend(fake, period)
        assert len(trend) == 7
        for i, (day, amount, unit) in enumerate(trend):
            assert day == period.start + timedelta(days=i)
            assert isinstance(amount, Decimal)
            assert unit == "USD"

    def test_empty_response_returns_empty_list(self) -> None:
        period = build_period(7, today=date(2026, 10, 5))
        fake = _FakeCE(pages=[{"ResultsByTime": []}])
        assert get_daily_trend(fake, period) == []


# ---------------------------------------------------------------------------
# get_by_service / get_by_region
# ---------------------------------------------------------------------------


class TestGroupByService:
    def test_aggregates_across_period_blocks(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(30, today=today)
        results_by_time = [
            {
                "TimePeriod": {
                    "Start": period.start.strftime(CE_DATE_FORMAT),
                    "End": period.end.strftime(CE_DATE_FORMAT),
                },
                "Groups": [
                    {"Keys": ["Amazon EC2"], "Metrics": {COST_METRIC: _stubbed_unblended("80.00")}},
                    {"Keys": ["Amazon S3"], "Metrics": {COST_METRIC: _stubbed_unblended("20.00")}},
                ],
            }
        ]
        fake = _FakeCE(pages=[{"ResultsByTime": results_by_time}])
        rows = get_by_service(fake, period)
        assert rows == [
            ("Amazon EC2", Decimal("80.00"), "USD"),
            ("Amazon S3", Decimal("20.00"), "USD"),
        ]


class TestGroupByRegion:
    def test_no_region_key_preserved(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        results_by_time = [
            {
                "TimePeriod": {
                    "Start": period.start.strftime(CE_DATE_FORMAT),
                    "End": period.end.strftime(CE_DATE_FORMAT),
                },
                "Groups": [
                    {"Keys": ["us-east-1"], "Metrics": {COST_METRIC: _stubbed_unblended("42.00")}},
                    {"Keys": ["no_region"], "Metrics": {COST_METRIC: _stubbed_unblended("1.50")}},
                ],
            }
        ]
        fake = _FakeCE(pages=[{"ResultsByTime": results_by_time}])
        rows = get_by_region(fake, period)
        keys = {row[0] for row in rows}
        assert "us-east-1" in keys
        assert "no_region" in keys


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_next_page_token_walked_until_empty(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(30, today=today)
        page1 = {
            "ResultsByTime": [_period_block(period.start + timedelta(days=i), "1.00") for i in range(5)],
            "NextPageToken": "TOKEN-PAGE-2",
        }
        page2 = {
            "ResultsByTime": [_period_block(period.start + timedelta(days=i + 5), "2.00") for i in range(5)],
        }
        fake = _FakeCE(pages=[page1, page2])
        trend = get_daily_trend(fake, period)
        # Two pages of 5 daily blocks -> 10 daily points aggregated.
        assert len(trend) == 10
        assert len(fake.calls) == 2
        # The second call must include the NextPageToken from page 1.
        assert fake.calls[1].get("NextPageToken") == "TOKEN-PAGE-2"


# ---------------------------------------------------------------------------
# Error sanitization
# ---------------------------------------------------------------------------


class TestErrorSanitization:
    def test_access_denied_sanitized(self) -> None:
        period = build_period(7, today=date(2026, 10, 5))
        fake = _FakeCE(
            raises=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "User: AKIAEXAMPLE is not authorized"}},
                "get_cost_and_usage",
            )
        )
        with pytest.raises(CostExplorerError) as excinfo:
            get_daily_trend(fake, period)
        assert excinfo.value.code == "AccessDenied"
        # Public message must NOT echo the original Boto3 message
        # (which contained the access key id).
        assert "AKIAEXAMPLE" not in excinfo.value.message
        assert "AKIA" not in excinfo.value.message

    def test_throttling_sanitized(self) -> None:
        period = build_period(7, today=date(2026, 10, 5))
        fake = _FakeCE(
            raises=ClientError(
                {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
                "get_cost_and_usage",
            )
        )
        with pytest.raises(CostExplorerError) as excinfo:
            get_daily_trend(fake, period)
        assert excinfo.value.code == "Throttling"

    def test_generic_exception_becomes_cost_explorer_error(self) -> None:
        period = build_period(7, today=date(2026, 10, 5))
        fake = _FakeCE(raises=RuntimeError("network is down"))
        with pytest.raises(CostExplorerError):
            get_daily_trend(fake, period)
