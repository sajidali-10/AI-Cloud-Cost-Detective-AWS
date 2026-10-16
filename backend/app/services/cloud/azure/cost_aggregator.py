"""Azure cost aggregator — Phase 7C.2B.

Aggregates Microsoft Cost Management responses for one or more
Azure subscriptions into the provider-neutral shapes the rest of
the platform already understands.

Public surface
--------------

* :func:`aggregate_cost_summary_for_subscriptions` — produces a
  :class:`~app.services.cloud.models.CloudCostSummary` for a set
  of subscriptions (single, "all", or per-subscription fan-out).
* :func:`aggregate_cost_trend_for_subscriptions` — produces a
  :class:`~app.services.cloud.models.CloudCostTrend` for a set of
  subscriptions.
* :func:`aggregate_cost_by_service_for_subscriptions` / 
  :func:`aggregate_cost_by_region_for_subscriptions` — the
  per-dimension breakdowns.

Failure isolation
-----------------

Each subscription is queried independently.  An error from one
subscription is captured as a sanitised
:class:`AzureSubscriptionCostFailure` and **does not** fail the
overall response.

Currency safety
---------------

The aggregator refuses to silently combine different currencies.
When every successful subscription reports the same currency, the
rollup is denominated in that currency.  When the currencies
differ, the aggregator returns a special
:class:`AzureCostSummary` whose ``extensions["currency_status"]``
is ``"MIXED_CURRENCY_UNSUPPORTED"`` and whose monetary totals are
left at ``Decimal("0")`` — the caller (the API route) renders an
explicit "mixed currency" UI state instead of inventing an FX
rate.

NO_DATA / EMPTY semantics
-------------------------

Cost Management distinguishes:

* 204 No Content → ``AzureCostCallStatus.NO_DATA``.  The aggregator
  treats this as the **NO_DATA** public status.  The frontend
  renders "Connected but no cost data" — distinct from
  "authentication failed".
* Rows but no data for the requested period →
  ``AzureCostCallStatus.EMPTY_FOR_PERIOD`` (rare; surfaced the
  same as NO_DATA).
* Rows summing to zero → ``AzureCostCallStatus.OK`` with a zero
  amount.  The frontend renders "0.00 USD" — the cost actually is
  zero, distinct from absence of data.

Bounded concurrency
-------------------

The aggregator uses an :class:`asyncio.Semaphore` of size
``AZURE_COST_MAX_CONCURRENCY`` (env-driven, default ``4``) so a
slow subscription cannot serialise the rest of the fan-out.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.db.models import CloudAccount
from app.services.cloud.azure.costs import (
    ARM_BASE_URL,
    AzureCostCallStatus,
    AzureCostClient,
    AzureCostError,
    AzureCostErrorCategory,
    AzureQueryResult,
    DIM_RESOURCE_LOCATION,
    DIM_SERVICE_NAME,
    UNASSIGNED_BUCKET,
    build_daily_dataset,
    build_grouped_dataset,
    build_time_period,
    build_total_dataset,
    extract_by_dimension,
    extract_daily,
    extract_total,
)
from app.services.cloud.models import (
    CloudCostSummary,
    CloudCostTrend,
    CloudProviderKind,
    CloudServiceCost,
)

logger = logging.getLogger("cost-detective.azure.cost_aggregator")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


def _max_concurrency() -> int:
    return int(os.getenv("AZURE_COST_MAX_CONCURRENCY", "4"))


def _default_currency() -> str:
    return os.getenv("AZURE_COST_DEFAULT_CURRENCY", "USD").upper()


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureSubscriptionCostFailure:
    """Sanitised per-subscription failure surfaced to the API layer."""

    subscription_id: str
    display_name: str
    category: str
    status_code: Optional[int]
    message: str


@dataclass(frozen=True)
class AzureSubscriptionCostSnapshot:
    """A single subscription's contribution to a rollup."""

    subscription_id: str
    display_name: str
    status: AzureCostCallStatus
    currency: str
    total: Decimal
    daily: Tuple[Tuple[date, Decimal, str], ...]
    by_service: Tuple[Tuple[str, Decimal, str], ...]
    by_region: Tuple[Tuple[str, Decimal, str], ...]
    error: Optional[AzureSubscriptionCostFailure] = None


# ---------------------------------------------------------------------------
# Per-subscription fetch helpers
# ---------------------------------------------------------------------------


def _query_total(
    client: AzureCostClient,
    *,
    subscription_id: str,
    period_start: date,
    period_end: date,
    today: Optional[date],
) -> Tuple[AzureCostCallStatus, Decimal, str]:
    timeframe, from_iso, to_iso = build_time_period(
        period_start=period_start, period_end=period_end, today=today
    )
    result = client.query(
        subscription_id=subscription_id,
        dataset=build_total_dataset(),
        timeframe=timeframe,
        from_iso=from_iso or None,
        to_iso=to_iso or None,
    )
    return extract_total(result, default_currency=_default_currency())


def _query_daily(
    client: AzureCostClient,
    *,
    subscription_id: str,
    period_start: date,
    period_end: date,
    today: Optional[date],
) -> Tuple[AzureCostCallStatus, List[Tuple[date, Decimal, str]], str]:
    timeframe, from_iso, to_iso = build_time_period(
        period_start=period_start, period_end=period_end, today=today
    )
    result = client.query(
        subscription_id=subscription_id,
        dataset=build_daily_dataset(),
        timeframe=timeframe,
        from_iso=from_iso or None,
        to_iso=to_iso or None,
    )
    return extract_daily(result, default_currency=_default_currency())


def _query_grouped(
    client: AzureCostClient,
    *,
    subscription_id: str,
    period_start: date,
    period_end: date,
    today: Optional[date],
    dimension: str,
) -> Tuple[AzureCostCallStatus, List[Tuple[str, Decimal, str]], str]:
    timeframe, from_iso, to_iso = build_time_period(
        period_start=period_start, period_end=period_end, today=today
    )
    result = client.query(
        subscription_id=subscription_id,
        dataset=build_grouped_dataset(dimension),
        timeframe=timeframe,
        from_iso=from_iso or None,
        to_iso=to_iso or None,
    )
    return extract_by_dimension(result, dimension_name=dimension, default_currency=_default_currency())


def _sanitize_error(
    exc: AzureCostError,
    *,
    subscription_id: str,
    display_name: str,
) -> AzureSubscriptionCostFailure:
    return AzureSubscriptionCostFailure(
        subscription_id=subscription_id,
        display_name=display_name,
        category=exc.category.value,
        status_code=exc.status_code,
        message=exc.category.value,  # sanitised category only — never the raw message
    )


def _client_for_subscription() -> AzureCostClient:
    """Build a per-call Cost Management client.

    Cheap to construct; closes at the end of the call.
    """
    return AzureCostClient()


# ---------------------------------------------------------------------------
# All-subscription fan-out
# ---------------------------------------------------------------------------


async def fetch_subscription_snapshots(
    *,
    rows: Sequence[CloudAccount],
    period_start: date,
    period_end: date,
    today: Optional[date],
    include_groupings: bool = True,
) -> List[AzureSubscriptionCostSnapshot]:
    """Fetch total + (optionally) groupings for every subscription.

    Returns one :class:`AzureSubscriptionCostSnapshot` per
    subscription, in input order.  Failures are folded into the
    snapshot's ``error`` field; the snapshot's ``status`` is
    :class:`AzureCostCallStatus` (``OK`` / ``NO_DATA`` /
    ``EMPTY``) on success, ``EMPTY`` on failure so the caller can
    see the snapshot was attempted but did not yield data.
    """
    if not rows:
        return []

    sem = asyncio.Semaphore(_max_concurrency())

    async def _fetch(row: CloudAccount) -> AzureSubscriptionCostSnapshot:
        async with sem:
            client = _client_for_subscription()
            try:
                status, total, currency = _query_total(
                    client,
                    subscription_id=row.account_id,
                    period_start=period_start,
                    period_end=period_end,
                    today=today,
                )
                daily: List[Tuple[date, Decimal, str]] = []
                by_service: List[Tuple[str, Decimal, str]] = []
                by_region: List[Tuple[str, Decimal, str]] = []
                if include_groupings:
                    _, daily, currency_d = _query_daily(
                        client,
                        subscription_id=row.account_id,
                        period_start=period_start,
                        period_end=period_end,
                        today=today,
                    )
                    # Daily + total should agree on currency; trust
                    # the grouping call's currency when the total
                    # call didn't surface one.
                    if currency_d:
                        currency = currency_d
                    _, by_service, _ = _query_grouped(
                        client,
                        subscription_id=row.account_id,
                        period_start=period_start,
                        period_end=period_end,
                        today=today,
                        dimension=DIM_SERVICE_NAME,
                    )
                    _, by_region, _ = _query_grouped(
                        client,
                        subscription_id=row.account_id,
                        period_start=period_start,
                        period_end=period_end,
                        today=today,
                        dimension=DIM_RESOURCE_LOCATION,
                    )
                return AzureSubscriptionCostSnapshot(
                    subscription_id=row.account_id,
                    display_name=row.display_name,
                    status=status,
                    currency=currency,
                    total=total,
                    daily=tuple(daily),
                    by_service=tuple(by_service),
                    by_region=tuple(by_region),
                    error=None,
                )
            except AzureCostError as exc:
                return AzureSubscriptionCostSnapshot(
                    subscription_id=row.account_id,
                    display_name=row.display_name,
                    status=AzureCostCallStatus.EMPTY,
                    currency=_default_currency(),
                    total=Decimal("0"),
                    daily=(),
                    by_service=(),
                    by_region=(),
                    error=_sanitize_error(
                        exc,
                        subscription_id=row.account_id,
                        display_name=row.display_name,
                    ),
                )
            finally:
                client.close()

    return await asyncio.gather(*(_fetch(row) for row in rows))


# ---------------------------------------------------------------------------
# Rollup — totals
# ---------------------------------------------------------------------------


@dataclass
class AzureRollupTotals:
    """Aggregated totals across multiple subscriptions.

    Distinct from :class:`CloudCostSummary` so the caller (the
    adapter) can decide whether to render a single-subscription or
    multi-subscription envelope.
    """

    status: str  # SUCCESS | PARTIAL_SUCCESS | ERROR | MIXED_CURRENCY
    currency: str
    total: Decimal
    snapshots: List[AzureSubscriptionCostSnapshot]
    failures: List[AzureSubscriptionCostFailure]

    @property
    def has_mixed_currencies(self) -> bool:
        return self.status == "MIXED_CURRENCY"

    @property
    def is_no_data(self) -> bool:
        return (
            not self.snapshots
            and not self.failures
        ) or (
            all(snap.status == AzureCostCallStatus.NO_DATA for snap in self.snapshots)
            and not self.failures
        )


def rollup_totals(
    snapshots: Sequence[AzureSubscriptionCostSnapshot],
    *,
    failures: Sequence[AzureSubscriptionCostFailure] = (),
) -> AzureRollupTotals:
    """Combine per-subscription snapshots into a single rollup.

    Currency rules:

    * If every successful snapshot reports the same currency, the
      rollup uses that currency and totals are summed.
    * If no snapshot succeeded, the rollup uses the default
      currency (``AZURE_COST_DEFAULT_CURRENCY``).
    * If the successful snapshots report different currencies, the
      rollup status is ``MIXED_CURRENCY``; ``total`` is left at
      ``Decimal("0")`` and the API layer renders an explicit
      "mixed currency" UI state.
    """
    successful = [snap for snap in snapshots if snap.error is None]
    if not successful and not failures:
        return AzureRollupTotals(
            status="NO_DATA",
            currency=_default_currency(),
            total=Decimal("0"),
            snapshots=list(snapshots),
            failures=list(failures),
        )
    currencies = {snap.currency for snap in successful if snap.currency}
    if not currencies:
        currency = _default_currency()
    elif len(currencies) == 1:
        currency = next(iter(currencies))
    else:
        return AzureRollupTotals(
            status="MIXED_CURRENCY",
            currency=_default_currency(),
            total=Decimal("0"),
            snapshots=list(snapshots),
            failures=list(failures),
        )
    total = sum((snap.total for snap in successful), Decimal("0"))
    if failures:
        status = "PARTIAL_SUCCESS"
    else:
        status = "SUCCESS"
    return AzureRollupTotals(
        status=status,
        currency=currency,
        total=total.quantize(Decimal("0.01")),
        snapshots=list(snapshots),
        failures=list(failures),
    )


def rollup_daily(
    snapshots: Sequence[AzureSubscriptionCostSnapshot],
    *,
    period_start: date,
    period_end: date,
) -> Tuple[str, List[Tuple[date, Decimal, str]], str]:
    """Combine per-subscription daily series into a single calendar sequence.

    Returns ``(currency_status, daily_points, currency)``.

    ``currency_status`` is one of:

    * ``OK`` — every successful snapshot reported the same currency.
    * ``MIXED_CURRENCY`` — different currencies; ``daily_points``
      is empty so the UI can render an explicit "mixed currency"
      state.
    * ``EMPTY`` — no daily data was returned for any subscription.

    Days where every snapshot reported zero are filled with 0
    *only when the underlying query returned ``OK``* — never for
    NO_DATA / EMPTY / MIXED_CURRENCY outcomes.  This is the spec's
    "use zero only for absent days after a successful query"
    requirement.
    """
    successful = [snap for snap in snapshots if snap.error is None]
    if not successful:
        return ("EMPTY", [], _default_currency())
    currencies = {snap.currency for snap in successful if snap.currency}
    if len(currencies) > 1:
        return ("MIXED_CURRENCY", [], _default_currency())
    if not currencies:
        return ("EMPTY", [], _default_currency())
    currency = next(iter(currencies))

    # Build the full UTC calendar range for the period.
    days_in_period = (period_end - period_start).days
    if days_in_period <= 0:
        return ("OK", [], currency)
    calendar_days = [period_start + _offset_day(i) for i in range(days_in_period)]

    # Aggregate per-day totals — only include the days Cost
    # Management actually returned.  We honour the spec's rule:
    # fill missing days only when the rollup is OK.
    accum: Dict[date, Decimal] = {day: Decimal("0") for day in calendar_days}
    seen: Dict[date, int] = {day: 0 for day in calendar_days}
    for snap in successful:
        for day, amount, cur in snap.daily:
            if day not in accum:
                # Cost Management may return days slightly outside
                # the requested window for MTD queries; ignore them
                # so we don't inflate the trend.
                continue
            accum[day] += amount
            seen[day] += 1
    # A "successful query" with no rows means NO_DATA — do not
    # fill zeros for it.
    if all(seen[day] == 0 for day in calendar_days):
        return ("OK", [], currency)
    points = [
        (day, accum[day].quantize(Decimal("0.01")), currency)
        for day in calendar_days
    ]
    return ("OK", points, currency)


def _offset_day(n: int):
    """Helper — return ``timedelta(days=n)`` for the rollup range.

    Kept inline so the import surface stays minimal.
    """
    from datetime import timedelta
    return timedelta(days=n)


def rollup_by_dimension(
    snapshots: Sequence[AzureSubscriptionCostSnapshot],
    *,
    dimension_attr: str,
) -> Tuple[str, List[Tuple[str, Decimal, str]], str]:
    """Combine per-subscription per-dimension breakdowns.

    Returns ``(currency_status, items, currency)``.

    ``dimension_attr`` is the attribute name on the snapshot that
    holds the per-subscription breakdown tuple — ``"by_service"``
    or ``"by_region"``.  When two subscriptions disagree on the
    currency the rollup is ``MIXED_CURRENCY`` and ``items`` is
    empty.
    """
    successful = [snap for snap in snapshots if snap.error is None]
    if not successful:
        return ("EMPTY", [], _default_currency())
    currencies = {snap.currency for snap in successful if snap.currency}
    if len(currencies) > 1:
        return ("MIXED_CURRENCY", [], _default_currency())
    if not currencies:
        return ("EMPTY", [], _default_currency())
    currency = next(iter(currencies))
    accum: Dict[str, Decimal] = {}
    for snap in successful:
        items = getattr(snap, dimension_attr)
        for name, amount, cur in items:
            accum[name] = accum.get(name, Decimal("0")) + amount
    if not accum:
        return ("OK", [], currency)
    items = sorted(
        ((name, amt.quantize(Decimal("0.01")), currency) for name, amt in accum.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    return ("OK", items, currency)


# ---------------------------------------------------------------------------
# Public aggregator entry points
# ---------------------------------------------------------------------------


async def aggregate_cost_summary_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
    period_start: date,
    period_end: date,
    previous_period_start: date,
    previous_period_end: date,
    today: Optional[date],
    region: Optional[str] = None,
) -> Tuple[CloudCostSummary, List[AzureSubscriptionCostFailure]]:
    """Aggregate a CloudCostSummary across ``rows``.

    The returned summary is attributed back to each contributing
    subscription via ``extensions["by_subscription"]``.  ``region``
    is forwarded as a ``ResourceLocation`` filter when supplied.
    Today this layer does not expose the region filter at the
    adapter level — Cost Management supports it but a future phase
    will surface it through the UI; for now ``region`` is accepted
    for symmetry with the AWS adapter but ignored.
    """
    if not rows:
        empty = CloudCostSummary(
            provider=CloudProviderKind.AZURE,
            account_id=None,
            scope="all",
            period_start=period_start,
            period_end=period_end,
            currency=_default_currency(),
            current_total=Decimal("0"),
            previous_total=Decimal("0"),
            change_amount=Decimal("0"),
            change_percent=None,
            estimated=False,
            by_service=[],
            by_region=[],
            extensions={
                "status": "NO_DATA",
                "currency_status": "EMPTY",
                "by_subscription": [],
                "subscription_errors": [],
                "period_kind": "current",
            },
        )
        return empty, []

    snapshots = await fetch_subscription_snapshots(
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        today=today,
    )
    failures = [snap.error for snap in snapshots if snap.error is not None]
    rollup = rollup_totals(snapshots, failures=failures)
    by_service_status, by_service, by_service_currency = rollup_by_dimension(
        snapshots, dimension_attr="by_service"
    )
    by_region_status, by_region, by_region_currency = rollup_by_dimension(
        snapshots, dimension_attr="by_region"
    )
    # Previous period — fetch totals only (the by-service /
    # by-region breakdowns would multiply API calls for marginal
    # benefit on the comparison view).
    previous_snapshots = await fetch_subscription_snapshots(
        rows=rows,
        period_start=previous_period_start,
        period_end=previous_period_end,
        today=today,
        include_groupings=False,
    )
    previous_rollup = rollup_totals(previous_snapshots)

    change = rollup.total - previous_rollup.total
    if previous_rollup.total == 0:
        change_percent: Optional[Decimal] = None
    else:
        change_percent = (change / previous_rollup.total).quantize(Decimal("0.01"))

    # Account-id attribution: None when aggregating across
    # subscriptions, or the single subscription id when ``rows``
    # has exactly one entry.
    account_id_value: Optional[str] = None
    if len(rows) == 1:
        account_id_value = rows[0].account_id
    scope = "all" if len(rows) != 1 else f"sub:{rows[0].account_id}"
    # Currency resolution — if either side mixes currencies, surface
    # the mixed-currency status on the envelope.
    currency_status = rollup.status
    if rollup.has_mixed_currencies:
        currency_status = "MIXED_CURRENCY"
    elif by_service_status == "MIXED_CURRENCY" or by_region_status == "MIXED_CURRENCY":
        currency_status = "MIXED_CURRENCY"

    extensions = {
        "status": rollup.status,
        "currency_status": currency_status,
        "by_service_currency_status": by_service_status,
        "by_region_currency_status": by_region_status,
        "by_subscription": [
            {
                "subscription_id": snap.subscription_id,
                "display_name": snap.display_name,
                "amount": str(snap.total),
                "currency": snap.currency,
                "status": snap.status.value,
            }
            for snap in snapshots
        ],
        "subscription_errors": [
            {
                "subscription_id": err.subscription_id,
                "display_name": err.display_name,
                "code": err.category,
                "message": err.message,
            }
            for err in failures
        ],
        "previous_status": previous_rollup.status,
        "period_kind": "current",
    }
    summary = CloudCostSummary(
        provider=CloudProviderKind.AZURE,
        account_id=account_id_value,
        scope=scope,
        period_start=period_start,
        period_end=period_end,
        currency=rollup.currency,
        current_total=rollup.total,
        previous_total=previous_rollup.total,
        change_amount=change,
        change_percent=change_percent,
        estimated=False,
        by_service=[
            CloudServiceCost(
                service=name,
                amount=amount,
                currency=by_service_currency,
                provider=CloudProviderKind.AZURE,
                account_id=account_id_value,
                region=None,
            )
            for name, amount, _ in by_service
        ],
        by_region=[
            CloudServiceCost(
                service=name,
                amount=amount,
                currency=by_region_currency,
                provider=CloudProviderKind.AZURE,
                account_id=account_id_value,
                region=name,
            )
            for name, amount, _ in by_region
        ],
        extensions=extensions,
    )
    return summary, list(failures)


async def aggregate_cost_trend_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
    period_start: date,
    period_end: date,
    today: Optional[date],
) -> Tuple[CloudCostTrend, List[AzureSubscriptionCostFailure], str]:
    """Aggregate a daily cost trend across ``rows``.

    Returns ``(trend, failures, currency_status)``.  The
    :class:`CloudCostTrend` dataclass is frozen and does not
    expose an ``extensions`` field, so the per-subscription
    attribution is surfaced through the API layer's own envelope
    rather than attached to the model.
    """
    if not rows:
        return (
            CloudCostTrend(
                provider=CloudProviderKind.AZURE,
                account_id=None,
                period_start=period_start,
                period_end=period_end,
                currency=_default_currency(),
                daily=[],
                source="AZURE_COST_MANAGEMENT",
            ),
            [],
            "EMPTY",
        )
    snapshots = await fetch_subscription_snapshots(
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        today=today,
        include_groupings=True,
    )
    failures = [snap.error for snap in snapshots if snap.error is not None]
    currency_status, daily_points, currency = rollup_daily(
        snapshots, period_start=period_start, period_end=period_end
    )
    account_id_value = rows[0].account_id if len(rows) == 1 else None
    trend = CloudCostTrend(
        provider=CloudProviderKind.AZURE,
        account_id=account_id_value,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        daily=[
            CloudServiceCost(
                service=str(day),
                amount=amount,
                currency=cur,
                provider=CloudProviderKind.AZURE,
                account_id=account_id_value,
                region=None,
                extensions={"date": day.isoformat()},
            )
            for day, amount, cur in daily_points
        ],
        source="AZURE_COST_MANAGEMENT",
    )
    return trend, list(failures), currency_status


async def aggregate_cost_by_service_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
    period_start: date,
    period_end: date,
    today: Optional[date],
) -> Tuple[List[CloudServiceCost], str, List[AzureSubscriptionCostFailure]]:
    """Aggregate cost by service across ``rows``."""
    if not rows:
        return [], "EMPTY", []
    snapshots = await fetch_subscription_snapshots(
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        today=today,
        include_groupings=True,
    )
    failures = [snap.error for snap in snapshots if snap.error is not None]
    status, items, currency = rollup_by_dimension(snapshots, dimension_attr="by_service")
    account_id_value = rows[0].account_id if len(rows) == 1 else None
    return (
        [
            CloudServiceCost(
                service=name,
                amount=amount,
                currency=currency,
                provider=CloudProviderKind.AZURE,
                account_id=account_id_value,
                region=None,
            )
            for name, amount, _ in items
        ],
        status,
        list(failures),
    )


async def aggregate_cost_by_region_for_subscriptions(
    *,
    rows: Sequence[CloudAccount],
    period_start: date,
    period_end: date,
    today: Optional[date],
) -> Tuple[List[CloudServiceCost], str, List[AzureSubscriptionCostFailure]]:
    """Aggregate cost by region/location across ``rows``."""
    if not rows:
        return [], "EMPTY", []
    snapshots = await fetch_subscription_snapshots(
        rows=rows,
        period_start=period_start,
        period_end=period_end,
        today=today,
        include_groupings=True,
    )
    failures = [snap.error for snap in snapshots if snap.error is not None]
    status, items, currency = rollup_by_dimension(snapshots, dimension_attr="by_region")
    account_id_value = rows[0].account_id if len(rows) == 1 else None
    return (
        [
            CloudServiceCost(
                service=name,
                amount=amount,
                currency=currency,
                provider=CloudProviderKind.AZURE,
                account_id=account_id_value,
                region=name,
            )
            for name, amount, _ in items
        ],
        status,
        list(failures),
    )


__all__ = [
    "AzureRollupTotals",
    "AzureSubscriptionCostFailure",
    "AzureSubscriptionCostSnapshot",
    "aggregate_cost_by_region_for_subscriptions",
    "aggregate_cost_by_service_for_subscriptions",
    "aggregate_cost_summary_for_subscriptions",
    "aggregate_cost_trend_for_subscriptions",
    "fetch_subscription_snapshots",
    "rollup_by_dimension",
    "rollup_daily",
    "rollup_totals",
]
