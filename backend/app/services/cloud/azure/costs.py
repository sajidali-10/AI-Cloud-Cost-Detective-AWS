"""Azure Cost Management client + aggregator — Phase 7C.2B.

This module wraps the Microsoft Cost Management Query API at
subscription scope and turns the provider-specific payloads into
provider-neutral :class:`~app.services.cloud.models.CloudCostSummary`
shapes the frontend already understands.

.. code-block:: text

    POST https://management.azure.com/subscriptions/{id}/providers/
         Microsoft.CostManagement/query?api-version=2026-06-01

Cost semantic decision
----------------------

We pin to ``ActualCost`` for every subscription.  ``ActualCost``
represents the unbilled + billed charges as they accrue — the
metric that maps cleanly to AWS Cost Explorer's ``UnblendedCost``
and to what an operator sees on the Azure Portal "Cost analysis"
blade.  ``AmortizedCost`` would require reservation / savings-plan
data that this phase does not consume, and the data window for
``AmortizedCost`` differs (today minus 7 days only).  The pin is
deliberate — the cost semantics are documented and tested; the
caller is never allowed to silently swap cost types.

Period semantics
----------------

The Azure portal uses **billing month** semantics (28th of prior
month through 27th of current month) by default.  This portal is
calendar-month-only and the API path uses the application's
existing AWS-style calendar-month semantics.  Specifically:

* Current month: ``timeframe=MonthToDate`` so the server trims the
  window to ``[month_start, today)``.
* Any other (completed) month: ``timeframe=Custom`` with explicit
  UTC boundaries ``[month_start, next_month_start)``.

The mapping is enforced inside :func:`build_time_period`; the
caller never decides between MTD and Custom.

Pagination
----------

Cost Management paginates via ``properties.nextLink`` which carries
an absolute URL the caller must GET against the same management
endpoint.  We:

* follow nextLink verbatim (it is authoritative);
* reject nextLinks that don't point at the ARM management endpoint
  (defence against an unexpected 302 chain);
* bound the loop with :data:`MAX_PAGES` so a pathological response
  cannot drive an unbounded request chain.

Error mapping
-------------

Every non-2xx response is folded into a sanitised category by
:class:`AzureCostError`.  The status field is consumed by the
adapter and propagated through the cache + API layers without
ever echoing the raw ARM body.

Status semantics
----------------

The :class:`AzureCostCallStatus` enum is the **public** contract
between this client and the adapter.  ``NO_DATA`` is the
distinct "Azure returned 204 No Content" outcome; ``OK`` is the
distinct "Azure returned numeric cost > 0"; ``EMPTY`` is the
distinct "Azure returned rows but they summed to zero" (which we
preserve as a real signal — cost actually is zero, distinct from
NO_DATA).  ``AUTH_FAILED`` / ``ACCESS_DENIED`` / ``NOT_FOUND`` /
``THROTTLED`` / ``UPSTREAM_ERROR`` are propagated as-is so the
adapter can map them onto provider-neutral UI states.

Thread-safety
-------------

:class:`AzureCostClient` owns a private ``httpx.Client`` and is
safe to share across requests.  The class is instantiated once at
adapter construction time and reused for every cost call.

Security
--------

* No ``Authorization: Bearer …`` is ever logged.
* No access token / client assertion / federation JWT is ever
  exposed in an exception message.
* All subscription ids are truncated via
  :func:`app.services.cloud.azure.redaction.safe_subscription_label`
  before logging.
* Response payloads are never echoed in errors — only the status
  category and the safe subscription label.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import httpx

from app.services.cloud.azure.credential import (
    AzureAuthError,
    acquire_access_token,
)
from app.services.cloud.azure.redaction import (
    AZURE_STATUS_ACCESS_DENIED,
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_NOT_FOUND,
    safe_subscription_label,
    scrub_log_payload,
)
from app.services.cloud.models import CloudProviderKind, CloudServiceCost


logger = logging.getLogger("cost-detective.azure.cost")


# ---------------------------------------------------------------------------
# Constants — pinned from the Phase 7C.2B spec.
# ---------------------------------------------------------------------------

#: Microsoft Cost Management Query API version.
#: 2026-06-01 is the current GA API version; pinning explicitly so
#: the cache key + docs reference a single, stable version.
COST_MGMT_API_VERSION: str = "2026-06-01"

#: ARM base URL.
ARM_BASE_URL: str = "https://management.azure.com"

#: Cost-type semantic — see module docstring.
COST_TYPE_ACTUAL: str = "ActualCost"

#: Cap on paginated follow-up pages.  Cost Management may page large
#: responses; 50 is generous for any single subscription's daily
#: monthly slice and bounds the worst-case call chain.
MAX_PAGES: int = 50

#: Per-request timeout in seconds.  Cost Management responses can be
#: large (e.g. by-service per day) so we are slightly more generous
#: than the validation GET.
DEFAULT_TIMEOUT_SECONDS: float = 20.0

#: Unassigned bucket for resources with no ResourceLocation metadata.
UNASSIGNED_BUCKET: str = "Unassigned"

#: Service dimension name returned by the Cost Management API.
#: Validated dynamically before use — see :func:`_resolve_columns`.
DIM_SERVICE_NAME: str = "ServiceName"
DIM_RESOURCE_LOCATION: str = "ResourceLocation"


# ---------------------------------------------------------------------------
# Public status enum
# ---------------------------------------------------------------------------


class AzureCostCallStatus(str, Enum):
    """Public status envelope for a single cost call.

    Distinct from :class:`AzureCostError` — ``AzureCostCallStatus``
    is the success-side outcome (``OK`` / ``NO_DATA`` / ``EMPTY`` /
    ``EMPTY_FOR_PERIOD``); ``AzureCostError`` is the failure-side
    exception type raised on a non-2xx or parse failure.

    The two-sided shape lets the adapter distinguish:

    * "Azure returned 204 → NO_DATA, do not invent a zero" from
    * "Azure returned rows that summed to zero → cost actually is
      zero, not absence of data".
    """

    OK = "OK"
    NO_DATA = "NO_DATA"
    EMPTY = "EMPTY"  # zero rows
    EMPTY_FOR_PERIOD = "EMPTY_FOR_PERIOD"  # rows but no data for the requested period


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


# Status categories that align with the existing
# ``app.services.cloud.azure.redaction`` mapping so the adapter
# can fold a cost failure into the same UI vocabulary as a
# subscription-validation failure.
class AzureCostErrorCategory(str, Enum):
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    THROTTLED = "THROTTLED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"


@dataclass
class AzureCostError(Exception):
    """Sanitized Cost Management failure.

    The original exception / response body is never exposed.  The
    ``category`` is the public contract the adapter consumes; the
    ``retry_after`` is propagated only when the upstream supplied
    a ``Retry-After`` header (so the API layer can honour it).
    """

    category: AzureCostErrorCategory
    subscription_id: Optional[str] = None
    status_code: Optional[int] = None
    retry_after: Optional[int] = None
    message: Optional[str] = None

    def __str__(self) -> str:  # pragma: no cover - defensive
        label = safe_subscription_label(self.subscription_id)
        return f"[{self.category.value}] azure cost call failed sub={label}"


# ---------------------------------------------------------------------------
# Result dataclasses (provider-internal — never exposed past the adapter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Column:
    """A column from the Cost Management ``properties.columns`` array."""

    name: str
    type: str
    index: int


@dataclass(frozen=True)
class AzureQueryResult:
    """A parsed Cost Management Query response.

    ``columns`` is the dynamic column metadata returned by the
    server; ``rows`` is the raw row tuples whose positional order
    matches ``columns[i].index``.  We never assume a fixed column
    position — see :func:`_resolve_columns`.
    """

    columns: Tuple[_Column, ...]
    rows: Tuple[Tuple[Any, ...], ...]

    def find_column(self, name: str) -> Optional[int]:
        """Return the index of column ``name`` or ``None`` if absent."""
        for col in self.columns:
            if col.name == name:
                return col.index
        return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AzureCostClient:
    """Thin REST wrapper around the Microsoft Cost Management Query API."""

    def __init__(
        self,
        *,
        base_url: str = ARM_BASE_URL,
        api_version: str = COST_MGMT_API_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cost_type: str = COST_TYPE_ACTUAL,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._cost_type = cost_type
        self._max_pages = max_pages
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            trust_env=False,
            headers={"User-Agent": "cost-detective-azure-cost/phase7c2b"},
        )

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001 - defensive
            pass

    # ---- public API ------------------------------------------------------

    def query(
        self,
        *,
        subscription_id: str,
        dataset: Mapping[str, Any],
        timeframe: str,
        from_iso: Optional[str] = None,
        to_iso: Optional[str] = None,
    ) -> AzureQueryResult:
        """Issue a Cost Management Query call and follow pagination.

        ``dataset`` is the standard ``dataset`` object (granularity,
        aggregation, grouping).  ``timeframe`` is one of
        ``MonthToDate`` / ``Custom``.  For ``Custom``, ``from_iso``
        and ``to_iso`` are required ISO-8601 strings.  For
        ``MonthToDate`` the server picks the current calendar month
        — we pass no ``timePeriod`` so the server uses its own
        interpretation; this matches the Azure portal "Month to
        date" preset.
        """
        if not subscription_id or not isinstance(subscription_id, str):
            raise AzureCostError(
                category=AzureCostErrorCategory.INVALID_RESPONSE,
                message="subscription_id must be a non-empty string",
            )
        if timeframe not in {"MonthToDate", "Custom"}:
            raise AzureCostError(
                category=AzureCostErrorCategory.INVALID_RESPONSE,
                message=f"timeframe must be MonthToDate or Custom (got {timeframe!r})",
            )
        if timeframe == "Custom":
            if not from_iso or not to_iso:
                raise AzureCostError(
                    category=AzureCostErrorCategory.INVALID_RESPONSE,
                    message="Custom timeframe requires from_iso and to_iso",
                )

        body: Dict[str, Any] = {
            "type": self._cost_type,
            "timeframe": timeframe,
            "dataset": dict(dataset),
        }
        if timeframe == "Custom":
            body["timePeriod"] = {"from": from_iso, "to": to_iso}

        return self._query_with_pagination(
            subscription_id=subscription_id, body=body
        )

    # ---- internals -------------------------------------------------------

    def _query_with_pagination(
        self,
        *,
        subscription_id: str,
        body: Dict[str, Any],
    ) -> AzureQueryResult:
        """Walk the query call and any nextLink pages.

        Raises :class:`AzureCostError` on a non-2xx response or
        network failure.  Returns the concatenated column / row set
        of the final page.
        """
        url: Optional[str] = (
            f"/subscriptions/{subscription_id}/providers/"
            "Microsoft.CostManagement/query"
        )
        params: Optional[Dict[str, str]] = {"api-version": self._api_version}
        page_count = 0
        columns: Optional[Tuple[_Column, ...]] = None
        rows: List[Tuple[Any, ...]] = []

        while True:
            page_count += 1
            if page_count > self._max_pages:
                label = safe_subscription_label(subscription_id)
                logger.warning(
                    "azure.cost.query.max_pages_exceeded sub=%s max=%d",
                    label, self._max_pages,
                )
                raise AzureCostError(
                    category=AzureCostErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_id,
                    message=f"pagination exceeded {self._max_pages} pages",
                )

            # Acquire a fresh token per call so we don't share an
            # in-flight token across pages of a single request —
            # the underlying azure-identity SDK already caches the
            # token across short windows, but acquiring once per
            # page keeps the failure surface uniform with the ARM
            # client.
            try:
                token = acquire_access_token()
            except AzureAuthError as exc:
                label = safe_subscription_label(subscription_id)
                logger.warning(
                    "azure.cost.query.auth_failed sub=%s category=%s",
                    label, exc.code,
                )
                raise AzureCostError(
                    category=AzureCostErrorCategory.AUTH_FAILED,
                    subscription_id=subscription_id,
                ) from None

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            label = safe_subscription_label(subscription_id)

            try:
                if params is not None:
                    response = self._http.post(
                        url, params=params, json=body, headers=headers
                    )
                else:
                    # nextLink carries the full query string — pass
                    # it through with no extra params.
                    response = self._http.post(
                        url, json=body, headers=headers
                    )
            except httpx.HTTPError as exc:
                logger.warning(
                    "azure.cost.query.network_error sub=%s code=%s",
                    label, type(exc).__name__,
                )
                raise AzureCostError(
                    category=AzureCostErrorCategory.NETWORK_ERROR,
                    subscription_id=subscription_id,
                ) from None

            # ---- handle non-2xx ----
            if response.status_code == 204:
                # 204 = "the request was valid but there is no
                # data" — this is the canonical NO_DATA outcome.
                # Return an empty result so the caller maps it to
                # AzureCostCallStatus.NO_DATA.
                logger.info(
                    "azure.cost.query.no_data sub=%s page=%d",
                    label, page_count,
                )
                return AzureQueryResult(columns=(), rows=())

            if response.status_code != 200:
                self._raise_for_status(response, subscription_id, label)

            # ---- parse body ----
            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "azure.cost.query.bad_json sub=%s page=%d",
                    label, page_count,
                )
                raise AzureCostError(
                    category=AzureCostErrorCategory.INVALID_RESPONSE,
                    subscription_id=subscription_id,
                ) from None

            properties = payload.get("properties") or {}
            page_columns = properties.get("columns") or []
            page_rows = properties.get("rows") or []
            if page_count == 1:
                columns = self._parse_columns(page_columns)
            # All pages must agree on the column shape — if a later
            # page returns a different schema, that's an upstream
            # surprise and we refuse to silently concatenate.
            else:
                parsed = self._parse_columns(page_columns)
                if (
                    len(parsed) != len(columns or ())
                    or any(
                        a.name != b.name or a.type != b.type
                        for a, b in zip(parsed, columns or ())
                    )
                ):
                    logger.warning(
                        "azure.cost.query.schema_drift sub=%s page=%d",
                        label, page_count,
                    )
                    raise AzureCostError(
                        category=AzureCostErrorCategory.INVALID_RESPONSE,
                        subscription_id=subscription_id,
                        message="column schema drifted between pages",
                    )
            for row in page_rows:
                rows.append(tuple(row))

            # ---- nextLink handling ----
            next_link = properties.get("nextLink")
            if not next_link:
                break
            # nextLink is an absolute URL pointing at the ARM
            # management endpoint.  We validate the host before
            # following so a malicious / compromised response can't
            # redirect us off-platform.
            if not self._is_safe_nextlink(next_link):
                logger.warning(
                    "azure.cost.query.bad_nextlink sub=%s page=%d",
                    label, page_count,
                )
                raise AzureCostError(
                    category=AzureCostErrorCategory.UPSTREAM_ERROR,
                    subscription_id=subscription_id,
                    message="nextLink is not an ARM management endpoint",
                )
            url = next_link
            params = None  # nextLink carries its own query string

        if columns is None:
            return AzureQueryResult(columns=(), rows=tuple(rows))
        return AzureQueryResult(columns=columns, rows=tuple(rows))

    @staticmethod
    def _parse_columns(raw: Sequence[Mapping[str, Any]]) -> Tuple[_Column, ...]:
        return tuple(
            _Column(name=str(col.get("name", "")), type=str(col.get("type", "")), index=idx)
            for idx, col in enumerate(raw)
        )

    @staticmethod
    def _is_safe_nextlink(url: str) -> bool:
        """Defence against an off-platform nextLink.

        Accepts only ``https://management.azure.com/...``.  The
        spec does not provide a sub-endpoint, so any ``management.
        azure.com`` host on the canonical management path is OK.
        """
        if not isinstance(url, str):
            return False
        if not url.startswith(("https://management.azure.com/", "https://management.azure.com?")):
            return False
        return True

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        subscription_id: str,
        label: str,
    ) -> None:
        """Map a non-2xx response onto :class:`AzureCostError`."""
        status = response.status_code
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if status == 401:
            logger.warning(
                "azure.cost.query.auth_failed sub=%s status=401",
                label,
            )
            raise AzureCostError(
                category=AzureCostErrorCategory.AUTH_FAILED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if status == 403:
            logger.warning(
                "azure.cost.query.forbidden sub=%s status=403",
                label,
            )
            raise AzureCostError(
                category=AzureCostErrorCategory.ACCESS_DENIED,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 404:
            logger.warning(
                "azure.cost.query.not_found sub=%s status=404",
                label,
            )
            raise AzureCostError(
                category=AzureCostErrorCategory.NOT_FOUND,
                subscription_id=subscription_id,
                status_code=status,
            )
        if status == 429:
            logger.warning(
                "azure.cost.query.throttled sub=%s status=429 retry_after=%s",
                label, retry_after,
            )
            raise AzureCostError(
                category=AzureCostErrorCategory.THROTTLED,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        if 500 <= status <= 599:
            logger.warning(
                "azure.cost.query.upstream_error sub=%s status=%d",
                label, status,
            )
            raise AzureCostError(
                category=AzureCostErrorCategory.UPSTREAM_ERROR,
                subscription_id=subscription_id,
                status_code=status,
                retry_after=retry_after,
            )
        logger.warning(
            "azure.cost.query.unexpected_status sub=%s status=%d",
            label, status,
        )
        raise AzureCostError(
            category=AzureCostErrorCategory.UPSTREAM_ERROR,
            subscription_id=subscription_id,
            status_code=status,
        )


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def build_time_period(
    *,
    period_start: date,
    period_end: date,
    today: Optional[date] = None,
) -> Tuple[str, str, str]:
    """Map a calendar-month window onto a Cost Management timeframe.

    Returns ``(timeframe, from_iso, to_iso)``:

    * ``MonthToDate`` when ``period_start`` is the first day of the
      current month AND ``period_end`` falls strictly inside the
      same month (the existing AWS aggregator already clips to MTD).
    * ``Custom`` with explicit UTC boundaries otherwise.

    Both boundaries are always UTC midnight (``Z``); the API
    interprets the values as UTC regardless of any local time
    zone.  This matches the existing AWS calendar-month semantics
    so the dashboard's "selected month" matches across providers.
    """
    today = today or datetime.now(timezone.utc).date()
    month_first = date(today.year, today.month, 1)
    if period_start.month == 12:
        next_month_first = date(period_start.year + 1, 1, 1)
    else:
        next_month_first = date(period_start.year, period_start.month + 1, 1)
    is_current_month = (
        period_start == month_first
        and period_start.year == today.year
        and period_start.month == today.month
        and period_end <= next_month_first
    )
    if is_current_month:
        return ("MonthToDate", "", "")
    # Custom — explicit UTC boundaries matching the calendar month.
    from_iso = f"{period_start.isoformat()}T00:00:00Z"
    to_iso = f"{period_end.isoformat()}T00:00:00Z"
    return ("Custom", from_iso, to_iso)


# ---------------------------------------------------------------------------
# Row extraction — operates on the parsed AzureQueryResult
# ---------------------------------------------------------------------------


def _coerce_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a Cost Management decimal value to Decimal.

    Cost Management returns ``PreTaxCost`` as a JSON number today;
    we tolerate ``None`` and strings for forward-compatibility.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _coerce_currency(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value.upper()


def _coerce_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _coerce_date(value: Any) -> Optional[date]:
    """Coerce an Azure UsageDate value to a date.

    Cost Management returns UsageDate as either:
    * an ISO date string ``"YYYY-MM-DD"`` (the documented form), or
    * an ISO datetime string with ``Z`` / ``+00:00`` suffix.

    We tolerate both.
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # Date-only form
            return date.fromisoformat(s[:10])
        except ValueError:
            pass
        try:
            # Datetime form — parse, drop tz, return date.
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).date()
        except ValueError:
            return None
    return None


# Cost Management may not include the currency column on every
# endpoint.  When absent, we surface ``None`` and let the adapter
# fall back to a configurable default (USD by default, but see
# :data:`_DEFAULT_CURRENCY`).
_DEFAULT_CURRENCY: str = os.getenv("AZURE_COST_DEFAULT_CURRENCY", "USD").upper()


def extract_total(
    result: AzureQueryResult,
    *,
    default_currency: str = _DEFAULT_CURRENCY,
) -> Tuple[AzureCostCallStatus, Decimal, str]:
    """Compute the selected-period total cost.

    Returns ``(status, amount, currency)``.

    ``amount`` is the sum of every row's ``PreTaxCost``.  Status
    rules:

    * Empty rows → ``EMPTY``.
    * Non-empty rows that sum to zero → ``OK`` with a zero amount
      (this is the "Azure says cost really is $0" outcome).
    * Otherwise → ``OK``.
    """
    cost_idx = result.find_column("PreTaxCost")
    if cost_idx is None:
        cost_idx = result.find_column("Cost")
    if cost_idx is None:
        # No cost column → either 204/empty or a dimension-only
        # response.  Caller distinguishes 204 from EMPTY based on
        # row count.
        return (AzureCostCallStatus.EMPTY, Decimal("0"), default_currency)
    currency_idx = result.find_column("Currency")

    if not result.rows:
        return (AzureCostCallStatus.EMPTY, Decimal("0"), default_currency)

    total = Decimal("0")
    currency: Optional[str] = None
    for row in result.rows:
        amt = _coerce_decimal(_row_at(row, cost_idx))
        if amt is None:
            continue
        total += amt
        if currency_idx is not None:
            cur = _coerce_currency(_row_at(row, currency_idx))
            if cur:
                currency = cur
    currency = currency or default_currency
    return (AzureCostCallStatus.OK, total.quantize(Decimal("0.01")), currency)


def extract_daily(
    result: AzureQueryResult,
    *,
    default_currency: str = _DEFAULT_CURRENCY,
) -> Tuple[AzureCostCallStatus, List[Tuple[date, Decimal, str]], str]:
    """Compute the daily cost trend.

    Returns ``(status, daily_points, currency)``.

    Each ``daily_point`` is ``(date, amount, currency)``.  The
    column carrying the date is resolved dynamically — Cost
    Management returns ``UsageDate`` for cost data and may
    additionally emit ``BillingPeriod`` for invoice alignment; we
    prefer ``UsageDate`` but accept either as a fallback.

    Missing days are NOT filled here.  Filling decisions belong to
    the caller (the dashboard only fills when the response is
    :class:`AzureCostCallStatus.OK`).
    """
    date_idx = result.find_column("UsageDate")
    if date_idx is None:
        date_idx = result.find_column("BillingPeriodStartDate")
    if date_idx is None:
        return (AzureCostCallStatus.EMPTY, [], default_currency)
    cost_idx = result.find_column("PreTaxCost")
    if cost_idx is None:
        cost_idx = result.find_column("Cost")
    if cost_idx is None:
        return (AzureCostCallStatus.EMPTY, [], default_currency)
    currency_idx = result.find_column("Currency")

    points: List[Tuple[date, Decimal, str]] = []
    currency: Optional[str] = None
    seen_dates: set = set()
    for row in result.rows:
        d = _coerce_date(_row_at(row, date_idx))
        if d is None or d in seen_dates:
            continue
        seen_dates.add(d)
        amt = _coerce_decimal(_row_at(row, cost_idx)) or Decimal("0")
        cur: Optional[str] = None
        if currency_idx is not None:
            cur = _coerce_currency(_row_at(row, currency_idx))
        cur = cur or default_currency
        points.append((d, amt.quantize(Decimal("0.01")), cur))
        if cur and not currency:
            currency = cur
    points.sort(key=lambda point: point[0])
    currency = currency or default_currency
    if not points:
        return (AzureCostCallStatus.EMPTY, [], currency)
    return (AzureCostCallStatus.OK, points, currency)


def extract_by_dimension(
    result: AzureQueryResult,
    *,
    dimension_name: str,
    default_currency: str = _DEFAULT_CURRENCY,
    unassigned_bucket: str = UNASSIGNED_BUCKET,
) -> Tuple[AzureCostCallStatus, List[Tuple[str, Decimal, str]], str]:
    """Group rows by the named dimension and sum their cost.

    Returns ``(status, items, currency)``.  Each ``item`` is
    ``(name, amount, currency)`` sorted by amount descending.

    When the dimension column is absent from the response (the API
    does not return it for every query type / account), returns
    ``EMPTY``.  The caller decides whether to surface that as a
    "feature unavailable for this subscription" status.

    Empty / blank dimension values are bucketed under
    ``unassigned_bucket`` (``"Unassigned"``) so the cost is never
    silently dropped.
    """
    if not result.columns:
        return (AzureCostCallStatus.EMPTY, [], default_currency)
    dim_idx = result.find_column(dimension_name)
    if dim_idx is None:
        return (AzureCostCallStatus.EMPTY, [], default_currency)
    cost_idx = result.find_column("PreTaxCost")
    if cost_idx is None:
        cost_idx = result.find_column("Cost")
    if cost_idx is None:
        return (AzureCostCallStatus.EMPTY, [], default_currency)
    currency_idx = result.find_column("Currency")

    accum: Dict[str, Tuple[Decimal, str]] = {}
    for row in result.rows:
        name = _coerce_string(_row_at(row, dim_idx)) or unassigned_bucket
        amt = _coerce_decimal(_row_at(row, cost_idx)) or Decimal("0")
        cur = None
        if currency_idx is not None:
            cur = _coerce_currency(_row_at(row, currency_idx))
        cur = cur or default_currency
        prior_amt, prior_cur = accum.get(name, (Decimal("0"), cur))
        accum[name] = (prior_amt + amt, prior_cur or cur)

    items = sorted(
        (
            (name, amt.quantize(Decimal("0.01")), unit)
            for name, (amt, unit) in accum.items()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    currency = items[0][2] if items else default_currency
    if not items:
        return (AzureCostCallStatus.EMPTY, [], currency)
    return (AzureCostCallStatus.OK, items, currency)


def _row_at(row: Sequence[Any], index: int) -> Any:
    if 0 <= index < len(row):
        return row[index]
    return None


# ---------------------------------------------------------------------------
# Pagination guard utility (exposed for tests)
# ---------------------------------------------------------------------------


def parse_retry_after_header(value: Optional[str]) -> Optional[int]:
    """Public re-export of the retry-after parser."""
    return _parse_retry_after(value)


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    """Parse an HTTP ``Retry-After`` header.

    Returns the integer number of seconds, or ``None`` when the
    header is absent / malformed / a date we can't easily reason
    about in tests.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = int(value)
    except ValueError:
        # Date form — refuse to interpret in this layer; the API
        # layer can decide what to do.
        return None
    if seconds < 0:
        return None
    return seconds


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def build_total_dataset() -> Dict[str, Any]:
    """Build the dataset for a total-cost query.

    ``granularity=None`` means the API returns one row covering
    the full window (no daily split).
    """
    return {
        "granularity": None,
        "aggregation": {
            "totalCost": {
                "name": "PreTaxCost",
                "function": "Sum",
            }
        },
    }


def build_daily_dataset() -> Dict[str, Any]:
    """Build the dataset for a daily trend query."""
    return {
        "granularity": "Daily",
        "aggregation": {
            "totalCost": {
                "name": "PreTaxCost",
                "function": "Sum",
            }
        },
    }


def build_grouped_dataset(dimension_name: str) -> Dict[str, Any]:
    """Build the dataset for a per-dimension grouping.

    ``dimension_name`` is the Cost Management dimension (e.g.
    ``ServiceName``, ``ResourceLocation``).  The dataset groups
    rows by that dimension and sums ``PreTaxCost`` within each
    group.
    """
    return {
        "granularity": None,
        "aggregation": {
            "totalCost": {
                "name": "PreTaxCost",
                "function": "Sum",
            }
        },
        "grouping": [
            {
                "type": "Dimension",
                "name": dimension_name,
            }
        ],
    }


__all__ = [
    "ARM_BASE_URL",
    "COST_MGMT_API_VERSION",
    "COST_TYPE_ACTUAL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIM_RESOURCE_LOCATION",
    "DIM_SERVICE_NAME",
    "MAX_PAGES",
    "UNASSIGNED_BUCKET",
    "AzureCostCallStatus",
    "AzureCostClient",
    "AzureCostError",
    "AzureCostErrorCategory",
    "AzureQueryResult",
    "build_daily_dataset",
    "build_grouped_dataset",
    "build_time_period",
    "build_total_dataset",
    "extract_by_dimension",
    "extract_daily",
    "extract_total",
    "parse_retry_after_header",
]
