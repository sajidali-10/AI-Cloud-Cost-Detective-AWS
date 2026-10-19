"""Provider-neutral Cloud cost API — Phase 7C.2B.

Canonical cost surface for every registered provider.  Lives on
``/api/cloud/costs`` so the front-end can hit a single endpoint
regardless of provider.

Architectural notes
-------------------

* AWS keeps its existing :mod:`app.api.aws_costs` route on
  ``/api/aws/costs`` for backwards-compatibility with every
  Phase 7A / 7B / 7C.1 caller.  The canonical provider-neutral
  route delegates to the AWS adapter for ``provider='AWS'`` so the
  same normalised shape is returned, but the AWS legacy route
  remains untouched.
* Azure cost data is fetched by the Azure adapter, which delegates
  to :mod:`app.services.cloud.azure.cost_aggregator`.  No raw Azure
  payloads ever leak past the adapter layer.
* Cache isolation is enforced inside :func:`_build_cache_key`
  below.  The key includes ``user``, ``provider``, ``scope``,
  ``region``, ``kind``, and ``signature`` so AWS and Azure cannot
  collide, and so two different subscriptions cannot collide when
  the same account id format happens to overlap.

Selection semantics
-------------------

* ``provider`` — required, one of ``AWS`` / ``AZURE`` (future
  ``RACKSPACE``).
* ``account_id`` (optional) — ``"all"`` (default), ``"acct:<id>"``
  for AWS, ``"sub:<id>"`` for Azure, or a bare numeric row id.

Period resolution mirrors the AWS route: ``month=YYYY-MM`` first,
then ``start_date`` + ``end_date``, then ``days``.

Error mapping
-------------

Distinct from the AWS route: Azure responses are mapped onto a
provider-specific envelope so the front-end can distinguish

* ``NO_DATA`` — Cost Management returned 204.
* ``MIXED_CURRENCY`` — different subscription currencies.
* ``UPSTREAM_ERROR`` — Cost Management returned 5xx.
* ``AUTH_FAILED`` / ``ACCESS_DENIED`` / ``NOT_FOUND`` / ``THROTTLED``
  — mapped to the corresponding HTTP status with a sanitised
  error_code so the caller can render an appropriate UI state.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cost import (
    CacheStatus,
    CloudCostAccountError,
    CloudCostAccountShare,
    CloudCostReport,
    CloudCostReportResponse,
    CostPeriod,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
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
from app.services.aws.account_provider import (
    iter_enabled_accounts,
    get_management_account,
    resolve_account_or_404,
)
from app.services.cloud import (
    CloudProviderKind,
    ProviderNotFound,
    ProviderRegistry,
)
from app.services.cloud.azure.costs import (
    AzureCostError,
    AzureCostErrorCategory,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.cloud_costs")

_settings = get_settings


# Standalone router with the ``/cloud`` prefix so the public URL is
# ``/api/cloud/costs`` (after Nginx strips ``/api/``).  Mirrors the
# pattern established by :mod:`app.api.cloud_providers`.
router = APIRouter(prefix="/cloud", tags=["cloud"])


# ---------------------------------------------------------------------------
# Cache key — must include EVERY dimension that affects the response.
# Phase 7C.2B adds provider + subscription scope so AWS and Azure
# responses can never collide.
# ---------------------------------------------------------------------------


def _build_cache_key(
    *,
    user_id: int,
    provider: str,
    scope: str,
    region: str,
    kind: str,
    signature: str,
) -> str:
    raw = "|".join(
        ["v4", provider.upper(), scope, f"user={user_id}", region, kind, signature]
    )
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


# ---------------------------------------------------------------------------
# Period resolution (shared across providers)
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
# Selection / row resolution
# ---------------------------------------------------------------------------


def _resolve_rows(
    db: Session,
    *,
    provider: CloudProviderKind,
    selection: Optional[str],
) -> tuple[Optional[list], str, Optional[JSONResponse]]:
    """Translate ``selection`` into a concrete list of registry rows.

    Returns ``(rows, scope, error_response)``.  ``rows`` is ``None``
    on error; ``scope`` is one of ``"all"`` / ``"acct:<id>"`` /
    ``"sub:<id>"`` so the cache key + response envelope agree on the
    provider-agnostic shape.
    """
    selection = (selection or "all").strip() or "all"
    if selection == "all":
        rows = list(iter_enabled_accounts(db, provider=provider.value))
        if not rows and provider == CloudProviderKind.AWS:
            mgmt = get_management_account(db)
            if mgmt is not None:
                rows = [mgmt]
        if not rows:
            return None, "all", JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "error_code": "NoAccountsRegistered",
                    "message": (
                        f"no {provider.value} accounts are registered"
                    ),
                },
            )
        return rows, "all", None

    # Specific-scope path — translate the selector to a registry row.
    # Azure subscription IDs are GUIDs (non-digit), so the
    # AWS-only ``resolve_account_or_404`` cannot resolve them.
    # We handle both AWS-style ``acct:<id>`` (or bare numeric id)
    # and Azure-style ``sub:<guid>`` (or bare GUID) here.
    from sqlalchemy import select
    from app.db.models import CloudAccount as _CloudAccountRow

    cleaned = selection
    if provider == CloudProviderKind.AZURE and cleaned.startswith("sub:"):
        cleaned = cleaned[len("sub:"):]
    elif provider == CloudProviderKind.AWS and cleaned.startswith("acct:"):
        cleaned = cleaned[len("acct:"):]
    row = None
    if cleaned.isdigit():
        try:
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
            status_code=status.HTTP_404_NOT_FOUND,
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
            status_code=status.HTTP_409_CONFLICT,
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


def _account_id_value(rows: list, scope: str) -> Optional[str]:
    if len(rows) == 1:
        return rows[0].account_id
    return None


def _display_name(rows: list, scope: str) -> Optional[str]:
    if len(rows) == 1:
        return rows[0].display_name
    return None


def _previous_period_bounds(
    period_start: date, period_end: date
) -> tuple[date, date]:
    days = (period_end - period_start).days
    if days <= 0:
        return (period_start, period_start)
    return (
        date.fromordinal(period_start.toordinal() - days),
        period_start,
    )


def _daily_date_for_aws(item) -> date:
    """Best-effort conversion of an AWS daily CloudServiceCost to a date.

    The AWS adapter encodes daily points with ``service=str(day)``
    for backward compatibility with Phase 7B consumers.  We
    reconstruct the date so the cloud envelope stays consistent
    across providers.
    """
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


# ---------------------------------------------------------------------------
# Adapter dispatch — translate the normalised provider summary into
# the provider-neutral Cloud cost envelope.
# ---------------------------------------------------------------------------


def _build_aws_envelope(
    summary,
    *,
    rows: list,
    scope: str,
    daily_trend=None,
) -> CloudCostReport:
    """Translate AWS adapter output into the provider-neutral envelope."""
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
    # Phase 7D.3.3 (correctness) — ``previous_total`` is now
    # Optional.  The AWS upstream always returns a numeric previous
    # period (Cost Explorer's GetCostAndUsage always supports a
    # previous window), so the Optional branch is defensive only.
    if summary.previous_total is not None:
        change_amount: Optional[Decimal] = summary.current_total - summary.previous_total
        if summary.previous_total == 0:
            change_percent: Optional[Decimal] = None
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
    # Phase 7D.3.1 — surface the real per-day Cost Explorer points
    # returned by ``get_cost_trend``.  Falls back to an empty list
    # when the upstream has no daily data so the envelope stays
    # well-formed (the Dashboard renders the documented empty state
    # instead of fabricating values).
    daily: list[DailyCostPoint] = []
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
            # A single malformed point must NEVER 502 the envelope.
            # Drop it and continue; the partial-data banner surfaces
            # the gap at the page level.
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
    )


def _build_azure_envelope(
    summary,
    *,
    rows: list,
    scope: str,
    daily_trend=None,
    daily_currency_status: str = "OK",
) -> CloudCostReport:
    """Translate Azure adapter output into the provider-neutral envelope."""
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
    # Phase 7D.3.3 (correctness) — ``previous_total`` is now
    # Optional.  When the previous-period fetch failed, we MUST
    # NOT fabricate a numeric change / percentage.  Both
    # ``change_amount`` and ``change_percent`` are left as ``None``
    # and the envelope's ``previous_period_unavailable`` flag is
    # set so the front-end renders "—" / "Not available".
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
    # Phase 7D.3.3 (correctness) — forward the
    # ``previous_period_unavailable`` flag from the aggregator so
    # the front-end renders "—" / "Not available" when the
    # previous-period fetch failed entirely.
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
    daily = []
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
    # Currency safety: if either the summary or the daily trend
    # mixed currencies, surface the union.
    currency_status = extensions.get("currency_status", "OK")
    if daily_currency_status == "MIXED_CURRENCY":
        currency_status = "MIXED_CURRENCY"
    # Phase 7D.3.3 — surface per-subscription counts directly on
    # the envelope so the frontend does not need to derive them
    # from ``by_account`` / ``account_errors`` counts.
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
        # Phase 7D.3.3 (correctness) — surface whether the
        # previous-period fetch succeeded so the front-end renders
        # "—" / "Not available" instead of fabricating a percentage
        # against an invented zero baseline.
        previous_period_unavailable=bool(
            extensions.get("previous_period_unavailable", False)
        ),
    )


# ---------------------------------------------------------------------------
# Route — GET /api/cloud/costs
# ---------------------------------------------------------------------------


@router.get(
    "/costs",
    summary="Provider-neutral cost report for the selected provider and account context.",
)
async def get_cloud_costs(
    provider: str = Query(..., description="AWS | AZURE (Rackspace in a future phase)"),
    days: Optional[int] = Query(default=None),
    month: Optional[str] = Query(default=None),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    region: Optional[str] = Query(default=None),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Default 'all'.  For AWS: 'acct:<id>' or a registry row id.  "
            "For Azure: 'sub:<id>' or a registry row id."
        ),
    ),
    user: AppUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    provider = (provider or "").strip().upper()
    try:
        kind = CloudProviderKind(provider)
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "UnknownProvider",
                "message": f"unknown provider {provider!r}",
            },
        )
    registry = ProviderRegistry.current()
    try:
        registry.require(kind)
    except ProviderNotFound:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "ProviderNotRegistered",
                "message": f"provider {kind.value} is not registered in this deployment",
            },
        )

    period, prev, cache_kind, cache_signature, err_resp = _resolve_period(
        days=days,
        month=month,
        start_date=start_date,
        end_date=end_date,
    )
    if err_resp is not None:
        return err_resp

    rows, scope, rows_err = _resolve_rows(db, provider=kind, selection=account_id)
    if rows_err is not None:
        return rows_err

    settings = _settings()
    # The provider-neutral route does NOT inject a default region
    # for Azure (Azure Cost Management does not accept a region
    # filter at subscription scope today).  AWS still benefits
    # from the default so legacy behaviour is preserved.
    if kind == CloudProviderKind.AWS:
        effective_region = region or settings.aws_default_region
    else:
        effective_region = region or ""

    cache_key = _build_cache_key(
        user_id=int(user.id),
        provider=kind.value,
        scope=scope,
        region=effective_region,
        kind=cache_kind,
        signature=cache_signature,
    )
    # Use the cache key as a stable correlation id without
    # persisting it (the provider-neutral route does not write to
    # the cost_cache table — the AWS legacy route still does for
    # backwards compatibility).
    _ = cache_key

    adapter = registry.require(kind)

    async def _aws_envelope() -> CloudCostReport:
        summary = await adapter.get_cost_summary(
            accounts=rows,
            period_start=period.start,
            period_end=period.end,
            previous_period_start=prev.start,
            previous_period_end=prev.end,
            region=effective_region or None,
        )
        # A trend fetch failure MUST NOT 502 the whole envelope —
        # the summary still has value.  Fall back to an empty
        # trend and surface the failure in ``account_errors`` so
        # the dashboard-level partial banner picks it up.
        daily_trend: list = []
        try:
            trend = await adapter.get_cost_trend(
                accounts=rows,
                period_start=period.start,
                period_end=period.end,
                region=effective_region or None,
            )
            daily_trend = list(trend.daily or [])
        except Exception as trend_exc:  # noqa: BLE001 - sanitised
            logger.warning(
                "cloud.costs.daily_trend_failed provider=AWS code=%s",
                type(trend_exc).__name__,
            )
            extensions = dict(summary.extensions or {})
            raw_errors = list(extensions.get("account_errors", []) or [])
            raw_errors.append(
                {
                    "account_id": "aggregate",
                    "display_name": "All AWS Accounts",
                    "code": "DailyTrendUnavailable",
                    "message": f"daily trend unavailable: {type(trend_exc).__name__}",
                }
            )
            extensions["account_errors"] = raw_errors
            summary = replace(summary, extensions=extensions)
        return _build_aws_envelope(
            summary,
            rows=rows,
            scope=scope,
            daily_trend=daily_trend,
        )

    async def _azure_envelope_dict() -> dict:
        """Build a JSON-safe dict of the Azure canonical envelope.

        Returned as a dict (not a :class:`CloudCostReport`) so the
        Azure cache layer can serialise it directly into the
        ``cost_cache.payload`` JSONB column.  The route layer
        rehydrates the dict back to a ``CloudCostReport`` on
        cache HIT.
        """
        summary = await adapter.get_cost_summary(
            accounts=rows,
            period_start=period.start,
            period_end=period.end,
            previous_period_start=prev.start,
            previous_period_end=prev.end,
            region=effective_region or None,
        )
        extensions = dict(summary.extensions or {})
        cached_points = extensions.get("_daily_points") or []
        # The Azure adapter stashes the rolled-up daily points on
        # ``extensions``; build the trend dataclass from them so
        # we don't issue a second fan-out.
        from app.services.cloud.azure.cost_aggregator import (
            build_trend_from_points,
        )
        from decimal import Decimal as _Decimal
        daily = [
            (
                date.fromisoformat(p["date"]),
                _Decimal(str(p["amount"])),
                p["currency"],
            )
            for p in cached_points
        ]
        daily_currency_status = str(extensions.get("_daily_currency_status") or "OK")
        trend = build_trend_from_points(
            rows=list(rows),
            period_start=period.start,
            period_end=period.end,
            daily_points=daily,
        )
        envelope = _build_azure_envelope(
            summary,
            rows=rows,
            scope=scope,
            daily_trend=trend.daily,
            daily_currency_status=daily_currency_status,
        )
        # Phase 7D.3.3 — attach the per-subscription counts + the
        # last POST count to the envelope BEFORE serialisation so
        # the cache stores them and the route can re-expose them
        # on HIT / STALE.
        envelope_payload = envelope.model_dump(mode="json")
        envelope_payload["successful_accounts"] = envelope.successful_accounts
        envelope_payload["failed_accounts"] = envelope.failed_accounts
        envelope_payload["azure_post_count"] = int(
            extensions.get("_azure_post_count") or 0
        )
        return envelope_payload

    if kind == CloudProviderKind.AWS:
        # Phase 7D.3.3 — the AWS path keeps its Phase 2 read-through
        # cache (mounted on the AWS legacy route) and does NOT go
        # through the new Azure cache layer.  The canonical
        # provider-neutral route serves AWS via the same envelope
        # but does not write to cost_cache (preserves backwards
        # compatibility with Phase 7C.2B).
        try:
            envelope = await _aws_envelope()
        except AzureCostError as exc:
            return _azure_error_response(exc)
        except Exception as exc:  # noqa: BLE001 - sanitised
            logger.warning(
                "cloud.costs.upstream_failed provider=%s code=%s",
                kind.value, type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "status": "error",
                    "error_code": type(exc).__name__,
                    "message": "upstream cost provider failed",
                },
            )
        response = CloudCostReportResponse(
            report=envelope,
            cache_status=CacheStatus.REFRESHED,
            cached_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        )
        return response.model_dump(mode="json")

    # AZURE — route through the new cache layer.
    from app.services.cloud.azure.cost_cache import (
        AzureDataFreshness,
        get_or_refresh_azure_canonical,
    )

    # Reset the POST counter at the entry of the loader so the
    # value we persist reflects only this canonical request.
    from app.services.cloud.azure.costs import (
        get_and_reset_azure_post_count as _reset_post_count,
    )

    async def _azure_loader() -> dict:
        # Reset BEFORE the loader runs so the count includes only
        # this canonical request's POSTs.  The reset value is
        # harmless if the loader short-circuits; the route layer
        # reads the post-load value via the same helper.
        _reset_post_count()
        envelope_dict = await _azure_envelope_dict()
        # Read the post-load counter and attach it to the
        # envelope payload before it lands in the cache.
        from app.services.cloud.azure.costs import azure_post_count
        envelope_dict["azure_post_count"] = int(azure_post_count())
        return envelope_dict

    try:
        entry = await get_or_refresh_azure_canonical(
            scope=scope,
            region=effective_region or "",
            kind=cache_kind,
            signature=cache_signature,
            loader=_azure_loader,
        )
    except AzureCostError as exc:
        return _azure_error_response(exc)
    except Exception as exc:  # noqa: BLE001 - sanitised
        logger.warning(
            "cloud.costs.upstream_failed provider=AZURE code=%s",
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": type(exc).__name__,
                "message": "upstream cost provider failed",
            },
        )

    # Rehydrate the cached envelope dict into a CloudCostReport.
    try:
        envelope = CloudCostReport.model_validate(entry.payload)
    except Exception as exc:  # noqa: BLE001 - corrupted cache row
        logger.warning(
            "cloud.costs.cache.corrupt_payload key=%s code=%s",
            entry.cache_key, type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": "CorruptCachePayload",
                "message": "cached envelope failed validation; please retry",
            },
        )

    # Translate the cache layer's enum onto the schema enum.
    cache_status_mapping = {
        AzureDataFreshness.LIVE: CacheStatus.REFRESHED,
        AzureDataFreshness.CACHED: CacheStatus.HIT,
        AzureDataFreshness.STALE: CacheStatus.STALE,
        AzureDataFreshness.UNAVAILABLE: CacheStatus.MISS,
    }
    cache_status = cache_status_mapping.get(entry.data_freshness, CacheStatus.REFRESHED)
    data_freshness_value = entry.data_freshness.value

    # Compute cache expiry hint based on the freshness window.
    if entry.data_freshness == AzureDataFreshness.STALE:
        expires_at = entry.last_successful_at or datetime.now(timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)

    response = CloudCostReportResponse(
        report=envelope,
        cache_status=cache_status,
        cached_at=entry.last_successful_at,
        expires_at=expires_at,
        data_freshness=data_freshness_value,
        last_successful_at=entry.last_successful_at,
    )
    return response.model_dump(mode="json")


def _azure_error_response(exc: AzureCostError) -> JSONResponse:
    """Translate an :class:`AzureCostError` into a sanitised HTTP envelope."""
    mapping = {
        AzureCostErrorCategory.AUTH_FAILED: (status.HTTP_401_UNAUTHORIZED, "AUTH_FAILED"),
        AzureCostErrorCategory.ACCESS_DENIED: (status.HTTP_403_FORBIDDEN, "ACCESS_DENIED"),
        AzureCostErrorCategory.NOT_FOUND: (status.HTTP_404_NOT_FOUND, "NOT_FOUND"),
        AzureCostErrorCategory.THROTTLED: (status.HTTP_429_TOO_MANY_REQUESTS, "THROTTLED"),
        AzureCostErrorCategory.UPSTREAM_ERROR: (status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"),
        AzureCostErrorCategory.NETWORK_ERROR: (status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"),
        AzureCostErrorCategory.INVALID_RESPONSE: (status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR"),
    }
    http_status, public_code = mapping.get(
        exc.category, (status.HTTP_502_BAD_GATEWAY, "UPSTREAM_ERROR")
    )
    headers: dict = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(int(exc.retry_after))
    return JSONResponse(
        status_code=http_status,
        content={
            "status": "error",
            "error_code": public_code,
            "category": exc.category.value,
            "status_code": exc.status_code,
        },
        headers=headers,
    )


__all__ = ["get_cloud_costs"]
