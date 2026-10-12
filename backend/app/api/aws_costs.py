"""FastAPI routes for Cost Explorer — Phase 2 + Phase 7B.

Mounted on the existing :data:`router` from :mod:`app.api.aws` so the
public URL stays consistent with Phase 1 (``/api/aws/costs``).

The endpoint accepts an ``account_id`` query parameter:

* absent / ``"all"`` — query every enabled account, sum the totals,
  return the multi-account aggregate.
* ``"acct:<id>"`` or a registry row id — query that account only.

Period resolution order (Phase 7A):

1. ``month=YYYY-MM`` — calendar month, MTD-clipped if current.
2. ``start_date`` + ``end_date`` — explicit UTC range.
3. ``days`` — legacy rolling lookback (back-compat).
4. ``days`` defaults to 30 if none of the above are supplied.

Days outside the allowed set yield a sanitized HTTP 422 (FastAPI's
default Pydantic validation envelope) so the caller never sees a
Boto3 stack trace.

Cache key shape:

* scope token ``acct:<id>`` or ``all``
* user id (via ``get_current_user``) — preserves Phase 7A isolation
* period signature (days / month / explicit range)
* region
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.db.models import AppUser, CloudAccount
from app.db.session import get_db
from app.schemas.cost import (
    AccountCostError,
    AccountCostShare,
    CacheStatus,
    CostPeriod,
    CostReport,
    CostReportResponse,
    DailyCostPoint,
    MultiAccountCostReport,
    MultiAccountCostReportResponse,
    RegionCost,
    ServiceCost,
)
from app.services.aws.account_provider import (
    AccountContext,
    iter_enabled_accounts,
    get_management_account,
    list_accounts,
    resolve_account_or_404,
)
from app.services.aws.cost_aggregator import (
    aggregate_cost_report_for_context,
)
from app.services.aws.cost_explorer import (
    ALLOWED_LOOKBACK_DAYS,
    CostExplorerError,
    build_explicit_period,
    build_month_period,
    build_period,
    matched_mtd_previous_period,
    parse_month as parse_month_unvalidated,
    previous_month_period,
    previous_period,
)
from app.services.cost_cache import get_or_refresh
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.aws_costs")

_settings = get_settings


# ---------------------------------------------------------------------------
# Cache key — must include EVERY dimension that affects the response.
# Phase 7B: the account-selection scope (``acct:<id>`` vs ``all``)
# is the leading dimension so Account-A data never satisfies an
# Account-B request, and vice-versa.
# ---------------------------------------------------------------------------


def _build_cache_key(
    *,
    user_id: int,
    scope: str,
    region: str,
    kind: str,
    signature: str,
) -> str:
    raw = "|".join(["v3", scope, f"user={user_id}", region, kind, signature])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


# ---------------------------------------------------------------------------
# Period resolution (shared between single-account and aggregate paths)
# ---------------------------------------------------------------------------


def _resolve_period(
    *,
    days: Optional[int],
    month: Optional[str],
    start_date: Optional[date],
    end_date: Optional[date],
) -> tuple[Any, Any, str, str, Optional[JSONResponse]]:
    """Return (period, prev, cache_kind, cache_signature, error_response)."""
    if month is not None:
        try:
            today = datetime.now(timezone.utc).date()
            requested_year, requested_m = parse_month_unvalidated(month)
            is_current_month = (
                requested_year == today.year and requested_m == today.month
            )
            period = build_month_period(month, today=today)
            if is_current_month:
                prev = matched_mtd_previous_period(month, today=today)
            else:
                prev = previous_month_period(month)
            return (
                period,
                prev,
                "month",
                f"{month}|{period.days}",
                None,
            )
        except CostExplorerError as exc:
            return (
                None,
                None,
                "",
                "",
                JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": exc.code,
                        "message": exc.message,
                    },
                ),
            )
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            return (
                None,
                None,
                "",
                "",
                JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": "InvalidDates",
                        "message": "start_date and end_date must be supplied together",
                    },
                ),
            )
        try:
            period = build_explicit_period(start_date, end_date)
            prev = CostPeriod(
                start=period.start - timedelta(days=period.days),
                end=period.start,
                days=period.days,
            )
            return (
                period,
                prev,
                "explicit",
                f"{period.start.isoformat()}:{period.end.isoformat()}",
                None,
            )
        except CostExplorerError as exc:
            return (
                None,
                None,
                "",
                "",
                JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "status": "error",
                        "error_code": exc.code,
                        "message": exc.message,
                    },
                ),
            )
    effective_days = days if days is not None else 30
    if effective_days not in ALLOWED_LOOKBACK_DAYS:
        return (
            None,
            None,
            "",
            "",
            JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
    period = build_period(effective_days)
    prev = previous_period(period)
    return period, prev, "days", str(effective_days), None


# ---------------------------------------------------------------------------
# Helpers — convert between dataclasses and Pydantic
# ---------------------------------------------------------------------------


def _pydantic_from_multi(report, *, scope: str) -> MultiAccountCostReport:
    change = report.total_cost - report.previous_period_cost
    if report.previous_period_cost == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change / report.previous_period_cost).quantize(
            Decimal("0.01")
        )
    by_account: list[AccountCostShare] = [
        AccountCostShare(
            account_id=aid, display_name=name, amount=amt
        )
        for aid, name, amt in report.by_account
    ]
    errors: list[AccountCostError] = [
        AccountCostError(
            account_id=e.account_id,
            display_name=e.display_name,
            code=e.code,
            message=e.message,
        )
        for e in report.account_errors
    ]
    return MultiAccountCostReport(
        scope=scope,
        status=report.status,
        account_id=None if scope == "all" else scope.split(":", 1)[-1],
        account_display_name=None,
        period=CostPeriod(
            start=report.period.start, end=report.period.end, days=report.period.days
        ),
        previous_period=CostPeriod(
            start=report.previous.start,
            end=report.previous.end,
            days=report.previous.days,
        ),
        currency=report.currency,
        total_cost=report.total_cost,
        previous_period_cost=report.previous_period_cost,
        change_amount=change,
        change_percent=change_percent,
        estimated=False,
        daily_trend=[
            DailyCostPoint(date=day, amount=amt, unit=unit)
            for day, amt, unit in report.daily
        ],
        by_service=[
            ServiceCost(service=name, amount=amt, unit=unit)
            for name, amt, unit in report.by_service
        ],
        by_region=[
            RegionCost(region=name, amount=amt, unit=unit)
            for name, amt, unit in report.by_region
        ],
        by_account=by_account,
        account_errors=errors,
        source="AWS_COST_EXPLORER",
    )


def _pydantic_from_single(raw, *, account_id: str, account_display_name: Optional[str]) -> CostReport:
    period = CostPeriod(
        start=raw.period.start, end=raw.period.end, days=raw.period.days
    )
    previous = CostPeriod(
        start=raw.previous.start, end=raw.previous.end, days=raw.previous.days
    )
    change_amount = raw.current_total - raw.previous_total
    if raw.previous_total == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change_amount / raw.previous_total).quantize(
            Decimal("0.01")
        )
    return CostReport(
        account_id=account_id,
        period=period,
        previous_period=previous,
        currency=raw.unit,
        total_cost=raw.current_total,
        previous_period_cost=raw.previous_total,
        change_amount=change_amount,
        change_percent=change_percent,
        estimated=raw.estimated,
        daily_trend=[
            DailyCostPoint(date=day, amount=amount, unit=unit)
            for (day, amount, unit) in raw.daily
        ],
        by_service=[
            ServiceCost(service=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_service
        ],
        by_region=[
            RegionCost(region=name, amount=amount, unit=unit)
            for (name, amount, unit) in raw.by_region
        ],
        source="AWS_COST_EXPLORER",
    )


# ---------------------------------------------------------------------------
# Route — GET /api/aws/costs
# ---------------------------------------------------------------------------


@aws_router.get(
    "/costs",
    summary="AWS Cost Explorer spend for the selected period and account context",
)
def get_aws_costs(
    request: Request,
    days: Optional[int] = Query(default=None),
    month: Optional[str] = Query(default=None),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    region: Optional[str] = Query(default=None),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all'."
        ),
    ),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Return the Cost Explorer report for the requested period.

    Selection:
    * ``account_id='all'`` (default) — every enabled account.
    * ``account_id='acct:<id>'`` or a registry row id — that account.

    The result is wrapped in either ``MultiAccountCostReport``
    (selection='all') or ``CostReport`` (single-account) so the
    single-account behaviour of Phase 7A is preserved.
    """
    settings = _settings()
    effective_region = region or settings.aws_default_region

    period, prev, cache_kind, cache_signature, err_resp = _resolve_period(
        days=days,
        month=month,
        start_date=start_date,
        end_date=end_date,
    )
    if err_resp is not None:
        return err_resp

    selection = (account_id or "all").strip()
    if selection == "":
        selection = "all"

    # --- Aggregate (all accounts) path ---
    if selection == "all":
        rows = list(iter_enabled_accounts(db))
        if not rows:
            mgmt = get_management_account(db)
            if mgmt is None:
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "status": "error",
                        "error_code": "NoAccountsRegistered",
                        "message": "no AWS accounts are registered",
                    },
                )
            rows = [mgmt]
        scope_token = "all"
        cache_key = _build_cache_key(
            user_id=int(user.id),
            scope=scope_token,
            region=effective_region,
            kind=cache_kind,
            signature=cache_signature,
        )
        cache_account_id = (
            get_management_account(db).account_id if get_management_account(db) else "all"
        )

        async def _loader() -> MultiAccountCostReport:
            agg = await aggregate_cost_report_for_context(
                rows=rows,
                period=period,
                previous=prev,
                region=effective_region,
            )
            return _pydantic_from_multi(agg, scope=scope_token)

        try:
            response = asyncio.run(get_or_refresh(
                account_id=cache_account_id,
                cache_key=cache_key,
                loader=_loader,
                scope=scope_token,
                model_cls=MultiAccountCostReport,
            ))
        except CostExplorerError as exc:
            return _cost_error_502(exc.code, effective_region)
        except (ClientError, BotoCoreError) as exc:
            return _cost_error_502(type(exc).__name__, effective_region)
        return response.model_dump(mode="json")

    # --- Specific-account path ---
    row = resolve_account_or_404(db, selection)
    if row is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "AccountNotFound",
                "message": f"no AWS account matches selection={selection!r}",
            },
        )
    if not row.enabled:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "AccountDisabled",
                "message": f"account {row.account_id} is disabled",
            },
        )
    scope_token = f"acct:{row.account_id}"
    cache_key = _build_cache_key(
        user_id=int(user.id),
        scope=scope_token,
        region=effective_region,
        kind=cache_kind,
        signature=cache_signature,
    )
    from app.services.aws.cost_explorer import (
        aggregate_cost_report_for_period,
        get_cost_explorer_client,
    )

    ce_client = get_cost_explorer_client(
        region=effective_region,
        account_row=row,
    )

    async def _loader() -> CostReport:
        def _build():
            return aggregate_cost_report_for_period(
                ce_client, period, prev, account_id=row.account_id, region=region
            )
        raw = await asyncio.to_thread(_build)
        return _pydantic_from_single(
            raw,
            account_id=row.account_id,
            account_display_name=row.display_name,
        )

    try:
        response = asyncio.run(get_or_refresh(
            account_id=row.account_id,
            cache_key=cache_key,
            loader=_loader,
            scope=scope_token,
        ))
    except CostExplorerError as exc:
        return _cost_error_502(exc.code, effective_region)
    except (ClientError, BotoCoreError) as exc:
        return _cost_error_502(type(exc).__name__, effective_region)
    return response.model_dump(mode="json")


def _cost_error_502(code: str, region: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "status": "error",
            "error_code": code,
            "region": region,
        },
    )


__all__ = ["get_aws_costs"]
