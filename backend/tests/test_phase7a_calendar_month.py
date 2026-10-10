"""Phase 7A — Calendar-month Cost Explorer contract tests.

These tests pin the new public surface of ``cost_explorer.build_*``
and the `/api/aws/costs?month=` route without contacting AWS.  All
values are checked against in-process dataclass / Pydantic
contracts — no fixtures, no mocks.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.aws import cost_explorer


# ---------------------------------------------------------------------------
# parse_month
# ---------------------------------------------------------------------------


def test_parse_month_accepts_valid_value() -> None:
    assert cost_explorer.parse_month("2026-09") == (2026, 9)


def test_parse_month_rejects_malformed() -> None:
    for bad in ("", "2026", "2026-9", "2026-13", "2026-00", "abcd-ef", "26-09"):
        with pytest.raises(cost_explorer.CostExplorerError) as exc:
            cost_explorer.parse_month(bad)
        assert exc.value.code == "InvalidMonth"


# ---------------------------------------------------------------------------
# build_month_period
# ---------------------------------------------------------------------------


def test_build_month_period_full_completed_month() -> None:
    p = cost_explorer.build_month_period("2026-09", today=date(2026, 10, 5))
    assert p.start == date(2026, 9, 1)
    assert p.end == date(2026, 10, 1)
    assert p.days == 30


def test_build_month_period_current_month_clips_to_mtd() -> None:
    # October 1 through 9 → MTD clips at today+1 = 2026-10-10.
    p = cost_explorer.build_month_period("2026-10", today=date(2026, 10, 9))
    assert p.start == date(2026, 10, 1)
    assert p.end == date(2026, 10, 10)
    assert p.days == 9


def test_build_month_period_december_wraps_to_next_year() -> None:
    p = cost_explorer.build_month_period("2026-12", today=date(2026, 11, 15))
    assert p.start == date(2026, 12, 1)
    assert p.end == date(2027, 1, 1)
    assert p.days == 31


# ---------------------------------------------------------------------------
# previous_month_period
# ---------------------------------------------------------------------------


def test_previous_month_period_full() -> None:
    p = cost_explorer.previous_month_period("2026-09")
    assert p.start == date(2026, 8, 1)
    assert p.end == date(2026, 9, 1)


def test_previous_month_period_wraps_year() -> None:
    p = cost_explorer.previous_month_period("2026-01")
    assert p.start == date(2025, 12, 1)
    assert p.end == date(2026, 1, 1)


# ---------------------------------------------------------------------------
# matched_mtd_previous_period
# ---------------------------------------------------------------------------


def test_matched_mtd_returns_same_day_window_in_previous_month() -> None:
    # Today = Oct 9.  Previous MTD = Sep 1-9.
    p = cost_explorer.matched_mtd_previous_period("2026-10", today=date(2026, 10, 9))
    assert p.start == date(2026, 9, 1)
    assert p.end == date(2026, 9, 10)
    assert p.days == 9


def test_matched_mtd_for_completed_month_falls_back_to_full_previous() -> None:
    # September is complete today; matched-MTD only applies to
    # current-month requests.
    p = cost_explorer.matched_mtd_previous_period("2026-09", today=date(2026, 10, 5))
    assert p.start == date(2026, 8, 1)
    assert p.end == date(2026, 9, 1)


def test_matched_mtd_clamps_to_previous_month_length() -> None:
    # Sept has 30 days.  Asking for Oct 31 → prev = Sep 1..Oct 1
    # but we clamp end to Sep 30+1 = Oct 1 already.  Test March-31
    # instead so the previous month is February (28/29 days).
    p = cost_explorer.matched_mtd_previous_period("2026-03", today=date(2026, 3, 31))
    # prev_year=2026, prev_m=2; leap year? 2026 is not.  Feb=28.
    assert p.start == date(2026, 2, 1)
    assert p.end == date(2026, 3, 1)  # clamped to Feb end + 1
    assert p.days == 28


# ---------------------------------------------------------------------------
# list_recent_calendar_months
# ---------------------------------------------------------------------------


def test_list_recent_calendar_months_returns_12_entries() -> None:
    months = cost_explorer.list_recent_calendar_months(today=date(2026, 10, 9))
    assert len(months) == 12
    assert months[0] == "2025-11"
    assert months[-1] == "2026-10"


def test_list_recent_calendar_months_wraps_year() -> None:
    months = cost_explorer.list_recent_calendar_months(today=date(2026, 2, 5), count=3)
    assert months == ["2025-12", "2026-01", "2026-02"]


# ---------------------------------------------------------------------------
# build_explicit_period
# ---------------------------------------------------------------------------


def test_build_explicit_period_happy_path() -> None:
    p = cost_explorer.build_explicit_period(date(2026, 9, 1), date(2026, 10, 1))
    assert p.days == 30
    assert p.start == date(2026, 9, 1)
    assert p.end == date(2026, 10, 1)


def test_build_explicit_period_rejects_inverted_range() -> None:
    with pytest.raises(cost_explorer.CostExplorerError) as exc:
        cost_explorer.build_explicit_period(date(2026, 10, 1), date(2026, 9, 1))
    assert exc.value.code == "InvalidDates"


def test_build_explicit_period_rejects_too_long_range() -> None:
    with pytest.raises(cost_explorer.CostExplorerError) as exc:
        cost_explorer.build_explicit_period(date(2020, 1, 1), date(2026, 1, 1))
    assert exc.value.code == "InvalidDates"
