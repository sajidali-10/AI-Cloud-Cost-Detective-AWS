"""Rackspace canonical cost normalization — Phase 9B.1.

This module translates raw Rackspace Billing V2
``estimated_charges`` payloads into provider-neutral cost snapshots
ready for the :class:`~app.services.cloud.models.CloudCostSummary`
canonical envelope.  It is the ONLY place where Rackspace charge
data is interpreted — every rule here is deliberately explicit so
no fabricated daily, regional, or previous-period data can leak
into the canonical cost surface.

Canonical source identity
--------------------------

The canonical cost source for Rackspace is::

    RACKSPACE_BILLING

backed by ``GET https://billing.api.rackspacecloud.com/v2/accounts/{ran}/estimated_charges``.

Money handling
--------------

Every monetary value is parsed into :class:`decimal.Decimal` via
``Decimal(str(value))``.  Binary floats never accumulate: a JSON
number such as ``1234.567`` is stringified first so the exact
decimal literal is preserved.  The canonical total is the Decimal
sum of the individual charge records whenever records are present,
which guarantees the service breakdown reconciles to the total
*exactly* (no cent-level drift).  The body-level total Rackspace
reports is used as a cross-check: drift beyond one cent is surfaced
as a warning — never silently absorbed and never fabricated.

Period semantics
----------------

``estimated_charges`` describes the CURRENT Rackspace billing cycle
only.  The normalizer extracts the actual source period from the
response (``currentBillingPeriodStartDate`` / ``currentBillingPeriodEndDate``
or the ``currentPeriod`` object) and refuses to fabricate a period
when the API does not provide one.  Callers that request a window
with ZERO overlap against the source period get a controlled
``PERIOD_UNSUPPORTED`` error instead of silently substituted
current-cycle data.

Dimensions the source does not provide
---------------------------------------

* **Daily trend** — ``estimated_charges`` has no reliable per-day
  granularity; the canonical daily collection stays EMPTY rather
  than deriving fake daily points from the total.
* **Region breakdown** — the response carries no regional
  dimension; the canonical region collection stays EMPTY.
* **Previous period / historical totals** — ``estimated_charges``
  cannot answer historical queries and ``invoices/latest`` alignment
  guarantees are non-trivial; ``previous_total`` stays ``None``
  (``previous_period_unavailable``) and historical support is
  documented as a Phase 9B follow-up.

True-zero semantics
-------------------

A valid HTTP 200 with ``total == 0`` (or an empty charge-record
list totalling zero) is a SUCCESS — never an unavailable state.
Zero cost, an empty account, an API failure, a permission failure,
and missing configuration are five distinct conditions and are
never conflated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceThrottledError,
    RackspaceTransportError,
)


#: Canonical cost source identifier for the Rackspace provider.
RACKSPACE_COST_SOURCE = "RACKSPACE_BILLING"

#: Monetary tolerance (one cent) for the body-total cross-check.
TOTAL_RECONCILIATION_TOLERANCE = Decimal("0.01")

#: Service-name cap so a hostile / malformed offering label cannot
#: blow up the canonical envelope.
_MAX_SERVICE_LABEL_LENGTH = 120

#: Bucket used for charge records that carry no reliable
#: product / offering / category field.  Explicit — never fabricated.
UNCATEGORIZED_SERVICE = "Uncategorized"


class RackspaceCostErrorCategory(str, Enum):
    """Stable failure categories for the canonical Rackspace cost path."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    NOT_FOUND = "NOT_FOUND"
    THROTTLED = "THROTTLED"
    NETWORK_ERROR = "NETWORK_ERROR"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PERIOD_UNSUPPORTED = "PERIOD_UNSUPPORTED"


class RackspaceCostError(Exception):
    """A sanitized, typed failure from the canonical Rackspace cost path.

    ``message`` never contains credentials, X-Auth-Token values,
    the RAN, or raw response bodies — only stable category
    semantics and sanitized diagnostics.
    """

    def __init__(
        self,
        category: RackspaceCostErrorCategory,
        message: str,
        *,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class RackspaceServiceCost:
    """A single normalized service/category total."""

    service: str
    amount: Decimal


@dataclass(frozen=True)
class RackspaceCostSnapshot:
    """The normalized current-cycle cost snapshot for one Rackspace account.

    ``period_start`` / ``period_end`` are the ACTUAL source period
    dates as returned by the Rackspace Billing API (exposed verbatim
    in the canonical envelope).  ``total`` is the Decimal sum of the
    charge records when records were present (exact
    reconciliation with ``by_service``), otherwise the body-level
    total.  ``warnings`` carries sanitized, non-secret limitation
    notes (uncategorized records, body-total drift, ...).
    """

    currency: str
    period_start: date
    period_end: date
    total: Decimal
    by_service: Tuple[RackspaceServiceCost, ...] = ()
    estimated: bool = True
    record_count: int = 0
    body_total: Optional[Decimal] = None
    warnings: Tuple[str, ...] = ()
    extensions: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------


def translate_rackspace_exception(exc: RackspaceError) -> RackspaceCostError:
    """Translate a transport-layer exception into the canonical error.

    The transport layer (:mod:`app.services.rackspace.client`) already
    sanitizes its messages — no credentials, tokens, or raw bodies.
    We only attach the stable canonical category.
    """

    # NB: RackspaceThrottledError subclasses RackspaceTransportError,
    # so the isinstance check MUST come first.
    if isinstance(exc, RackspaceThrottledError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.THROTTLED,
            "Rackspace Billing throttled the request",
            retry_after=exc.retry_after,
        )
    if isinstance(exc, RackspaceAuthError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.AUTH_FAILED,
            "Rackspace rejected the authentication credentials",
        )
    if isinstance(exc, RackspacePermissionError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.ACCESS_DENIED,
            "Rackspace denied access to the Billing account",
        )
    if isinstance(exc, RackspaceNotFoundError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.NOT_FOUND,
            "Rackspace Billing account is invalid or inaccessible",
        )
    if isinstance(exc, RackspaceConfigError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.NOT_CONFIGURED,
            "Rackspace is not configured",
        )
    if isinstance(exc, RackspaceTransportError):
        return RackspaceCostError(
            RackspaceCostErrorCategory.NETWORK_ERROR,
            "Rackspace Billing is unreachable or returned a malformed transport response",
        )
    return RackspaceCostError(
        RackspaceCostErrorCategory.NETWORK_ERROR,
        "Rackspace cost fetch failed",
    )


# ---------------------------------------------------------------------------
# Payload parsing helpers
# ---------------------------------------------------------------------------


#: Keys under which a charge-record list may appear (documented
#: Rackspace Billing V2 shapes observed in Phase 9A).
_RECORD_LIST_KEYS = ("estimatedCharge", "estimatedCharges", "charges", "lineItems")

#: Keys that carry a reliable product / offering / category label on a
#: charge record — in precedence order.  Only these documented fields
#: are used for the service breakdown; no positional inference.
_PRODUCT_FIELD_KEYS = (
    "service",
    "offeringCode",
    "product",
    "category",
    "productName",
    "offering",
)

#: Keys that carry the body-level total.
_TOTAL_FIELD_KEYS = (
    "chargeTotal",
    "estimatedTotal",
    "total",
    "currentCharges",
    "amount",
)

_CURRENCY_FIELD_KEYS = ("currency", "currencyCode")


def _unwrap_envelope(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Any]]:
    """Return ``(scalar_source, records)`` from the response body.

    Rackspace Billing V2 wraps some payloads in a single
    ``estimatedCharges`` object.  The normalizer accepts both the
    flat shape and the wrapped shape: scalar fields (currency,
    period, totals) are looked up in the top-level body FIRST and
    the wrapped object second; the record list may live at either
    level.
    """

    wrapped = body.get("estimatedCharges")
    scalar_source = body
    if isinstance(wrapped, dict):
        # Prefer top-level scalars; fall back to the wrapped object.
        merged: Dict[str, Any] = dict(wrapped)
        for key in (
            "currency",
            "currencyCode",
            "currentBillingPeriodStartDate",
            "currentBillingPeriodEndDate",
        ) + _TOTAL_FIELD_KEYS + _CURRENCY_FIELD_KEYS:
            if key in body and key not in merged:
                merged[key] = body[key]
        for period_key in ("currentPeriod", "period"):
            if isinstance(body.get(period_key), dict):
                merged.setdefault(period_key, body[period_key])
        scalar_source = merged

    records: Optional[Any] = None
    for key in _RECORD_LIST_KEYS:
        value = body.get(key)
        if isinstance(value, list):
            records = value
            break
    if records is None and isinstance(wrapped, dict):
        for key in _RECORD_LIST_KEYS:
            value = wrapped.get(key)
            if isinstance(value, list):
                records = value
                break
    # A wrapped dict that IS the record list itself (single object).
    if records is None and isinstance(wrapped, dict):
        records = None  # not a list — ignore.
    return scalar_source, records


def _parse_decimal(value: Any) -> Optional[Decimal]:
    """Parse a JSON number / numeric string into a Decimal, or None."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # str() of a float preserves the decimal literal JSON gave us
        # without binary-float accumulation.
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _parse_source_date(value: Any) -> Optional[date]:
    """Parse a Rackspace ISO-8601 date / datetime into a ``date``."""

    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    # Date-only first (``2024-09-01``).
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        return None


def _extract_period(
    scalar_source: Dict[str, Any],
) -> Optional[Tuple[date, date]]:
    """Extract the actual source billing period, or ``None``.

    Accepted shapes (documented Billing V2 fields):

    * ``currentBillingPeriodStartDate`` / ``currentBillingPeriodEndDate``
    * ``currentPeriod`` / ``period`` object with ``start`` / ``end``
      (or ``periodStart`` / ``periodEnd``).
    """

    start = _parse_source_date(scalar_source.get("currentBillingPeriodStartDate"))
    end = _parse_source_date(scalar_source.get("currentBillingPeriodEndDate"))
    if start is not None and end is not None:
        return (start, end)
    for period_key in ("currentPeriod", "period"):
        period = scalar_source.get(period_key)
        if not isinstance(period, dict):
            continue
        start = _parse_source_date(
            period.get("start") or period.get("periodStart")
        )
        end = _parse_source_date(period.get("end") or period.get("periodEnd"))
        if start is not None and end is not None:
            return (start, end)
    return None


def _extract_currency(
    scalar_source: Dict[str, Any],
    records: Optional[List[Any]],
) -> Optional[str]:
    """Extract the billing currency, or ``None``.

    The currency is NEVER assumed — the live POC returns USD but the
    code must not hardcode it.  Precedence: body-level currency field,
    then the single distinct currency across all charge records.
    """

    for key in _CURRENCY_FIELD_KEYS:
        value = scalar_source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    if isinstance(records, list) and records:
        currencies = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            for key in _CURRENCY_FIELD_KEYS:
                value = record.get(key)
                if isinstance(value, str) and value.strip():
                    currencies.add(value.strip().upper())
        if len(currencies) == 1:
            return currencies.pop()
    return None


def _extract_body_total(scalar_source: Dict[str, Any]) -> Optional[Decimal]:
    """Extract the body-level total, or ``None``."""

    for key in _TOTAL_FIELD_KEYS:
        if key not in scalar_source:
            continue
        parsed = _parse_decimal(scalar_source.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_amount(record: Dict[str, Any]) -> Decimal:
    """Return the record's charge amount.

    Raises :class:`RackspaceCostError` (INVALID_RESPONSE) when a
    charge record carries no usable amount — the total cannot be
    computed without fabricating a value.
    """

    for key in ("amount", "charge", "chargeAmount", "cost", "total"):
        parsed = _parse_decimal(record.get(key))
        if parsed is not None:
            return parsed
    raise RackspaceCostError(
        RackspaceCostErrorCategory.INVALID_RESPONSE,
        "estimated_charges record is missing a usable amount field",
    )


def _record_service_label(record: Dict[str, Any]) -> Optional[str]:
    """Return the record's product / offering / category label, if any.

    Only documented, reliable product fields are consulted.  When no
    field is present the caller buckets the record under
    :data:`UNCATEGORIZED_SERVICE` — an explicit label, never an
    inferred category.
    """

    for key in _PRODUCT_FIELD_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            label = " ".join(value.strip().split())
            return label[:_MAX_SERVICE_LABEL_LENGTH]
    return None


# ---------------------------------------------------------------------------
# Normalization entry point
# ---------------------------------------------------------------------------


def normalize_estimated_charges(body: Dict[str, Any]) -> RackspaceCostSnapshot:
    """Normalize a Billing V2 ``estimated_charges`` payload.

    Pure function — no I/O, no clock, no configuration reads.
    Raises :class:`RackspaceCostError` with category
    ``INVALID_RESPONSE`` for structurally unusable payloads
    (missing currency / period / any usable total) and with
    ``PERIOD_UNSUPPORTED`` never (period overlap is checked by the
    caller via :func:`assert_period_overlap`).
    """

    if not isinstance(body, dict):
        raise RackspaceCostError(
            RackspaceCostErrorCategory.INVALID_RESPONSE,
            "estimated_charges response is not a JSON object",
        )

    scalar_source, raw_records = _unwrap_envelope(body)
    records: List[Dict[str, Any]] = [
        r for r in (raw_records or []) if isinstance(r, dict)
    ]
    record_count = len(records)

    # --- Currency (never assumed) ------------------------------------
    currency = _extract_currency(scalar_source, records)
    if not currency:
        raise RackspaceCostError(
            RackspaceCostErrorCategory.INVALID_RESPONSE,
            "estimated_charges response does not provide a currency",
        )

    # --- Actual source period (never fabricated) ----------------------
    period = _extract_period(scalar_source)
    if period is None:
        raise RackspaceCostError(
            RackspaceCostErrorCategory.INVALID_RESPONSE,
            "estimated_charges response does not provide a billing period",
        )
    period_start, period_end = period
    if period_end <= period_start:
        # Exclusive-end semantics: the period must span at least one
        # full day (``start`` inclusive, ``end`` exclusive).
        raise RackspaceCostError(
            RackspaceCostErrorCategory.INVALID_RESPONSE,
            "estimated_charges billing period is empty or reversed",
        )

    # --- Service breakdown + canonical total --------------------------
    warnings: List[str] = []
    body_total = _extract_body_total(scalar_source)

    if record_count > 0:
        service_totals: Dict[str, Decimal] = {}
        uncategorized = 0
        record_sum = Decimal("0")
        for record in records:
            amount = _record_amount(record)
            record_sum += amount
            label = _record_service_label(record)
            if label is None:
                label = UNCATEGORIZED_SERVICE
                uncategorized += 1
            service_totals[label] = service_totals.get(label, Decimal("0")) + amount
        if uncategorized:
            warnings.append(
                f"uncategorized_charge_records={uncategorized}"
            )
        total = record_sum
        if body_total is not None:
            drift = abs(body_total - total)
            if drift > TOTAL_RECONCILIATION_TOLERANCE:
                warnings.append(
                    f"body_total_mismatch={drift:.2f}"
                )
    else:
        # No charge records.  The body-level total is the only source —
        # a legitimate empty/zero result when the API reports one.
        if body_total is None:
            raise RackspaceCostError(
                RackspaceCostErrorCategory.INVALID_RESPONSE,
                "estimated_charges response has no records and no total",
            )
        total = body_total
        warnings.append("no_charge_records_total_from_body")

    by_service = tuple(
        RackspaceServiceCost(service=name, amount=amount)
        for name, amount in sorted(
            service_totals.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ) if record_count > 0 else tuple()

    # Guard: the breakdown must reconcile with the canonical total
    # exactly whenever records were aggregated.
    if record_count > 0:
        breakdown_sum = sum((sc.amount for sc in by_service), Decimal("0"))
        if breakdown_sum != total:
            raise RackspaceCostError(
                RackspaceCostErrorCategory.INVALID_RESPONSE,
                "service breakdown does not reconcile with the canonical total",
            )

    return RackspaceCostSnapshot(
        currency=currency,
        period_start=period_start,
        period_end=period_end,
        total=total,
        by_service=by_service,
        estimated=True,
        record_count=record_count,
        body_total=body_total,
        warnings=tuple(warnings),
        extensions={
            "source": RACKSPACE_COST_SOURCE,
            "has_records": record_count > 0,
        },
    )


# ---------------------------------------------------------------------------
# Period support (overlap rule)
# ---------------------------------------------------------------------------


def assert_period_overlap(
    *,
    requested_start: date,
    requested_end: date,
    snapshot: RackspaceCostSnapshot,
) -> None:
    """Raise ``PERIOD_UNSUPPORTED`` unless the requested window overlaps
    the actual source billing cycle.

    The Rackspace Billing V2 ``estimated_charges`` endpoint answers
    ONLY for the current billing cycle.  Rather than silently
    substituting current-cycle data for a historical/custom request,
    we fail with an explicit, controlled limitation.  A requested
    window that overlaps the source period (even partially) is
    served with the ACTUAL source period exposed in the envelope.
    """

    if requested_end <= snapshot.period_start or requested_start >= snapshot.period_end:
        raise RackspaceCostError(
            RackspaceCostErrorCategory.PERIOD_UNSUPPORTED,
            (
                "Rackspace Billing supports only the current billing cycle "
                f"({snapshot.period_start.isoformat()} to "
                f"{snapshot.period_end.isoformat()} exclusive); the requested window "
                "does not overlap it"
            ),
        )


# ---------------------------------------------------------------------------
# Fetch + normalize convenience
# ---------------------------------------------------------------------------


async def fetch_cost_snapshot(billing_client: Any) -> RackspaceCostSnapshot:
    """Fetch ``estimated_charges`` and normalize it into a snapshot.

    Translates every transport-layer exception into the canonical
    :class:`RackspaceCostError`.  Never returns fabricated data and
    never converts an API failure into ``$0``.
    """

    try:
        body = await billing_client.fetch_estimated_charges()
    except RackspaceError as exc:
        raise translate_rackspace_exception(exc) from exc
    return normalize_estimated_charges(body)


__all__ = [
    "RACKSPACE_COST_SOURCE",
    "RackspaceCostError",
    "RackspaceCostErrorCategory",
    "RackspaceCostSnapshot",
    "RackspaceServiceCost",
    "assert_period_overlap",
    "fetch_cost_snapshot",
    "normalize_estimated_charges",
    "translate_rackspace_exception",
]
