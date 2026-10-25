"""Rackspace Billing client — Phase 9A POC (read-only).

Per Rackspace's official documentation the Billing API is
intentionally NOT exposed through the Identity service catalog.
The Identity ``access.serviceCatalog`` for a Rackspace tenant
will not contain ``cloudBilling`` (or any of its legacy
identifiers) regardless of whether the account is enabled for
Billing.

Instead the Billing API is reachable at the *documented* base
endpoint:

    https://billing.api.rackspacecloud.com

and every path is scoped to the Rackspace Account Number (RAN)
— a *separate* identifier from the Cloud API tenant / customer
account number.  The RAN is supplied via the optional
``RACKSPACE_BILLING_ACCOUNT`` environment variable and is
NEVER logged.  The client fails closed when the RAN is absent.

The supported Billing API V2 endpoints exercised by the POC are:

    GET /v2/accounts/{ran}
    GET /v2/accounts/{ran}/currency
    GET /v2/accounts/{ran}/billing-summary
    GET /v2/accounts/{ran}/invoices/latest
    GET /v2/accounts/{ran}/estimated_charges

Only ``GET`` requests are issued.  Every method returns a
sanitized :class:`BillingEndpointResult` that captures the HTTP
status, a coarse record count, and a *summary* of the response
— never the raw record bodies.  Raw invoice lines can contain
customer-sensitive PII / addresses; the POC contract is "no raw
billing records in logs / error messages".

A missing RAN (or empty RAN) is reported as
``status="NOT_CONFIGURED"`` so callers do not silently call the
Billing API with an empty path.  A genuine 401 / 403 / 404 from
the Billing API is surfaced as the corresponding status so the
operator can see exactly what the Billing service returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.rackspace.client import RackspaceHttpClient
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import RackspaceIdentity


#: Documented Billing API base endpoint.  Per Rackspace documentation
#: this is the canonical Billing base URL — it is intentionally NOT
#: published in the Identity service catalog.
BILLING_BASE_URL = "https://billing.api.rackspacecloud.com"

#: Billing API version.  Phase 9A targets V2 which is the documented
#: production version at the time of writing.
BILLING_API_VERSION = "v2"


@dataclass(frozen=True)
class BillingEndpointResult:
    """Sanitized outcome of one Billing API probe.

    Attributes
    ----------
    endpoint:
        Logical endpoint name (e.g. ``"billing_account"``,
        ``"billing_currency"``, ``"billing_estimated_charges"``,
        ``"billing_billing_summary"``, ``"billing_invoice_latest"``).
    http_status:
        HTTP status code returned by Rackspace, or ``None`` when
        no request was issued (e.g. RAN not configured, Identity
        authentication unavailable).
    status:
        Coarse outcome label — one of:

        * ``"OK"`` — HTTP 200 with a JSON body.
        * ``"AUTH_FAILED"`` — HTTP 401 (token rejected).
        * ``"PERMISSION_DENIED"`` — HTTP 403 (Billing role
          insufficient or not granted on this account).
        * ``"NOT_FOUND"`` — HTTP 404 (invalid RAN / Billing not
          available on this account).
        * ``"NOT_CONFIGURED"`` — RAN is missing or empty; no
          request was issued.
        * ``"TRANSPORT_ERROR"`` — network / parse failure.
    summary:
        Sanitized summary fields (record counts, date ranges,
        currency codes, sanitized current-cost figures).  Never
        contains raw customer billing record fields, full invoice
        lines, addresses, or payment instruments.
    detail:
        Optional human-readable diagnostic message; must not
        include credentials, tokens, or raw record bodies.
    """

    endpoint: str
    http_status: Optional[int]
    status: str
    summary: Dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK"


class RackspaceBillingClient:
    """Read-only client for the Rackspace Billing API (V2).

    The base URL is hard-coded to :data:`BILLING_BASE_URL` per
    Rackspace's documentation — the Billing service is NOT
    reachable through the Identity service catalog.  The RAN
    (``RACKSPACE_BILLING_ACCOUNT``) is read from settings at
    every call so a tenant can rotate the value without
    restarting the application.

    The client only ever issues ``GET`` requests.  The HTTP
    client itself has no ``PUT`` / ``PATCH`` / ``DELETE``
    methods so this guarantee is enforced at the transport
    layer.
    """

    def __init__(
        self,
        *,
        http_client: RackspaceHttpClient,
        identity: RackspaceIdentity,
    ) -> None:
        self._http = http_client
        self._identity = identity

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def probe_all(self) -> List[BillingEndpointResult]:
        """Probe the documented Billing endpoints in safe order.

        Order is deliberately chosen so the operator sees a
        working account lookup and currency before any
        sensitive summary or invoice endpoint:

        1. ``GET /v2/accounts/{ran}`` — account lookup.
        2. ``GET /v2/accounts/{ran}/currency`` — currency.
        3. ``GET /v2/accounts/{ran}/estimated_charges`` —
           current / estimated charges (sanitized).
        4. ``GET /v2/accounts/{ran}/billing-summary`` —
           billing summary metadata (no raw invoice dump).
        5. ``GET /v2/accounts/{ran}/invoices/latest`` —
           latest invoice metadata (no full invoice body).

        The method never raises; it returns one
        :class:`BillingEndpointResult` per probed endpoint.
        """

        return [
            await self.get_account(),
            await self.get_currency(),
            await self.get_estimated_charges(),
            await self.get_billing_summary(),
            await self.get_latest_invoice_metadata(),
        ]

    async def get_account(self) -> BillingEndpointResult:
        """``GET /v2/accounts/{ran}`` — billing account metadata.

        Returns the account object which contains the currency
        and basic account state.  Raw contact fields (email,
        address) are projected to a single
        ``billing_account_identifier_present`` flag — the values
        are never included in the summary.
        """

        return await self._safe_get(
            endpoint="billing_account",
            path_suffix="",
            summary_kind="account",
        )

    async def get_currency(self) -> BillingEndpointResult:
        """``GET /v2/accounts/{ran}/currency`` — billing currency."""

        return await self._safe_get(
            endpoint="billing_currency",
            path_suffix="/currency",
            summary_kind="currency",
        )

    async def get_estimated_charges(self) -> BillingEndpointResult:
        """``GET /v2/accounts/{ran}/estimated_charges``.

        The response is summarised to a sanitized
        ``{currency, current_period: {start, end}, totals}``
        shape; raw per-resource usage data is never included.
        """

        return await self._safe_get(
            endpoint="billing_estimated_charges",
            path_suffix="/estimated_charges",
            summary_kind="estimated_charges",
        )

    async def get_billing_summary(self) -> BillingEndpointResult:
        """``GET /v2/accounts/{ran}/billing-summary``."""

        return await self._safe_get(
            endpoint="billing_billing_summary",
            path_suffix="/billing-summary",
            summary_kind="billing_summary",
        )

    async def get_latest_invoice_metadata(self) -> BillingEndpointResult:
        """``GET /v2/accounts/{ran}/invoices/latest``.

        Returns sanitized metadata only — invoice id, period,
        status, total, currency.  The raw invoice line items
        are never included in the summary.
        """

        return await self._safe_get(
            endpoint="billing_invoice_latest",
            path_suffix="/invoices/latest",
            summary_kind="latest_invoice",
        )

    # ------------------------------------------------------------------
    # Phase 9B.1 — canonical cost path
    # ------------------------------------------------------------------

    async def fetch_estimated_charges(self) -> Dict[str, Any]:
        """``GET /v2/accounts/{ran}/estimated_charges`` — raw payload.

        Unlike the Phase 9A probe methods (which return sanitized
        :class:`BillingEndpointResult` summaries), this method returns
        the *parsed JSON body* so the canonical cost layer can
        normalise real charge records into the provider-neutral cost
        envelope.  The body never contains credentials — it is the
        Rackspace charge data itself.

        Failure semantics (typed, never swallowed):

        * :class:`RackspaceConfigError` — the RAN is missing.
        * :class:`RackspaceAuthError` — Identity rejected the
          credentials / token (one re-authentication retry is
          performed on HTTP 401 before giving up).
        * :class:`RackspacePermissionError` — HTTP 403.
        * :class:`RackspaceNotFoundError` — HTTP 404 (invalid or
          inaccessible Billing Account / RAN).
        * :class:`RackspaceThrottledError` — HTTP 429 (carries
          ``retry_after`` when Rackspace sent a usable header).
        * :class:`RackspaceTransportError` — timeout, network
          failure, 5xx, or a non-JSON body.

        This method NEVER fabricates data: an empty ``{}`` body with
        HTTP 200 is returned verbatim for the normalizer to classify.
        """

        ran = self._read_ran()
        if not ran:
            raise RackspaceConfigError(
                "RACKSPACE_BILLING_ACCOUNT (RAN) is not configured"
            )

        path = f"/{BILLING_API_VERSION}/accounts/{ran}/estimated_charges"

        # A 401 from the Billing API may mean the cached token
        # expired between the Identity check and the product call.
        # Drop the cached session and retry exactly ONCE with a fresh
        # token before surfacing the authentication failure.
        for attempt in range(2):
            try:
                response = await self._http.get(
                    BILLING_BASE_URL,
                    path,
                    token_provider=self._identity.current_token,
                )
                return response.body
            except RackspaceAuthError:
                if attempt == 0:
                    await self._identity.invalidate()
                    continue
                raise
        # Unreachable — the loop either returns or raises.
        raise RackspaceTransportError(  # pragma: no cover - defensive
            "estimated_charges fetch exhausted retries"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _safe_get(
        self,
        *,
        endpoint: str,
        path_suffix: str,
        summary_kind: str,
    ) -> BillingEndpointResult:
        ran = self._read_ran()
        if not ran:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="NOT_CONFIGURED",
                summary={},
                detail="RACKSPACE_BILLING_ACCOUNT (RAN) is not configured",
            )

        try:
            session = await self._identity.current_session()
        except RackspaceConfigError as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="AUTH_FAILED",
                summary={},
                detail=exc.message,
            )
        except RackspaceAuthError as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="AUTH_FAILED",
                summary={},
                detail=exc.message,
            )

        path = f"/{BILLING_API_VERSION}/accounts/{ran}{path_suffix}"
        token_provider = self._identity.current_token

        try:
            response = await self._http.get(
                BILLING_BASE_URL, path, token_provider=token_provider,
            )
        except RackspaceAuthError as exc:
            await self._identity.invalidate()
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=401,
                status="AUTH_FAILED",
                summary={},
                detail=exc.message,
            )
        except RackspacePermissionError as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=403,
                status="PERMISSION_DENIED",
                summary={},
                detail=exc.message,
            )
        except RackspaceNotFoundError as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=404,
                status="NOT_FOUND",
                summary={},
                detail=exc.message,
            )
        except RackspaceTransportError as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="TRANSPORT_ERROR",
                summary={},
                detail=exc.message,
            )

        summary = _summarise_billing_response(
            response.body, kind=summary_kind,
        )
        return BillingEndpointResult(
            endpoint=endpoint,
            http_status=response.status_code,
            status="OK",
            summary=summary,
            detail="",
        )

    @staticmethod
    def _read_ran() -> str:
        """Return the stripped RAN from settings, or ``""`` if absent.

        The RAN is read on every call so a rotation in the
        operator's secret store is observed without restarting
        the application.
        """

        from app.core.config import get_settings

        try:
            settings = get_settings()
        except Exception:  # pragma: no cover - defensive
            return ""
        return (getattr(settings, "rackspace_billing_account", "") or "").strip()


def _summarise_billing_response(
    body: Dict[str, Any], *, kind: str,
) -> Dict[str, Any]:
    """Produce a sanitized summary of a Billing V2 response.

    The Billing API wraps every endpoint payload in a single
    envelope key (``billingAccount``, ``estimatedCharges``,
    ``billingSummary``, ``invoice``).  The summarizer unwraps
    the envelope before projecting the per-kind allow-list of
    safe fields.

    The summary is restricted to:

    * the account id (presence only, not the value),
    * currency codes,
    * period dates,
    * sanitized total amounts (rounded to two decimals, no
      per-line breakdown),
    * record counts for list-shaped responses.

    Raw customer contact fields, invoice line items, addresses,
    payment instruments, and tax breakdowns are NEVER included.
    """

    if not isinstance(body, dict):
        return {"response_kind": kind}

    # Unwrap the single-key envelope produced by the Billing
    # API.  We accept either the documented envelope key or a
    # flat shape so the summarizer keeps working if Rackspace
    # adds a new envelope in a future release.
    envelope_keys = {
        "currency": ("billingAccount",),
        "estimated_charges": ("estimatedCharges",),
        "billing_summary": ("billingSummary",),
        "latest_invoice": ("invoice",),
        "account": ("billingAccount",),
    }
    unwrapped = body
    for envelope_key in envelope_keys.get(kind, ()):  # type: ignore[arg-type]
        if envelope_key in body and isinstance(body[envelope_key], dict):
            unwrapped = body[envelope_key]
            break

    if kind == "currency":
        return _summarise_currency(unwrapped)
    if kind == "estimated_charges":
        return _summarise_estimated_charges(unwrapped)
    if kind == "billing_summary":
        return _summarise_billing_summary(unwrapped)
    if kind == "latest_invoice":
        return _summarise_latest_invoice(unwrapped)
    if kind == "account":
        return _summarise_account(unwrapped)
    return _summarise_object(unwrapped, allow_keys=())


def _summarise_account(body: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    # Project the currency code only — never the account number
    # or contact details.
    currency = body.get("currency") or body.get("defaultCurrency")
    if isinstance(currency, str):
        summary["currency"] = currency.upper()
    account_id = body.get("accountNumber") or body.get("id")
    summary["billing_account_identifier_present"] = bool(account_id)
    # Status / state if present.
    state = body.get("status") or body.get("accountStatus") or body.get("state")
    if isinstance(state, str):
        summary["status"] = state.upper()
    # Whether the account is configured for current-period billing.
    if "currentPeriod" in body and isinstance(body["currentPeriod"], dict):
        period = body["currentPeriod"]
        period_summary: Dict[str, Any] = {}
        for key in ("start", "end", "periodStart", "periodEnd"):
            value = period.get(key)
            if isinstance(value, str):
                period_summary[key] = value
        if period_summary:
            summary["current_period"] = period_summary
            summary["current_period_available"] = True
    return summary


def _summarise_currency(body: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("currency", "code", "isoCurrency"):
        value = body.get(key)
        if isinstance(value, str):
            summary["currency"] = value.upper()
            return summary
    return summary


def _summarise_estimated_charges(body: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    # Currency is always projected.
    for key in ("currency", "currencyCode"):
        value = body.get(key)
        if isinstance(value, str):
            summary["currency"] = value.upper()
            break

    # The Billing API documents the period as two top-level
    # ISO-8601 strings on the ``estimatedCharges`` envelope.
    period_start = body.get("currentBillingPeriodStartDate")
    period_end = body.get("currentBillingPeriodEndDate")
    if isinstance(period_start, str) or isinstance(period_end, str):
        period_summary: Dict[str, Any] = {}
        if isinstance(period_start, str):
            period_summary["start"] = period_start
        if isinstance(period_end, str):
            period_summary["end"] = period_end
        if period_summary:
            summary["current_period"] = period_summary
            summary["current_period_available"] = True
    for period_key in ("currentPeriod", "period"):
        period = body.get(period_key)
        if isinstance(period, dict):
            period_summary = {}
            for key in ("start", "end", "periodStart", "periodEnd"):
                value = period.get(key)
                if isinstance(value, str):
                    period_summary[key] = value
            if period_summary and "current_period" not in summary:
                summary["current_period"] = period_summary
                summary["current_period_available"] = True

    # Sanitized total — the authoritative envelope ``total`` first
    # (the documented bottom-line estimated total of the current
    # cycle), then the component totals.  Deterministic: the FIRST
    # parseable key wins, mirroring the canonical normalizer's
    # :data:`_TOTAL_FIELD_KEYS` precedence.  Only the rounded numeric
    # total is retained, never the per-resource breakdown.
    for total_key in ("total", "chargeTotal", "estimatedTotal", "currentCharges", "amount"):
        value = body.get(total_key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            summary["total"] = round(float(value), 2)
            summary["sanitized_current_cost_available"] = True
            break
        if isinstance(value, str):
            try:
                summary["total"] = round(float(value), 2)
                summary["sanitized_current_cost_available"] = True
            except ValueError:
                continue
            break

    # List-shaped responses (e.g. ``estimatedCharge: [...]``)
    # are summarised by count + currency set + date range.  Per-resource
    # amount / offeringCode / chargeType fields are NEVER
    # included — only the sanitized aggregates are recorded.
    for list_key in ("estimatedCharge", "estimatedCharges", "charges", "lineItems"):
        records = body.get(list_key)
        if isinstance(records, list):
            summary["record_count"] = len(records)
            summary["list_key"] = list_key
            currencies: set = set()
            dates: list = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                cur = record.get("currency")
                if isinstance(cur, str):
                    currencies.add(cur.upper())
                for date_key in ("date", "period", "startDate", "endDate"):
                    value = record.get(date_key)
                    if isinstance(value, str):
                        dates.append(value)
            if currencies:
                summary["currencies"] = sorted(currencies)
            if dates:
                dates_sorted = sorted(dates)
                summary["date_range"] = {
                    "earliest": dates_sorted[0],
                    "latest": dates_sorted[-1],
                }
            break

    return summary


def _summarise_billing_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("currency", "currencyCode"):
        value = body.get(key)
        if isinstance(value, str):
            summary["currency"] = value.upper()
            break
    for total_key in ("balance", "currentBalance", "outstandingBalance", "total"):
        value = body.get(total_key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            summary["total"] = round(float(value), 2)
            summary["sanitized_current_cost_available"] = True
            break
        if isinstance(value, str):
            try:
                summary["total"] = round(float(value), 2)
                summary["sanitized_current_cost_available"] = True
            except ValueError:
                continue
    period = body.get("billingPeriod") or body.get("currentPeriod")
    if isinstance(period, dict):
        period_summary: Dict[str, Any] = {}
        for key in ("start", "end"):
            value = period.get(key)
            if isinstance(value, str):
                period_summary[key] = value
        if period_summary:
            summary["current_period"] = period_summary
    return summary


def _summarise_latest_invoice(body: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in ("id", "invoiceNumber", "invoiceId"):
        value = body.get(key)
        if value:
            # Truncate to a non-PII preview (first 8 chars + '...').
            text = str(value)
            summary["invoice_id_preview"] = (
                text[:8] + "..." if len(text) > 8 else text
            )
            break
    for key in ("currency", "currencyCode"):
        value = body.get(key)
        if isinstance(value, str):
            summary["currency"] = value.upper()
            break
    for total_key in ("total", "amountDue", "totalAmount", "invoiceTotal"):
        value = body.get(total_key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            summary["total"] = round(float(value), 2)
            summary["sanitized_current_cost_available"] = True
            break
        if isinstance(value, str):
            try:
                summary["total"] = round(float(value), 2)
                summary["sanitized_current_cost_available"] = True
            except ValueError:
                continue
    status = body.get("status")
    if isinstance(status, str):
        summary["status"] = status.upper()
    period = body.get("period") or body.get("invoicePeriod")
    if isinstance(period, dict):
        period_summary: Dict[str, Any] = {}
        for key in ("start", "end"):
            value = period.get(key)
            if isinstance(value, str):
                period_summary[key] = value
        if period_summary:
            summary["current_period"] = period_summary
    # The Billing API also exposes the coverage window directly
    # on the invoice envelope.
    coverage_start = body.get("coverageStartDate")
    coverage_end = body.get("coverageEndDate")
    if isinstance(coverage_start, str) or isinstance(coverage_end, str):
        period_summary = summary.get("current_period") or {}
        if isinstance(coverage_start, str):
            period_summary["start"] = coverage_start
        if isinstance(coverage_end, str):
            period_summary["end"] = coverage_end
        if period_summary:
            summary["current_period"] = period_summary
    return summary


def _summarise_records(
    records: List[Dict[str, Any]], *, list_key: str,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "record_count": len(records),
        "list_key": list_key,
    }
    currencies: set = set()
    dates: list = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("currency", "currencyCode"):
            value = record.get(key)
            if isinstance(value, str):
                currencies.add(value.upper())
        for key in ("date", "period", "startDate", "endDate"):
            value = record.get(key)
            if isinstance(value, str):
                dates.append(value)
    if currencies:
        summary["currencies"] = sorted(currencies)
    if dates:
        dates_sorted = sorted(dates)
        summary["date_range"] = {
            "earliest": dates_sorted[0],
            "latest": dates_sorted[-1],
        }
    return summary


def _summarise_object(
    body: Dict[str, Any], *, allow_keys: tuple,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in allow_keys:
        if key in body:
            summary[key] = body[key]
    return summary


__all__ = [
    "BILLING_API_VERSION",
    "BILLING_BASE_URL",
    "BillingEndpointResult",
    "RackspaceBillingClient",
]
