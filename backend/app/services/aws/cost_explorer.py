"""AWS Cost Explorer service — Phase 2.

This module wraps the Boto3 ``ce`` (Cost Explorer) client with:

* an account-level client factory (Cost Explorer is a single global
  endpoint per account — there is NO per-region CE client);
* a strict UTC ``build_period(days)`` helper that enforces the
  ``{7, 30, 60, 90}`` allow-list and returns ``(start_inclusive,
  end_exclusive)`` in UTC;
* four retrievers — total_and_previous, daily_trend (DAILY), by_service
  (SERVICE), by_region (REGION) — that use ``UnblendedCost`` only and
  walk ``NextPageToken`` until the response is exhausted;
* a sanitized ``CostExplorerError`` exception type so credentials,
  request payloads, and stack traces never reach FastAPI handlers.

The module is pure-Python with no module-level boto3 work, so it is
importable from tests without a live AWS connection.  All Boto3 calls
go through the read-only guard in :mod:`app.services.aws.guard`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, List, Optional, Tuple

import boto3
from botocore.client import BaseClient
from botocore.config import Config as BotoConfig

from app.services.aws.guard import assert_read_only

# ---------------------------------------------------------------------------
# Constants — pinned from the Phase 2 spec.
# ---------------------------------------------------------------------------

# Allowed lookback windows (days).  Anything outside this set raises
# ``CostExplorerError`` BEFORE the AWS call, so we never bill for an
# obviously-bad request.
ALLOWED_LOOKBACK_DAYS: Tuple[int, ...] = (7, 30, 60, 90)

# Cost Explorer requires dates in ISO-8601 YYYY-MM-DD form.  We always
# pass UTC dates because the Cost Explorer API interprets the
# ``Start``/``End`` strings in UTC.
CE_DATE_FORMAT: str = "%Y-%m-%d"

# We pin to ``UnblendedCost`` only.  Mixing BlendedCost / AmortizedCost
# / NetUnblendedCost in the same report would silently change the
# meaning of every dollar figure, so the metric is hard-coded here.
COST_METRIC: str = "UnblendedCost"

# Per-Cost-Explorer-page maximum.  We keep it below the documented
# service maximum so a single response always fits comfortably inside
# FastAPI's default JSON body budget.
CE_PAGE_SIZE: int = 100000


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class CostExplorerError(RuntimeError):
    """Sanitized wrapper around any Cost Explorer failure.

    The constructor takes a stable ``code`` (e.g. ``"AccessDenied"``,
    ``"ValidationException"``, ``"Throttling"``) and a short public
    message.  The original Boto3 exception is intentionally swallowed
    so credentials, request payloads, and stack traces never leak
    through FastAPI's default error handler.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostPeriod:
    """A UTC Cost Explorer period: ``start`` inclusive, ``end`` exclusive.

    We use ``date`` rather than ``datetime`` because Cost Explorer's
    ``Start``/``End`` parameters are date strings, not timestamps.
    """

    start: date
    end: date
    days: int

    def to_ce_strings(self) -> Tuple[str, str]:
        return (
            self.start.strftime(CE_DATE_FORMAT),
            self.end.strftime(CE_DATE_FORMAT),
        )


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def build_period(days: int, *, today: Optional[date] = None) -> CostPeriod:
    """Validate ``days`` and return a UTC Cost Explorer period.

    ``days`` must be in ``ALLOWED_LOOKBACK_DAYS``.  The returned
    period covers the ``[today - days, today)`` interval in UTC —
    i.e. ``start`` is inclusive, ``end`` is exclusive (today is not
    yet a full day so we exclude it).
    """
    if days not in ALLOWED_LOOKBACK_DAYS:
        raise CostExplorerError(
            code="InvalidLookbackDays",
            message=(
                f"days={days!r} is not allowed; "
                f"allowed values are {sorted(ALLOWED_LOOKBACK_DAYS)}"
            ),
        )
    end = today or _today_utc()
    start = end - timedelta(days=days)
    return CostPeriod(start=start, end=end, days=days)


def previous_period(period: CostPeriod) -> CostPeriod:
    """Return the comparable period immediately before ``period``.

    For a 30-day current period we return the prior 30 days.
    """
    prev_end = period.start
    prev_start = prev_end - timedelta(days=period.days)
    return CostPeriod(start=prev_start, end=prev_end, days=period.days)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_cost_explorer_client(region: Optional[str] = None) -> BaseClient:
    """Return an account-level Cost Explorer client.

    Cost Explorer is a single global endpoint per account, so a region
    is not strictly required — but we accept it for symmetry with the
    other AWS services and to let callers override (e.g. for testing).

    Boto3 is configured with conservative retries and timeouts so a
    runaway call does not hang a FastAPI request.  Exponential
    backoff on throttling is Boto3's default.
    """
    config = BotoConfig(
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=30,
    )
    kwargs: dict[str, Any] = {"config": config}
    if region:
        kwargs["region_name"] = region
    return boto3.client("ce", **kwargs)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_boto_error(exc: Exception) -> CostExplorerError:
    """Map a Boto3 exception to a sanitized ``CostExplorerError``."""
    # ``ClientError`` carries ``ResponseMetadata.HTTPStatusCode`` and a
    # structured ``Error`` block.  We use only the high-level code —
    # never the message — because the latter often echoes request
    # payloads (which may contain account IDs we are already exposing).
    code = "CostExplorerError"
    try:
        # botocore.exceptions.ClientError path (most common)
        response = getattr(exc, "response", None) or {}
        err = response.get("Error") if isinstance(response, dict) else None
        if isinstance(err, dict) and err.get("Code"):
            code = str(err["Code"])
        elif hasattr(exc, "error_code") and exc.error_code:  # type: ignore[attr-defined]
            code = str(exc.error_code)
    except Exception:  # pragma: no cover - defensive
        pass
    return CostExplorerError(code=code, message="Cost Explorer request failed")


def _paginate(
    *,
    client: BaseClient,
    method_name: str,
    base_kwargs: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    """Yield ``ResultsByTime`` pages until ``NextPageToken`` is empty.

    The wrapper walks pagination transparently so individual
    retrievers can iterate over a flat stream of results.
    """
    assert_read_only(client, method_name)
    next_token: Optional[str] = None
    while True:
        # Build kwargs fresh and ONLY add NextPageToken when we have a
        # non-empty token.  Passing NextPageToken=None to Boto3 makes
        # its ParamValidator reject the call before dispatch, so we
        # must keep the key absent on the first (and last) page.
        kwargs = {k: v for k, v in base_kwargs.items() if v is not None}
        if next_token:
            kwargs["NextPageToken"] = next_token
        try:
            response = client.get_cost_and_usage(**kwargs) if method_name == "get_cost_and_usage" else client.get_cost_forecast(**kwargs)
        except Exception as exc:  # botocore raises many subclasses
            raise _sanitize_boto_error(exc) from None
        yield response
        next_token = response.get("NextPageToken")
        if not next_token:
            return


def _sum_unblended_cost(results_by_time: List[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for period_block in results_by_time:
        for group in period_block.get("Groups", []) or []:
            metrics = group.get("Metrics", {}) or {}
            unblended = metrics.get(COST_METRIC, {}) or {}
            amount = unblended.get("Amount", "0")
            total += Decimal(str(amount))
        # Periods with no groups still carry a Total at the period level.
        total_block = period_block.get("Total", {}) or {}
        unblended = total_block.get(COST_METRIC, {}) or {}
        amount = unblended.get("Amount", "0")
        total += Decimal(str(amount))
    return total.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Retrievers
# ---------------------------------------------------------------------------


def get_total_and_previous(
    client: BaseClient,
    period: CostPeriod,
) -> Tuple[Decimal, Decimal]:
    """Return ``(current_total, previous_total)`` as ``Decimal``.

    We make ONE ``get_cost_and_usage`` call with ``Metrics=UnblendedCost``
    and no group-by; the response's ``Total`` field gives us the
    current period's spend, and a second call with the previous
    period gives the comparison.
    """
    cur_str = period.to_ce_strings()
    prev = previous_period(period)
    prev_str = prev.to_ce_strings()

    base = {
        "TimePeriod": {"Start": cur_str[0], "End": cur_str[1]},
        "Granularity": "DAILY",
        "Metrics": [COST_METRIC],
        "MaxResults": CE_PAGE_SIZE,
    }
    pages = list(_paginate(client=client, method_name="get_cost_and_usage", base_kwargs=base))
    current_total = _sum_unblended_cost(pages[0].get("ResultsByTime", []) if pages else [])

    base_prev = {
        "TimePeriod": {"Start": prev_str[0], "End": prev_str[1]},
        "Granularity": "DAILY",
        "Metrics": [COST_METRIC],
        "MaxResults": CE_PAGE_SIZE,
    }
    pages_prev = list(
        _paginate(client=client, method_name="get_cost_and_usage", base_kwargs=base_prev)
    )
    previous_total = (
        _sum_unblended_cost(pages_prev[0].get("ResultsByTime", [])) if pages_prev else Decimal("0")
    )
    return current_total, previous_total


def get_daily_trend(
    client: BaseClient,
    period: CostPeriod,
) -> List[Tuple[date, Decimal, str]]:
    """Return ``[(date, amount, unit), ...]`` at DAILY granularity."""
    cur_str = period.to_ce_strings()
    base = {
        "TimePeriod": {"Start": cur_str[0], "End": cur_str[1]},
        "Granularity": "DAILY",
        "Metrics": [COST_METRIC],
        "MaxResults": CE_PAGE_SIZE,
    }
    out: List[Tuple[date, Decimal, str]] = []
    unit: str = "USD"
    for response in _paginate(client=client, method_name="get_cost_and_usage", base_kwargs=base):
        for period_block in response.get("ResultsByTime", []) or []:
            time_range = period_block.get("TimePeriod", {}) or {}
            start_str = time_range.get("Start")
            if not start_str:
                continue
            day = datetime.strptime(start_str, CE_DATE_FORMAT).date()
            total_block = period_block.get("Total", {}) or {}
            unblended = total_block.get(COST_METRIC, {}) or {}
            amount = Decimal(str(unblended.get("Amount", "0"))).quantize(Decimal("0.01"))
            unit = str(unblended.get("Unit", unit))
            out.append((day, amount, unit))
    return out


def get_by_service(
    client: BaseClient,
    period: CostPeriod,
) -> List[Tuple[str, Decimal, str]]:
    """Return ``[(service_name, amount, unit), ...]`` grouped by SERVICE."""
    cur_str = period.to_ce_strings()
    base = {
        "TimePeriod": {"Start": cur_str[0], "End": cur_str[1]},
        "Granularity": "MONTHLY",
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        "Metrics": [COST_METRIC],
        "MaxResults": CE_PAGE_SIZE,
    }
    return _aggregate_grouped(client, base)


def get_by_region(
    client: BaseClient,
    period: CostPeriod,
) -> List[Tuple[str, Decimal, str]]:
    """Return ``[(region_name, amount, unit), ...]`` grouped by REGION.

    Some services (e.g. global ones) report ``no_region``.  We keep
    that key verbatim so callers can decide whether to surface it or
    drop it from the report.
    """
    cur_str = period.to_ce_strings()
    base = {
        "TimePeriod": {"Start": cur_str[0], "End": cur_str[1]},
        "Granularity": "MONTHLY",
        "GroupBy": [{"Type": "DIMENSION", "Key": "REGION"}],
        "Metrics": [COST_METRIC],
        "MaxResults": CE_PAGE_SIZE,
    }
    return _aggregate_grouped(client, base)


def _aggregate_grouped(
    client: BaseClient,
    base_kwargs: dict[str, Any],
) -> List[Tuple[str, Decimal, str]]:
    accum: dict[str, Decimal] = {}
    unit: str = "USD"
    for response in _paginate(client=client, method_name="get_cost_and_usage", base_kwargs=base_kwargs):
        for period_block in response.get("ResultsByTime", []) or []:
            for group in period_block.get("Groups", []) or []:
                keys = group.get("Keys", []) or [""]
                key = keys[0] if keys else ""
                metrics = group.get("Metrics", {}) or {}
                unblended = metrics.get(COST_METRIC, {}) or {}
                amount = Decimal(str(unblended.get("Amount", "0")))
                unit = str(unblended.get("Unit", unit))
                accum[key] = accum.get(key, Decimal("0")) + amount
    out = [
        (key, value.quantize(Decimal("0.01")), unit)
        for key, value in accum.items()
    ]
    out.sort(key=lambda row: row[1], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostReport:
    """Raw aggregate of all four retrievers for a given period."""

    period: CostPeriod
    previous: CostPeriod
    current_total: Decimal
    previous_total: Decimal
    daily: List[Tuple[date, Decimal, str]]
    by_service: List[Tuple[str, Decimal, str]]
    by_region: List[Tuple[str, Decimal, str]]
    estimated: bool
    unit: str


def aggregate_cost_report(
    client: BaseClient,
    days: int,
    *,
    today: Optional[date] = None,
) -> CostReport:
    """Compose the four retrievers into a single ``CostReport``.

    Errors raised by any retriever propagate as ``CostExplorerError``;
    the route layer is responsible for turning them into a sanitized
    HTTP response.
    """
    period = build_period(days, today=today)
    prev = previous_period(period)
    current_total, previous_total = get_total_and_previous(client, period)
    daily = get_daily_trend(client, period)
    by_service = get_by_service(client, period)
    by_region = get_by_region(client, period)
    unit = "USD"
    if daily and daily[0][2]:
        unit = daily[0][2]
    elif by_service and by_service[0][2]:
        unit = by_service[0][2]
    return CostReport(
        period=period,
        previous=prev,
        current_total=current_total,
        previous_total=previous_total,
        daily=daily,
        by_service=by_service,
        by_region=by_region,
        estimated=False,  # Cost Explorer does not surface "estimated" in this call shape
        unit=unit,
    )


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "CE_DATE_FORMAT",
    "CE_PAGE_SIZE",
    "COST_METRIC",
    "CostExplorerError",
    "CostPeriod",
    "CostReport",
    "aggregate_cost_report",
    "build_period",
    "get_by_region",
    "get_by_service",
    "get_cost_explorer_client",
    "get_daily_trend",
    "get_total_and_previous",
    "previous_period",
]
