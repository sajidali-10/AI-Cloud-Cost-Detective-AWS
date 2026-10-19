"""Phase 7D.4 — canonical period + scope resolution.

Extracted from ``app.api.cloud_costs`` so the multi-cloud AI
FinOps context builder can resolve the canonical (period, scope)
tuple without importing from the API layer.  These are pure
functions of the request parameters + a DB session — they have
no FastAPI dependency and belong in the service layer.

The API route layer (``app.api.cloud_costs``) re-uses these
helpers so the AI path and the UI path always agree on the same
canonical period semantics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.services.aws.cost_explorer import (
    ALLOWED_LOOKBACK_DAYS,
    build_explicit_period,
    build_month_period,
    build_period,
    matched_mtd_previous_period,
    parse_month,
    previous_month_period,
)
from app.services.aws.account_provider import (
    iter_enabled_accounts,
    get_management_account,
)
from app.services.cloud.models import CloudProviderKind

logger = logging.getLogger("cost-detective-backend.cloud.period_resolver")


@dataclass(frozen=True)
class ResolvedPeriod:
    """Canonical cost-period tuple.

    Matches the shape the API layer returns via
    ``CloudCostReport.period``.  Routes can pass it directly to
    ``model_dump()`` to build the response envelope.
    """

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass(frozen=True)
class ResolvedScope:
    """Canonical scope tuple: the selected registry rows + the
    canonical ``scope`` token used in the cache key.
    """

    rows: List[Any]
    scope: str  # "all" | "acct:<id>" | "sub:<guid>" | "<provider>:<id>"


def resolve_period(
    *,
    days: Optional[int],
    month: Optional[str],
    start_date: Optional[date],
    end_date: Optional[date],
) -> Tuple[Optional[ResolvedPeriod], Optional[ResolvedPeriod], str, str, Optional[JSONResponse]]:
    """Resolve a (current, previous) period pair from the canonical
    request parameters.  Returns ``(period, prev, cache_kind,
    cache_signature, error_response)``.  On invalid input the
    ``error_response`` is set and the caller should return it
    directly.
    """
    from fastapi import status as http_status

    if month is not None:
        try:
            today = datetime.now(timezone.utc).date()
            requested_year, requested_m = parse_month(month)
            is_current_month = (
                requested_year == today.year and requested_m == today.month
            )
            period_obj = build_month_period(month, today=today)
            current = ResolvedPeriod(start=period_obj.start, end=period_obj.end)
            if is_current_month:
                prev_obj = matched_mtd_previous_period(month, today=today)
            else:
                prev_obj = previous_month_period(month)
            prev = ResolvedPeriod(start=prev_obj.start, end=prev_obj.end)
            return (
                current,
                prev,
                "month",
                f"{month}|{current.days}",
                None,
            )
        except Exception as exc:
            return (
                None,
                None,
                "",
                "",
                JSONResponse(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
            )
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            return (
                None, None, "", "",
                JSONResponse(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": "InvalidDates",
                        "message": "start_date and end_date must be supplied together",
                    },
                ),
            )
        try:
            period_obj = build_explicit_period(start_date, end_date)
            current = ResolvedPeriod(start=period_obj.start, end=period_obj.end)
            prev_obj_start = start_date - timedelta(days=current.days)
            prev = ResolvedPeriod(start=prev_obj_start, end=start_date)
            return (
                current,
                prev,
                "explicit",
                f"{start_date.isoformat()}:{end_date.isoformat()}",
                None,
            )
        except Exception as exc:
            return (
                None, None, "", "",
                JSONResponse(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": type(exc).__name__,
                        "message": str(exc),
                    },
                ),
            )
    effective_days = days if days is not None else 30
    if effective_days not in ALLOWED_LOOKBACK_DAYS:
        return (
            None, None, "", "",
            JSONResponse(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "status": "error",
                    "error_code": "InvalidLookbackDays",
                    "message": (
                        f"days={effective_days!r} is not allowed; "
                        f"allowed values are {sorted(ALLOWED_LOOKBACK_DAYS)}"
                    ),
                },
            ),
        )
    period_obj = build_period(effective_days)
    current = ResolvedPeriod(start=period_obj.start, end=period_obj.end)
    prev_obj = build_period(effective_days)  # caller computes prev window below
    # For days=N windows the route uses ``previous_period`` from
    # ``CostExplorer``; we mirror that by shifting by N days.
    from app.services.aws.cost_explorer import previous_period as _ce_previous_period
    prev_pe = _ce_previous_period(period_obj)
    prev = ResolvedPeriod(start=prev_pe.start, end=prev_pe.end)
    return (current, prev, "days", str(effective_days), None)


def resolve_rows(
    db: Session,
    *,
    provider: CloudProviderKind,
    selection: Optional[str],
) -> Tuple[Optional[List[Any]], str, Optional[JSONResponse]]:
    """Translate a scope selector into the canonical rows + scope
    token.  Mirrors ``app.api.cloud_costs._resolve_rows`` but returns
    plain Python objects (not the FastAPI ``Depends`` machinery).
    """
    from fastapi import status as http_status
    from sqlalchemy import select
    from app.db.models import CloudAccount as _CloudAccountRow

    selection = (selection or "all").strip() or "all"
    if selection == "all":
        rows = list(iter_enabled_accounts(db, provider=provider.value))
        if not rows and provider == CloudProviderKind.AWS:
            mgmt = get_management_account(db)
            if mgmt is not None:
                rows = [mgmt]
        if not rows:
            return None, "all", JSONResponse(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "error_code": "NoAccountsRegistered",
                    "message": (
                        f"no {provider.value} accounts are registered"
                    ),
                },
            )
        return rows, "all", None

    cleaned = selection
    if provider == CloudProviderKind.AZURE and cleaned.startswith("sub:"):
        cleaned = cleaned[len("sub:"):]
    elif provider == CloudProviderKind.AWS and cleaned.startswith("acct:"):
        cleaned = cleaned[len("acct:"):]
    row = None
    if cleaned.isdigit():
        try:
            from app.services.aws.account_provider import (
                resolve_account_or_404,
            )
            row = resolve_account_or_404(db, cleaned)
        except Exception:
            row = None
    if row is None:
        try:
            row = db.execute(
                select(_CloudAccountRow).where(
                    _CloudAccountRow.provider == provider.value,
                    _CloudAccountRow.account_id == cleaned,
                    _CloudAccountRow.enabled.is_(True),
                )
            ).scalar_one_or_none()
        except Exception:
            row = None
    if row is None:
        return None, "single", JSONResponse(
            status_code=http_status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "AccountNotFound",
                "message": (
                    f"no {provider.value} account matches selection={selection!r}"
                ),
            },
        )
    if not row.enabled:
        return None, "single", JSONResponse(
            status_code=http_status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "AccountDisabled",
                "message": f"account {row.account_id} is disabled",
            },
        )
    if provider == CloudProviderKind.AZURE:
        scope = f"sub:{row.account_id}"
    else:
        scope = f"acct:{row.account_id}"
    return [row], scope, None


__all__ = ["ResolvedPeriod", "ResolvedScope", "resolve_period", "resolve_rows"]
