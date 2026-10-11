"""Phase 7B — multi-account Cost Explorer aggregator.

The aggregator takes an :class:`AccountContext` (``all`` or a single
account) and returns a single :class:`MultiAccountCostReport` whose
math is **always** deterministic:

* Total spend = sum(account totals).  No averaging.  No ratios.
* By-service totals = sum of identical service keys across accounts.
  AWS uses the same service identifier everywhere (``Amazon EC2``,
  ``AWS Lambda``, etc.) so a plain ``dict`` accumulator is correct.
* By-region totals = sum of identical region keys across accounts.
* Daily trend = aligned-by-date sum; missing days are filled with 0.
* All-account rollup keeps per-account totals in ``by_account`` so
  the UI can drill in.

Failure isolation
-----------------

Each account is queried independently.  An error from one account
captures a sanitized :class:`AccountError` entry in
``MultiAccountCostReport.account_errors`` and **does not** fail the
overall response.  The rollup is marked with
``status="SUCCESS"`` or ``status="PARTIAL_SUCCESS"`` accordingly.

Bounded concurrency
-------------------

The aggregator uses an :class:`asyncio.Semaphore` of size
``MULTI_ACCOUNT_MAX_CONCURRENCY`` (env-driven, default ``4``) so a
slow account cannot serialise the rest of the fan-out.  Each per-
account fetch has its own ``PHASE7B_COST_ACCOUNT_TIMEOUT_SECONDS``
ceiling (default ``30``).
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from app.db.models import CloudAccount
from app.services.aws.cost_explorer import (
    CostExplorerError,
    CostPeriod,
    CostReport,
    aggregate_cost_report_for_period,
    get_by_region,
    get_by_service,
    get_cost_explorer_client,
    get_daily_trend,
    get_total_and_previous,
)

logger = logging.getLogger("cost-detective.cost_aggregator")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


def _max_concurrency() -> int:
    return int(os.getenv("PHASE7B_COST_MAX_CONCURRENCY", "4"))


def _account_timeout_seconds() -> float:
    return float(os.getenv("PHASE7B_COST_ACCOUNT_TIMEOUT_SECONDS", "30"))


def _daily_timeout_seconds() -> float:
    return float(os.getenv("PHASE7B_COST_DAILY_TIMEOUT_SECONDS", "45"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountError:
    """A sanitized per-account failure.

    ``code`` is the Boto3 error code (e.g. ``"AccessDenied"``,
    ``"NoSuchEntity"``) or ``"CostExplorerError"``.  ``message`` is
    a fixed, operator-readable reason — never a raw AWS exception
    payload.
    """

    account_id: str
    display_name: str
    code: str
    message: str


@dataclass
class MultiAccountCostReport:
    """The aggregated report returned to the API layer."""

    status: str  # SUCCESS | PARTIAL_SUCCESS | ERROR
    period: CostPeriod
    previous: CostPeriod
    currency: str
    total_cost: Decimal
    previous_period_cost: Decimal
    daily: List[Tuple[date, Decimal, str]]
    by_service: List[Tuple[str, Decimal, str]]
    by_region: List[Tuple[str, Decimal, str]]
    by_account: List[Tuple[str, str, Decimal]]  # (account_id, display_name, total)
    account_errors: List[AccountError] = field(default_factory=list)

    def to_summary_dict(self, *, scope: str) -> Dict[str, Any]:
        change = self.total_cost - self.previous_period_cost
        if self.previous_period_cost == 0:
            change_percent: Optional[Decimal] = None
        else:
            change_percent = (change / self.previous_period_cost).quantize(Decimal("0.01"))
        return {
            "scope": scope,
            "status": self.status,
            "period": {
                "start": self.period.start.isoformat(),
                "end": self.period.end.isoformat(),
                "days": self.period.days,
            },
            "previous_period": {
                "start": self.previous.start.isoformat(),
                "end": self.previous.end.isoformat(),
                "days": self.previous.days,
            },
            "currency": self.currency,
            "total_cost": str(self.total_cost),
            "previous_period_cost": str(self.previous_period_cost),
            "change_amount": str(change),
            "change_percent": str(change_percent) if change_percent is not None else None,
            "by_account": [
                {"account_id": aid, "display_name": name, "amount": str(amount)}
                for aid, name, amount in self.by_account
            ],
            "account_errors": [
                {
                    "account_id": e.account_id,
                    "display_name": e.display_name,
                    "code": e.code,
                    "message": e.message,
                }
                for e in self.account_errors
            ],
        }


# ---------------------------------------------------------------------------
# Per-account fetch
# ---------------------------------------------------------------------------


async def _fetch_account_report(
    *,
    row: CloudAccount,
    period: CostPeriod,
    previous: CostPeriod,
    region: Optional[str],
) -> CostReport:
    """Fetch a single account's :class:`CostReport`.

    Errors are normalised into :class:`CostExplorerError` so the
    aggregator can capture them without leaking raw Boto3 payloads.
    """
    effective_region = region or row.preferred_region

    def _build() -> CostReport:
        client = get_cost_explorer_client(
            region=effective_region, account_row=row
        )
        return aggregate_cost_report_for_period(
            client, period, previous, account_id=row.account_id
        )

    return await asyncio.to_thread(_build)


async def _fetch_account_totals(
    *,
    row: CloudAccount,
    period: CostPeriod,
    previous: CostPeriod,
    region: Optional[str],
) -> Tuple[Decimal, Decimal]:
    """Just the current + previous totals; used for cache hits."""
    effective_region = region or row.preferred_region

    def _build() -> Tuple[Decimal, Decimal]:
        client = get_cost_explorer_client(
            region=effective_region, account_row=row
        )
        return get_total_and_previous(client, period)

    return await asyncio.to_thread(_build)


async def _fetch_account_breakdowns(
    *,
    row: CloudAccount,
    period: CostPeriod,
    region: Optional[str],
) -> Tuple[
    List[Tuple[date, Decimal, str]],
    List[Tuple[str, Decimal, str]],
    List[Tuple[str, Decimal, str]],
]:
    effective_region = region or row.preferred_region

    def _build() -> Tuple[
        List[Tuple[date, Decimal, str]],
        List[Tuple[str, Decimal, str]],
        List[Tuple[str, Decimal, str]],
    ]:
        client = get_cost_explorer_client(
            region=effective_region, account_row=row
        )
        daily = get_daily_trend(client, period)
        by_service = get_by_service(client, period)
        by_region = get_by_region(client, period)
        return daily, by_service, by_region

    return await asyncio.to_thread(_build)


# ---------------------------------------------------------------------------
# Sanitized error mapping
# ---------------------------------------------------------------------------


def _sanitize_error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    if isinstance(exc, CostExplorerError):
        return exc.code
    if isinstance(exc, BotoCoreError):
        return type(exc).__name__
    return type(exc).__name__


def _sanitize_error_message(code: str) -> str:
    if code == "AccessDenied":
        return "AWS denied access for this account"
    if code == "NoSuchEntity":
        return "the configured role was not found"
    if code in ("InvalidClientTokenId", "ExpiredToken"):
        return "AWS credentials were rejected"
    if code == "Throttling":
        return "AWS request was throttled"
    if code == "OptInRequired":
        return "Cost Explorer is not enabled for this account"
    return f"AWS request failed ({code})"


# ---------------------------------------------------------------------------
# The aggregator itself
# ---------------------------------------------------------------------------


def _empty_report(
    period: CostPeriod, previous: CostPeriod, status: str = "ERROR"
) -> MultiAccountCostReport:
    return MultiAccountCostReport(
        status=status,
        period=period,
        previous=previous,
        currency="USD",
        total_cost=Decimal("0"),
        previous_period_cost=Decimal("0"),
        daily=[],
        by_service=[],
        by_region=[],
        by_account=[],
        account_errors=[],
    )


async def aggregate_cost_report_for_context(
    *,
    rows: Sequence[CloudAccount],
    period: CostPeriod,
    previous: CostPeriod,
    region: Optional[str],
) -> MultiAccountCostReport:
    """Fan out Cost Explorer across ``rows`` and return the rollup.

    ``rows`` is the *resolved* set of accounts to query — the caller
    is responsible for translating the account-context selector into
    a concrete list.  This keeps the function deterministic and
    trivially testable.
    """
    if not rows:
        return _empty_report(period, previous, status="SUCCESS")

    sem = asyncio.Semaphore(_max_concurrency())
    timeout = _account_timeout_seconds()
    daily_timeout = _daily_timeout_seconds()

    async def _safe_totals(row: CloudAccount) -> Tuple[CloudAccount, Optional[Tuple[Decimal, Decimal]], Optional[AccountError]]:
        async with sem:
            try:
                totals = await asyncio.wait_for(
                    _fetch_account_totals(
                        row=row, period=period, previous=previous, region=region
                    ),
                    timeout=timeout,
                )
                return row, totals, None
            except Exception as exc:  # noqa: BLE001 — we sanitize and continue
                code = _sanitize_error_code(exc)
                err = AccountError(
                    account_id=row.account_id,
                    display_name=row.display_name,
                    code=code,
                    message=_sanitize_error_message(code),
                )
                logger.warning(
                    "cost_aggregator.account_totals_failed account=%s code=%s",
                    row.account_id, code,
                )
                return row, None, err

    totals_results = await asyncio.gather(
        *(_safe_totals(row) for row in rows)
    )

    # ---- accounts whose totals fetched successfully ----
    successful_rows: List[CloudAccount] = []
    errors: List[AccountError] = []
    by_account: List[Tuple[str, str, Decimal]] = []
    total_cost = Decimal("0")
    previous_total = Decimal("0")
    currency = "USD"
    for row, totals, err in totals_results:
        if err is not None:
            errors.append(err)
            continue
        assert totals is not None
        cur, prev = totals
        total_cost += cur
        previous_total += prev
        successful_rows.append(row)
        by_account.append((row.account_id, row.display_name, cur))

    # Now fetch breakdowns only for the successful rows, with a
    # tighter daily timeout since daily trend paginates and tends to
    # be slower than the summary call.
    async def _safe_breakdowns(
        row: CloudAccount,
    ) -> Tuple[CloudAccount, Optional[Tuple[
        List[Tuple[date, Decimal, str]],
        List[Tuple[str, Decimal, str]],
        List[Tuple[str, Decimal, str]],
    ]], Optional[AccountError]]:
        async with sem:
            try:
                data = await asyncio.wait_for(
                    _fetch_account_breakdowns(
                        row=row, period=period, region=region
                    ),
                    timeout=daily_timeout,
                )
                return row, data, None
            except Exception as exc:  # noqa: BLE001
                code = _sanitize_error_code(exc)
                err = AccountError(
                    account_id=row.account_id,
                    display_name=row.display_name,
                    code=code,
                    message=_sanitize_error_message(code),
                )
                logger.warning(
                    "cost_aggregator.breakdowns_failed account=%s code=%s",
                    row.account_id, code,
                )
                return row, None, err

    breakdown_results = await asyncio.gather(
        *(_safe_breakdowns(row) for row in successful_rows)
    )

    # ---- accumulate breakdowns ----
    daily_accum: Dict[date, Tuple[Decimal, str]] = {}
    service_accum: Dict[str, Tuple[Decimal, str]] = {}
    region_accum: Dict[str, Tuple[Decimal, str]] = {}
    for row, data, err in breakdown_results:
        if err is not None:
            errors.append(err)
            continue
        assert data is not None
        daily, by_service, by_region = data
        for d, amount, unit in daily:
            prior_amount, prior_unit = daily_accum.get(d, (Decimal("0"), unit))
            daily_accum[d] = (prior_amount + amount, prior_unit or unit)
            currency = prior_unit or unit
        for name, amount, unit in by_service:
            prior_amount, prior_unit = service_accum.get(name, (Decimal("0"), unit))
            service_accum[name] = (prior_amount + amount, prior_unit or unit)
        for name, amount, unit in by_region:
            prior_amount, prior_unit = region_accum.get(name, (Decimal("0"), unit))
            region_accum[name] = (prior_amount + amount, prior_unit or unit)

    daily = sorted(
        ((d, amt.quantize(Decimal("0.01")), unit) for d, (amt, unit) in daily_accum.items()),
        key=lambda row: row[0],
    )
    by_service = sorted(
        ((name, amt.quantize(Decimal("0.01")), unit) for name, (amt, unit) in service_accum.items()),
        key=lambda row: row[1],
        reverse=True,
    )
    by_region = sorted(
        ((name, amt.quantize(Decimal("0.01")), unit) for name, (amt, unit) in region_accum.items()),
        key=lambda row: row[1],
        reverse=True,
    )
    by_account.sort(key=lambda row: row[2], reverse=True)

    status = "PARTIAL_SUCCESS" if errors else "SUCCESS"
    return MultiAccountCostReport(
        status=status,
        period=period,
        previous=previous,
        currency=currency or "USD",
        total_cost=total_cost.quantize(Decimal("0.01")),
        previous_period_cost=previous_total.quantize(Decimal("0.01")),
        daily=daily,
        by_service=by_service,
        by_region=by_region,
        by_account=by_account,
        account_errors=errors,
    )


__all__ = [
    "AccountError",
    "MultiAccountCostReport",
    "aggregate_cost_report_for_context",
]
