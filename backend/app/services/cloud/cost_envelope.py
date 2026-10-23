"""Phase 7D.4 — canonical cost envelope builders.

Extracted from ``app.api.cloud_costs`` so both the FastAPI
route layer AND the multi-cloud AI FinOps context builder can
assemble the canonical ``CloudCostReport`` envelope from the
same code path.  No HTTP / FastAPI dependency — these are pure
transformation functions of provider-summary dataclasses.

The route layer (``app.api.cloud_costs``) now imports these
helpers instead of carrying its own copy.  Both call paths
share identical envelope shape, so the AI grounding context
is bit-identical to what the UI consumes.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any, List, Optional

from app.schemas.cost import (
    CloudCostAccountError,
    CloudCostAccountShare,
    CloudCostReport,
    CostPeriod,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)


def _account_id_value(rows: Optional[List[Any]], scope: str) -> Optional[str]:
    if rows and len(rows) == 1:
        return rows[0].account_id
    return None


def _display_name(rows: Optional[List[Any]], scope: str) -> Optional[str]:
    if rows and len(rows) == 1:
        return rows[0].display_name
    return None


def _previous_period_bounds(period_start: date, period_end: date):
    days = (period_end - period_start).days
    if days <= 0:
        return (period_start, period_start)
    return (
        date.fromordinal(period_start.toordinal() - days),
        period_start,
    )


def _daily_date_for_aws(item) -> date:
    ext = item.extensions or {}
    raw = ext.get("date")
    if raw is not None:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            pass
    try:
        return date.fromisoformat(str(item.service))
    except ValueError:
        return date.today()


def build_aws_envelope(
    summary,
    *,
    rows: List[Any],
    scope: str,
    daily_trend: Optional[List[Any]] = None,
) -> CloudCostReport:
    """Translate an AWS adapter summary into the canonical envelope.

    Pure function of the AWS summary + rows + (optional) daily
    trend; no side effects.  Used by both the canonical route
    layer and the AI FinOps context builder so the AI grounding
    context is byte-identical to what the UI consumes.
    """
    from decimal import Decimal

    account_id = _account_id_value(rows, scope)
    display_name = _display_name(rows, scope)
    period = CostPeriod(
        start=summary.period_start,
        end=summary.period_end,
        days=(summary.period_end - summary.period_start).days,
    )
    prev_start, prev_end = _previous_period_bounds(
        summary.period_start, summary.period_end
    )
    previous_period = CostPeriod(start=prev_start, end=prev_end, days=period.days)
    if summary.previous_total is not None:
        change_amount = summary.current_total - summary.previous_total
        if summary.previous_total == 0:
            change_percent = None
        else:
            change_percent = (change_amount / summary.previous_total).quantize(
                Decimal("0.01")
            )
    else:
        change_amount = None
        change_percent = None
    extensions = summary.extensions or {}
    by_account_extensions = extensions.get("by_account", []) or []
    by_account = [
        CloudCostAccountShare(
            account_id=item["account_id"],
            display_name=item["display_name"],
            amount=Decimal(str(item["amount"])),
            currency=summary.currency,
        )
        for item in by_account_extensions
    ]
    raw_errors = extensions.get("account_errors", []) or []
    account_errors = [
        CloudCostAccountError(
            account_id=item["account_id"],
            display_name=item["display_name"],
            code=item["code"],
            message=item["message"],
        )
        for item in raw_errors
    ]
    daily: List[DailyCostPoint] = []
    for item in daily_trend or []:
        try:
            daily.append(
                DailyCostPoint(
                    date=_daily_date_for_aws(item),
                    amount=item.amount,
                    unit=item.currency,
                )
            )
        except Exception:
            continue
    return CloudCostReport(
        provider="AWS",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        currency_status="OK",
        account_id=account_id,
        account_display_name=display_name,
        period=period,
        previous_period=previous_period,
        currency=summary.currency,
        total_cost=summary.current_total,
        previous_period_cost=summary.previous_total,
        change_amount=change_amount,
        change_percent=change_percent,
        estimated=summary.estimated,
        daily_trend=daily,
        by_service=[
            ServiceCost(service=item.service, amount=item.amount, unit=item.currency)
            for item in summary.by_service
        ],
        by_region=[
            RegionCost(region=item.service, amount=item.amount, unit=item.currency)
            for item in summary.by_region
        ],
        by_account=by_account,
        account_errors=account_errors,
        source="AWS_COST_EXPLORER",
        successful_accounts=1,
        failed_accounts=0,
        previous_period_unavailable=False,
    )


def build_azure_envelope(
    summary,
    *,
    rows: List[Any],
    scope: str,
    daily_trend: Optional[List[Any]] = None,
    daily_currency_status: str = "OK",
) -> CloudCostReport:
    """Translate an Azure adapter summary into the canonical envelope."""
    from decimal import Decimal
    from app.services.cloud.azure.cost_aggregator import build_trend_from_points

    account_id = _account_id_value(rows, scope)
    display_name = _display_name(rows, scope)
    extensions = summary.extensions or {}
    period = CostPeriod(
        start=summary.period_start,
        end=summary.period_end,
        days=(summary.period_end - summary.period_start).days,
    )
    prev_start, prev_end = _previous_period_bounds(
        summary.period_start, summary.period_end
    )
    previous_period = CostPeriod(start=prev_start, end=prev_end, days=period.days)
    if summary.previous_total is not None:
        change_amount = summary.current_total - summary.previous_total
        if summary.previous_total == 0:
            change_percent = None
        else:
            change_percent = (change_amount / summary.previous_total).quantize(
                Decimal("0.01")
            )
    else:
        change_amount = None
        change_percent = None
    by_account_extensions = extensions.get("by_subscription", []) or []
    by_account = [
        CloudCostAccountShare(
            account_id=item["subscription_id"],
            display_name=item["display_name"],
            amount=Decimal(str(item["amount"])),
            currency=item.get("currency") or summary.currency,
        )
        for item in by_account_extensions
    ]
    raw_errors = extensions.get("subscription_errors", []) or []
    account_errors = [
        CloudCostAccountError(
            account_id=item["subscription_id"],
            display_name=item["display_name"],
            code=item["code"],
            message=item["message"],
        )
        for item in raw_errors
    ]
    daily: List[DailyCostPoint] = []
    if daily_trend is not None:
        for item in daily_trend:
            ext = item.extensions or {}
            raw = ext.get("date")
            try:
                d = date.fromisoformat(str(raw)) if raw else date.fromisoformat(str(item.service))
            except ValueError:
                d = date.today()
            daily.append(
                DailyCostPoint(date=d, amount=item.amount, unit=item.currency)
            )
    currency_status = extensions.get("currency_status", "OK")
    if daily_currency_status == "MIXED_CURRENCY":
        currency_status = "MIXED_CURRENCY"
    subscribed_count = len(by_account)
    failed_count = len(account_errors)
    successful_count = max(0, subscribed_count - failed_count)
    return CloudCostReport(
        provider="AZURE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        currency_status=currency_status,
        account_id=account_id,
        account_display_name=display_name,
        period=period,
        previous_period=previous_period,
        currency=summary.currency,
        total_cost=summary.current_total,
        previous_period_cost=summary.previous_total,
        change_amount=change_amount,
        change_percent=change_percent,
        estimated=summary.estimated,
        daily_trend=daily,
        by_service=[
            ServiceCost(service=item.service, amount=item.amount, unit=item.currency)
            for item in summary.by_service
        ],
        by_region=[
            RegionCost(region=item.service, amount=item.amount, unit=item.currency)
            for item in summary.by_region
        ],
        by_account=by_account,
        account_errors=account_errors,
        source="AZURE_COST_MANAGEMENT",
        successful_accounts=successful_count,
        failed_accounts=failed_count,
        previous_period_unavailable=bool(
            extensions.get("previous_period_unavailable", False)
        ),
    )


def build_rackspace_envelope(
    summary,
    *,
    rows: List[Any],
    scope: str,
) -> CloudCostReport:
    """Translate a Rackspace adapter summary into the canonical envelope.

    Phase 9B.1 — Rackspace Billing V2 semantics:

    * ``period`` is the ACTUAL source billing cycle returned by the
      Rackspace ``estimated_charges`` endpoint (never the requested
      window).  The end date is exclusive — the live cycle
      ``2026-10-19`` → ``2026-11-19`` spans exactly one month (31
      days) — matching the canonical ``CostPeriod`` convention
      (``start`` inclusive, ``end`` exclusive).
    * ``previous_period_cost`` / ``change_amount`` /
      ``change_percent`` are ``None`` and
      ``previous_period_unavailable`` is ``True`` — historical
      totals are a documented Phase 9B follow-up and are NEVER
      fabricated.
    * ``daily_trend`` and ``by_region`` are empty — the source has
      no reliable per-day or per-region granularity.
    * ``estimated`` is ``True`` — the source is the current-cycle
      estimated-charge snapshot.
    * A true ``$0`` total is a legitimate SUCCESS, not an
      unavailable state.
    """
    from decimal import Decimal

    account_id = _account_id_value(rows, scope)
    display_name = _display_name(rows, scope)
    extensions = summary.extensions or {}
    # The Rackspace billing-cycle end date is EXCLUSIVE (the live
    # cycle ``2026-10-19`` → ``2026-11-19`` spans exactly one month
    # = 31 days), which matches the canonical ``CostPeriod``
    # convention: ``start`` inclusive, ``end`` exclusive.
    period_days = (summary.period_end - summary.period_start).days
    period = CostPeriod(
        start=summary.period_start,
        end=summary.period_end,
        days=max(1, min(period_days, 365)),
    )
    prev_start, prev_end = _previous_period_bounds(
        summary.period_start, summary.period_end
    )
    previous_period = CostPeriod(
        start=prev_start,
        end=prev_end,
        days=period.days,
    )
    by_account_extensions = extensions.get("by_account", []) or []
    by_account = [
        CloudCostAccountShare(
            account_id=item["account_id"],
            display_name=item["display_name"],
            amount=Decimal(str(item["amount"])),
            currency=summary.currency,
        )
        for item in by_account_extensions
    ]
    account_errors = []  # failures surface as typed errors, not rows
    successful_count = len(by_account)
    failed_count = 0
    warnings = list(extensions.get("warnings", []) or [])
    if extensions.get("period_adjusted"):
        warnings.append(
            "period_adjusted_to_source_billing_cycle"
        )
    return CloudCostReport(
        provider="RACKSPACE",
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=extensions.get("status", "SUCCESS"),
        currency_status=extensions.get("currency_status", "OK"),
        account_id=account_id,
        account_display_name=display_name,
        period=period,
        previous_period=previous_period,
        currency=summary.currency,
        total_cost=summary.current_total,
        previous_period_cost=None,
        change_amount=None,
        change_percent=None,
        estimated=summary.estimated,
        daily_trend=[],
        by_service=[
            ServiceCost(service=item.service, amount=item.amount, unit=item.currency)
            for item in summary.by_service
        ],
        by_region=[],
        by_account=by_account,
        account_errors=account_errors,
        source="RACKSPACE_BILLING",
        successful_accounts=successful_count,
        failed_accounts=failed_count,
        previous_period_unavailable=True,
    )


__all__ = [
    "build_aws_envelope",
    "build_azure_envelope",
    "build_rackspace_envelope",
]


