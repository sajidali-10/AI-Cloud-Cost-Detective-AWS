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


# ---------------------------------------------------------------------------
# Closure-fix regression tests — Phase 2 Cost Explorer patch.
#
# Two verified issues drove this regression class:
#
#   1. GetCostAndUsage does NOT accept MaxResults.  Sending it triggers
#      a ``ValidationException`` from the Boto3 ParamValidator before
#      the request ever leaves the host.  We assert the parameter is
#      absent from every outgoing request, regardless of retriever.
#
#   2. get_total_and_previous() issues *ungrouped* requests but the
#      older ``_sum_unblended_cost`` summed the (always-empty) Groups
#      list and ignored the period-level Total — which produced a
#      zero total on every ungrouped response.  The fixed helper
#      reads ResultsByTime[*].Total[UnblendedCost].Amount directly,
#      so a non-zero Total now produces a non-zero current + previous
#      total.
#
# We also re-pinned pagination: NextPageToken must still walk every
# page, with no MaxResults on any page request.
# ---------------------------------------------------------------------------


class TestClosureRegression:
    """Phase 2 closure fix regression tests."""

    def test_get_total_and_previous_never_sends_maxresults(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        prev = previous_period(period)
        fake = _FakeCE(
            pages=[
                {"ResultsByTime": [_period_block(period.start, "1.00")]},
                {"ResultsByTime": [_period_block(prev.start, "1.00")]},
            ]
        )
        get_total_and_previous(fake, period)
        # Two outgoing requests: current + previous.  Neither may
        # include MaxResults (Cost Explorer rejects it).
        assert len(fake.calls) == 2
        for kwargs in fake.calls:
            assert "MaxResults" not in kwargs, (
                "GetCostAndUsage rejects MaxResults; "
                f"unexpected param in call: {sorted(kwargs)}"
            )

    def test_get_daily_trend_never_sends_maxresults(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        fake = _FakeCE(pages=[{"ResultsByTime": []}])
        get_daily_trend(fake, period)
        assert len(fake.calls) >= 1
        for kwargs in fake.calls:
            assert "MaxResults" not in kwargs

    def test_get_by_service_never_sends_maxresults(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        fake = _FakeCE(pages=[{"ResultsByTime": []}])
        get_by_service(fake, period)
        for kwargs in fake.calls:
            assert "MaxResults" not in kwargs

    def test_get_by_region_never_sends_maxresults(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        fake = _FakeCE(pages=[{"ResultsByTime": []}])
        get_by_region(fake, period)
        for kwargs in fake.calls:
            assert "MaxResults" not in kwargs

    def test_ungrouped_response_produces_nonzero_current_and_previous(self) -> None:
        """Closure fix #2: ``_sum_unblended_cost`` reads the period-level Total.

        An ungrouped ``GetCostAndUsage`` response has ``Groups == []``
        on every ``ResultsByTime`` block, so the period-level
        ``Total[UnblendedCost].Amount`` is the ONLY place the dollar
        figure appears.  Before the fix the helper iterated ``Groups``
        first (yielding zero) and the current/previous totals collapsed
        to ``Decimal("0.00")`` even when the response carried real
        spend.
        """
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        prev = previous_period(period)
        # An authentic ungrouped response shape: each ResultsByTime
        # block has an empty Groups list and a Total at the period
        # level.  We pick non-zero amounts that the buggy implementation
        # would have silently zeroed.
        fake = _FakeCE(
            pages=[
                {
                    "ResultsByTime": [
                        _period_block(period.start, "12.34"),
                        _period_block(period.start + timedelta(days=1), "5.67"),
                        _period_block(period.start + timedelta(days=2), "0.50"),
                    ]
                },
                {
                    "ResultsByTime": [
                        _period_block(prev.start, "9.99"),
                    ]
                },
            ]
        )
        current, previous = get_total_and_previous(fake, period)
        assert current == Decimal("18.51")  # 12.34 + 5.67 + 0.50
        assert previous == Decimal("9.99")
        # Defensive: ensure we did NOT silently drop into zero, which
        # is the exact bug the closure fix was meant to retire.
        assert current != Decimal("0.00")
        assert previous != Decimal("0.00")

    def test_next_page_token_pagination_still_works(self) -> None:
        """Closure fix #1 does not regress pagination.

        A paginated response must walk every page via NextPageToken,
        and *no* page request may carry MaxResults.
        """
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        page1 = {
            "ResultsByTime": [
                _period_block(period.start + timedelta(days=i), "1.00")
                for i in range(3)
            ],
            "NextPageToken": "TOKEN-PAGE-2",
        }
        page2 = {
            "ResultsByTime": [
                _period_block(period.start + timedelta(days=i + 3), "2.00")
                for i in range(3)
            ],
            "NextPageToken": "TOKEN-PAGE-3",
        }
        page3 = {
            "ResultsByTime": [
                _period_block(period.start + timedelta(days=6), "3.00"),
            ],
        }
        fake = _FakeCE(pages=[page1, page2, page3])
        trend = get_daily_trend(fake, period)
        # 3 + 3 + 1 = 7 daily points aggregated across three pages.
        assert len(trend) == 7
        assert len(fake.calls) == 3
        # No call may include MaxResults.
        for kwargs in fake.calls:
            assert "MaxResults" not in kwargs
        # First call has no token; subsequent calls carry the token.
        assert "NextPageToken" not in fake.calls[0]
        assert fake.calls[1].get("NextPageToken") == "TOKEN-PAGE-2"
        assert fake.calls[2].get("NextPageToken") == "TOKEN-PAGE-3"

    def test_daily_trend_remains_correct_after_fix(self) -> None:
        today = date(2026, 10, 5)
        period = build_period(7, today=today)
        results_by_time = [
            _period_block(period.start + timedelta(days=i), str(1.5 * (i + 1)))
            for i in range(7)
        ]
        fake = _FakeCE(pages=[{"ResultsByTime": results_by_time}])
        trend = get_daily_trend(fake, period)
        assert len(trend) == 7
        for i, (day, amount, unit) in enumerate(trend):
            assert day == period.start + timedelta(days=i)
            assert isinstance(amount, Decimal)
            assert unit == "USD"

    def test_by_service_remains_correct_after_fix(self) -> None:
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

    def test_by_region_remains_correct_after_fix(self) -> None:
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
        # EC2-style sanity: us-east-1 must still be the largest bucket.
        assert rows[0][0] == "us-east-1"
        assert rows[0][1] == Decimal("42.00")
