"""Phase 7D.4 — Multi-cloud FinOps context builder.

Assembles a deterministic, bounded, prompt-safe ``FinOpsContext``
from canonical Phase 7D application services.  The builder:

* Reuses the existing ``CloudProviderRegistry`` + adapter
  contracts (no new canonical logic).
* Calls the same envelope-building helpers that the
  ``/api/cloud/{costs,resources,recommendations}`` route layers
  use, so AI grounding is bit-identical to what the UI sees.
* Falls back to per-source ``None`` when an individual sub-source
  fails, isolating errors and surfacing them in ``warnings``.
* Preserves all existing semantics: ``LIVE`` / ``CACHED`` /
  ``STALE`` / ``PARTIAL`` / ``UNAVAILABLE``; ``None`` financial
  values stay ``None`` (no fabrication of zero).

The builder never talks to AWS / Azure directly.  All upstream
work is delegated to the registered provider adapter.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, List, Optional, Sequence, Tuple

from app.db.models import AppUser
from app.schemas.cloud_ai import (
    FinOpsAIProvider,
    FinOpsAIFreshness,
    FinOpsAIPeriod,
)

logger = logging.getLogger("cost-detective-backend.cloud_ai.context")

DEFAULT_TOP_N_SERVICES = 10
DEFAULT_TOP_N_REGIONS = 8
DEFAULT_TOP_N_RECOMMENDATIONS = 10
DEFAULT_LOOKBACK_DAYS = 30


class FinOpsContextStatus(str, Enum):
    LIVE = "LIVE"
    CACHED = "CACHED"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Bounded, deterministic view-model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinOpsCostSlice:
    currency: str
    total_cost: Optional[Decimal]
    previous_period_cost: Optional[Decimal]
    change_amount: Optional[Decimal]
    change_percent: Optional[Decimal]
    previous_period_unavailable: bool
    daily_trend: List[Dict[str, Any]]
    top_services: List[Dict[str, Any]]
    top_regions: List[Dict[str, Any]]
    by_account: List[Dict[str, Any]]
    account_errors: List[Dict[str, Any]]
    successful_accounts: int
    failed_accounts: int
    status: str


@dataclass(frozen=True)
class FinOpsResourceSlice:
    total: int
    by_type: List[Dict[str, Any]]
    by_region: List[Dict[str, Any]]
    by_subscription: List[Dict[str, Any]]
    status: str


@dataclass(frozen=True)
class FinOpsRecommendationSlice:
    count: int
    estimated_savings: Optional[Decimal]
    currency: Optional[str]
    savings_period: Optional[str]
    top_items: List[Dict[str, Any]]
    sources: List[str]


@dataclass(frozen=True)
class FinOpsContext:
    provider: FinOpsAIProvider
    scope: str
    account_display_name: Optional[str]
    period: FinOpsAIPeriod
    data_freshness: FinOpsAIFreshness
    status: FinOpsContextStatus
    cost: Optional[FinOpsCostSlice]
    resources: Optional[FinOpsResourceSlice]
    recommendations: Optional[FinOpsRecommendationSlice]
    partial_sources: List[str] = field(default_factory=list)
    unavailable_sources: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class FinOpsContextBuilder:
    """Provider-aware canonical context builder."""

    def __init__(
        self,
        *,
        top_n_services: int = DEFAULT_TOP_N_SERVICES,
        top_n_regions: int = DEFAULT_TOP_N_REGIONS,
        top_n_recommendations: int = DEFAULT_TOP_N_RECOMMENDATIONS,
    ) -> None:
        self.top_n_services = top_n_services
        self.top_n_regions = top_n_regions
        self.top_n_recommendations = top_n_recommendations

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def build(
        self,
        *,
        provider: FinOpsAIProvider,
        scope: str,
        user: AppUser,
        db,
        days: Optional[int] = None,
        month: Optional[str] = None,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        region: Optional[str] = None,
    ) -> FinOpsContext:
        from app.services.cloud.models import CloudProviderKind
        from app.services.cloud.period_resolver import (
            resolve_period as _resolve_period,
            resolve_rows as _resolve_rows,
        )
        from app.services.cloud.registry import ProviderRegistry

        period, prev, _, _, err_resp = _resolve_period(
            days=days,
            month=month,
            start_date=start_date,
            end_date=end_date,
        )
        warnings: List[str] = []
        # Period resolution failure (invalid month/days/explicit
        # combo) — return an unavailable context but DON'T raise.
        if err_resp is not None:
            warnings.append("Invalid period requested; AI answer will not include cost data.")
            today = date.today()
            fallback_period = self._derive_period_from_request(
                today=today, days=days, month=month, start_date=start_date, end_date=end_date
            )
            return FinOpsContext(
                provider=provider,
                scope=scope,
                account_display_name=None,
                period=fallback_period,
                data_freshness=FinOpsAIFreshness.UNAVAILABLE,
                status=FinOpsContextStatus.UNAVAILABLE,
                cost=None,
                resources=None,
                recommendations=None,
                partial_sources=[],
                unavailable_sources=["cost", "resources", "recommendations"],
                warnings=warnings,
            )

        try:
            kind = CloudProviderKind(provider.value)
        except ValueError as exc:
            warnings.append(f"Unknown provider {provider.value!r}; AI answer is unavailable.")
            return FinOpsContext(
                provider=provider,
                scope=scope,
                account_display_name=None,
                period=self._derive_period_from_request(
                    date.today(), days=days, month=month, start_date=start_date, end_date=end_date
                ),
                data_freshness=FinOpsAIFreshness.UNAVAILABLE,
                status=FinOpsContextStatus.UNAVAILABLE,
                cost=None,
                resources=None,
                recommendations=None,
                unavailable_sources=["cost", "resources", "recommendations"],
                warnings=warnings,
            )

        try:
            rows, canonical_scope, _ = _resolve_rows(db, provider=kind, selection=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloud_ai.context.rows_failed provider=%s scope=%s code=%s",
                provider.value, scope, type(exc).__name__,
            )
            warnings.append("Scope resolution failed; AI answer is unavailable.")
            return FinOpsContext(
                provider=provider,
                scope=scope,
                account_display_name=None,
                period=self._derive_period_from_request(
                    date.today(), days=days, month=month, start_date=start_date, end_date=end_date
                ),
                data_freshness=FinOpsAIFreshness.UNAVAILABLE,
                status=FinOpsContextStatus.UNAVAILABLE,
                cost=None,
                resources=None,
                recommendations=None,
                unavailable_sources=["cost", "resources", "recommendations"],
                warnings=warnings,
            )

        registry = ProviderRegistry.current()
        try:
            adapter = registry.require(kind)
        except Exception:  # noqa: BLE001
            adapter = None

        if kind == CloudProviderKind.AWS:
            effective_region = region or ""
            from app.core.config import get_settings
            effective_region = region or get_settings().aws_default_region
        else:
            effective_region = region or ""

        cost = await self._safe_cost(
            adapter=adapter, kind=kind, rows=rows,
            period_start=period.start, period_end=period.end,
            prev_start=prev.start, prev_end=prev.end,
            effective_region=effective_region, scope=canonical_scope,
        )
        resources = await self._safe_resources(
            adapter=adapter, kind=kind, rows=rows, scope=canonical_scope,
        )
        recommendations = await self._safe_recommendations(
            adapter=adapter, kind=kind, rows=rows, scope=canonical_scope,
        )

        partial, unavailable = self._classify_availability(cost, resources, recommendations, warnings)
        period_obj = self._period_from_inputs(period.start, period.end)
        freshness = self._resolve_freshness(cost, resources, recommendations)
        status = self._resolve_status(partial, unavailable, freshness)
        display_name = self._display_name(cost, canonical_scope)
        return FinOpsContext(
            provider=provider,
            scope=canonical_scope,
            account_display_name=display_name,
            period=period_obj,
            data_freshness=freshness,
            status=status,
            cost=cost,
            resources=resources,
            recommendations=recommendations,
            partial_sources=partial,
            unavailable_sources=unavailable,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Sub-source fetchers
    # ------------------------------------------------------------------

    async def _safe_cost(
        self, *, adapter, kind, rows, period_start, period_end,
        prev_start, prev_end, effective_region, scope,
    ) -> Optional[FinOpsCostSlice]:
        from app.services.cloud.azure.cost_aggregator import build_trend_from_points
        from app.services.cloud.cost_envelope import (
            build_aws_envelope,
            build_azure_envelope,
        )

        if adapter is None:
            return None
        from app.services.cloud.models import CloudProviderKind  # lazy — mirrors build()

        if kind == CloudProviderKind.RACKSPACE:
            # Phase 9B.1 — Rackspace AI FinOps is explicitly out of
            # scope.  The ``FinOpsAIProvider`` schema enum already
            # refuses RACKSPACE upstream; this guard keeps the cost
            # slice from ever mis-routing through the Azure envelope
            # branch if that enum grows a RACKSPACE member later
            # without a matching context builder.
            logger.info(
                "cloud_ai.context.cost_skipped reason=rackspace_out_of_scope_9b1"
            )
            return None
        try:
            summary = await adapter.get_cost_summary(
                accounts=rows,
                period_start=period_start,
                period_end=period_end,
                previous_period_start=prev_start,
                previous_period_end=prev_end,
                region=effective_region or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloud_ai.context.cost_failed provider=%s scope=%s code=%s",
                kind.value, scope, type(exc).__name__,
            )
            return None

        # Build the canonical envelope so the AI context matches
        # what the UI consumes bit-for-bit.
        try:
            if kind.value == "AWS":
                # Mirror the route layer's trend fallback.
                daily_trend: list = []
                try:
                    trend = await adapter.get_cost_trend(
                        accounts=rows,
                        period_start=period_start,
                        period_end=period_end,
                        region=effective_region or None,
                    )
                    daily_trend = list(trend.daily or [])
                except Exception:  # noqa: BLE001
                    daily_trend = []
                envelope = build_aws_envelope(
                    summary, rows=rows, scope=scope, daily_trend=daily_trend,
                )
            else:
                # Azure — reuse the per-snapshot ``_daily_points``
                # extension to avoid a second fan-out.
                from decimal import Decimal as _Decimal
                extensions = dict(summary.extensions or {})
                cached_points = extensions.get("_daily_points") or []
                daily = [
                    (
                        date.fromisoformat(p["date"]),
                        _Decimal(str(p["amount"])),
                        p["currency"],
                    )
                    for p in cached_points
                ]
                daily_currency_status = str(
                    extensions.get("_daily_currency_status") or "OK"
                )
                trend = build_trend_from_points(
                    rows=list(rows),
                    period_start=period_start,
                    period_end=period_end,
                    daily_points=daily,
                )
                azure_envelope = build_azure_envelope(
                    summary,
                    rows=rows,
                    scope=scope,
                    daily_trend=trend.daily,
                    daily_currency_status=daily_currency_status,
                )
                payload = azure_envelope.model_dump(mode="json")
                payload["successful_accounts"] = azure_envelope.successful_accounts
                payload["failed_accounts"] = azure_envelope.failed_accounts
                from app.schemas.cost import CloudCostReport
                envelope = CloudCostReport.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloud_ai.context.envelope_failed provider=%s scope=%s code=%s",
                kind.value, scope, type(exc).__name__,
            )
            return None

        return self._slice_cost(envelope)

    async def _safe_resources(
        self, *, adapter, kind, rows, scope,
    ) -> Optional[FinOpsResourceSlice]:
        if adapter is None:
            return None
        try:
            summary = await adapter.get_resource_summary(
                accounts=rows,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloud_ai.context.resources_failed provider=%s scope=%s code=%s",
                kind.value, scope, type(exc).__name__,
            )
            return None
        return self._slice_resources(summary)

    async def _safe_recommendations(
        self, *, adapter, kind, rows, scope,
    ) -> Optional[FinOpsRecommendationSlice]:
        if adapter is None:
            return None
        try:
            recommendations = await adapter.get_recommendations(
                accounts=rows, days=DEFAULT_LOOKBACK_DAYS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cloud_ai.context.recommendations_failed provider=%s scope=%s code=%s",
                kind.value, scope, type(exc).__name__,
            )
            return None
        return self._slice_recommendations(kind.value, recommendations)

    # ------------------------------------------------------------------
    # Slicers
    # ------------------------------------------------------------------

    def _slice_cost(self, envelope) -> FinOpsCostSlice:
        report = envelope.report if hasattr(envelope, "report") else envelope
        trend: List[Dict[str, Any]] = []
        for pt in (report.daily_trend or [])[:90]:
            trend.append(
                {
                    "date": str(pt.date),
                    "amount": str(pt.amount),
                    "currency": str(pt.unit),
                }
            )
        services_sorted = sorted(
            report.by_service or [],
            key=lambda s: -(Decimal(str(s.amount)) if s.amount is not None else Decimal("0")),
        )
        top_services = [
            {"service": s.service, "amount": str(s.amount), "currency": str(s.unit)}
            for s in services_sorted[: self.top_n_services]
        ]
        regions_sorted = sorted(
            report.by_region or [],
            key=lambda r: -(Decimal(str(r.amount)) if r.amount is not None else Decimal("0")),
        )
        top_regions = [
            {"region": r.region, "amount": str(r.amount), "currency": str(r.unit)}
            for r in regions_sorted[: self.top_n_regions]
        ]
        by_account = [
            {
                "account_id": a.account_id,
                "display_name": a.display_name,
                "amount": str(a.amount),
                "currency": str(a.currency),
            }
            for a in (report.by_account or [])
        ]
        errors = [
            {
                "account_id": e.account_id,
                "code": e.code,
                "message": e.message,
            }
            for e in (report.account_errors or [])
        ]
        return FinOpsCostSlice(
            currency=report.currency,
            total_cost=(
                Decimal(str(report.total_cost))
                if report.total_cost is not None
                else None
            ),
            previous_period_cost=(
                Decimal(str(report.previous_period_cost))
                if report.previous_period_cost is not None
                else None
            ),
            change_amount=(
                Decimal(str(report.change_amount))
                if report.change_amount is not None
                else None
            ),
            change_percent=(
                Decimal(str(report.change_percent))
                if report.change_percent is not None
                else None
            ),
            previous_period_unavailable=bool(
                getattr(report, "previous_period_unavailable", False) or False
            ),
            daily_trend=trend,
            top_services=top_services,
            top_regions=top_regions,
            by_account=by_account,
            account_errors=errors,
            successful_accounts=int(report.successful_accounts or 0),
            failed_accounts=int(report.failed_accounts or 0),
            status=str(report.status or "UNKNOWN"),
        )

    def _slice_resources(self, summary) -> FinOpsResourceSlice:
        # Resources surface totals + by_* aggregates (not raw rows).
        by_type = [
            {"type": (getattr(row, "key", "") or ""), "count": int(getattr(row, "count", 0) or 0)}
            for row in (getattr(summary, "by_type", []) or [])
        ]
        by_region = [
            {"region": (getattr(row, "key", "") or ""), "count": int(getattr(row, "count", 0) or 0)}
            for row in (getattr(summary, "by_region", []) or [])
        ]
        by_subscription = [
            {
                "subscription_id": (
                    getattr(row, "subscription_id", None)
                    or getattr(row, "key", "")
                    or ""
                ),
                "count": int(getattr(row, "count", 0) or 0),
            }
            for row in (getattr(summary, "by_subscription", []) or [])
        ]
        return FinOpsResourceSlice(
            total=int(getattr(summary, "total", 0) or getattr(summary, "total_resources", 0) or 0),
            by_type=by_type,
            by_region=by_region,
            by_subscription=by_subscription,
            status=str(getattr(summary, "status", "UNKNOWN") or "UNKNOWN"),
        )

    def _slice_recommendations(self, provider: str, recommendations) -> FinOpsRecommendationSlice:
        items = list(recommendations or [])
        def _key(it):
            try:
                v = Decimal(str(it.estimated_savings)) if it.estimated_savings is not None else Decimal("0")
            except Exception:
                v = Decimal("0")
            return (-v, str(getattr(it, "source", "") or ""), str(getattr(it, "recommendation_id", "") or ""))
        items_sorted = sorted(items, key=_key)
        top_items: List[Dict[str, Any]] = []
        for it in items_sorted[: self.top_n_recommendations]:
            top_items.append(
                {
                    "recommendation_id": str(getattr(it, "recommendation_id", "") or ""),
                    "title": str(getattr(it, "title", "") or ""),
                    "resource_id": str(getattr(it, "resource_id", "") or ""),
                    "resource_type": str(getattr(it, "resource_type", "") or ""),
                    "service": str(getattr(it, "service", "") or ""),
                    "estimated_savings": (
                        str(getattr(it, "estimated_savings"))
                        if getattr(it, "estimated_savings", None) is not None
                        else None
                    ),
                    "currency": (
                        str(getattr(it, "currency"))
                        if getattr(it, "currency", None) is not None
                        else None
                    ),
                    "source": str(getattr(it, "source", "") or ""),
                    "region": (
                        str(getattr(it, "region"))
                        if getattr(it, "region", None) is not None
                        else None
                    ),
                    "category": (
                        str(getattr(it, "category"))
                        if getattr(it, "category", None) is not None
                        else None
                    ),
                }
            )
        agg: Optional[Decimal] = None
        currency: Optional[str] = None
        for it in items:
            if getattr(it, "estimated_savings", None) is None:
                continue
            try:
                v = Decimal(str(it.estimated_savings))
            except Exception:
                continue
            agg = (agg + v) if agg is not None else v
            if currency is None and getattr(it, "currency", None) is not None:
                currency = str(it.currency)
        sources: List[str] = []
        seen: set[str] = set()
        for it in items:
            s = str(getattr(it, "source", "") or "")
            if s and s not in seen:
                seen.add(s)
                sources.append(s)
        savings_period = "ANNUAL" if provider == "AZURE" else "MONTHLY"
        return FinOpsRecommendationSlice(
            count=len(items),
            estimated_savings=agg,
            currency=currency,
            savings_period=savings_period,
            top_items=top_items,
            sources=sources,
        )

    # ------------------------------------------------------------------
    # Aggregations
    # ------------------------------------------------------------------

    def _classify_availability(
        self,
        cost: Optional[FinOpsCostSlice],
        resources: Optional[FinOpsResourceSlice],
        recommendations: Optional[FinOpsRecommendationSlice],
        warnings: List[str],
    ) -> Tuple[List[str], List[str]]:
        partial: List[str] = []
        unavailable: List[str] = []
        if cost is None:
            unavailable.append("cost")
            warnings.append("Costs source unavailable; AI answer will not include cost data.")
        elif cost.status == "PARTIAL_SUCCESS" and cost.successful_accounts == 0:
            unavailable.append("cost")
            warnings.append("All subscription/account cost calls failed; cost is unavailable.")
        elif cost.status == "PARTIAL_SUCCESS":
            partial.append("cost")
        if resources is None:
            unavailable.append("resources")
            warnings.append("Resources source unavailable.")
        if recommendations is None:
            unavailable.append("recommendations")
            warnings.append("Recommendations source unavailable.")
        elif recommendations.count == 0:
            partial.append("recommendations")
            warnings.append("No recommendations returned for this scope.")
        return partial, unavailable

    def _resolve_freshness(
        self,
        cost: Optional[FinOpsCostSlice],
        resources: Optional[FinOpsResourceSlice],
        recommendations: Optional[FinOpsRecommendationSlice],
    ) -> FinOpsAIFreshness:
        if cost is None and resources is None and recommendations is None:
            return FinOpsAIFreshness.UNAVAILABLE
        return FinOpsAIFreshness.LIVE

    def _resolve_status(
        self,
        partial: List[str],
        unavailable: List[str],
        freshness: FinOpsAIFreshness,
    ) -> FinOpsContextStatus:
        if unavailable and not partial:
            return FinOpsContextStatus.UNAVAILABLE
        if unavailable or partial:
            return FinOpsContextStatus.PARTIAL
        return FinOpsContextStatus.LIVE

    def _display_name(
        self, cost: Optional[FinOpsCostSlice], scope: str
    ) -> Optional[str]:
        if not cost:
            return None
        for acct in cost.by_account:
            if scope == "all":
                if acct.get("display_name"):
                    return acct.get("display_name")
                continue
            if scope in (acct.get("account_id") or ""):
                return acct.get("display_name")
        return None

    def _period_from_inputs(
        self, period_start: date, period_end: date,
    ) -> FinOpsAIPeriod:
        days = (period_end - period_start).days
        kind = "days"
        # If the range covers a full calendar month we still label it
        # ``days`` because the canonical contract distinguishes
        # ``month`` / ``days`` / ``explicit`` via the request shape,
        # not via derived dates.  The day-count is what the LLM
        # needs to compare windows.
        return FinOpsAIPeriod(
            kind=kind,
            start=period_start.isoformat(),
            end=period_end.isoformat(),
            days=days,
        )

    def _derive_period_from_request(
        self,
        *,
        today: date,
        days: Optional[int],
        month: Optional[str],
        start_date: Optional[date],
        end_date: Optional[date],
    ) -> FinOpsAIPeriod:
        if month:
            year, m = map(int, month.split("-"))
            start = date(year, m, 1)
            if m == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, m + 1, 1)
            return FinOpsAIPeriod(
                kind="month", start=start.isoformat(), end=end.isoformat(),
                days=(end - start).days,
            )
        if start_date and end_date:
            return FinOpsAIPeriod(
                kind="explicit", start=start_date.isoformat(),
                end=end_date.isoformat(), days=(end_date - start_date).days,
            )
        n = days or DEFAULT_LOOKBACK_DAYS
        return FinOpsAIPeriod(
            kind="days",
            start=(today - timedelta(days=n)).isoformat(),
            end=today.isoformat(),
            days=n,
        )


__all__ = [
    "FinOpsContextBuilder",
    "FinOpsContext",
    "FinOpsContextStatus",
    "FinOpsCostSlice",
    "FinOpsResourceSlice",
    "FinOpsRecommendationSlice",
    "DEFAULT_TOP_N_SERVICES",
    "DEFAULT_TOP_N_REGIONS",
    "DEFAULT_TOP_N_RECOMMENDATIONS",
]
