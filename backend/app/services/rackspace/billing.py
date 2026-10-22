"""Rackspace Billing client — Phase 9A POC (read-only).

Exposes a narrow, sanitized surface over the Rackspace Billing API.
Only ``GET`` requests are issued.  The following endpoints are
probed, each in its own method:

* ``GET /v1/billing`` — billing account metadata.
* ``GET /v1/billing/payments`` — historical payment records.
* ``GET /v1/billing/invoices`` — invoice list / metadata.

Every method returns a sanitized :class:`BillingEndpointResult`
that captures the HTTP status, a coarse record count, and a
*summary* of the response — never the raw record bodies.  Raw
invoice lines can contain customer-sensitive PII / addresses; the
POC contract is "no raw billing records in logs / error messages".

The Billing API is not enabled on every Rackspace tenant.  When
the service is not present in the Identity catalog, the methods
return a result with ``status="NOT_AVAILABLE"`` instead of
attempting an unauthenticated request.  When the catalog has the
endpoint but the actual product call returns 404 (e.g. billing
feature disabled on the account), the result is
``status="NOT_FOUND"`` with an explanatory note.
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


#: Identifiers Rackspace ships in the Identity catalog for its
#: Billing API.  Phase 9A resolves the endpoint URL from the
#: catalog by matching either the ``type`` or ``name`` field of
#: each entry, so the caller does not need to know which layout
#: a particular tenant uses.
BILLING_SERVICE_NAMES: List[str] = [
    "cloudBilling",
    "rax:billing",
    "billing",
]

#: Backwards-compatible alias used by tests and external callers
#: that prefer the ``type`` field label.
BILLING_SERVICE_TYPES: List[str] = list(BILLING_SERVICE_NAMES)


@dataclass(frozen=True)
class BillingEndpointResult:
    """Sanitized outcome of one Billing API probe.

    Attributes
    ----------
    endpoint:
        Logical endpoint name (e.g. ``"billing_root"``,
        ``"billing_payments"``, ``"billing_invoices"``).
    http_status:
        HTTP status code returned by Rackspace, or ``None`` when
        no request was issued (e.g. service absent from catalog).
    status:
        Coarse outcome label — one of:

        * ``"OK"`` — HTTP 200 with a JSON body.
        * ``"AUTH_FAILED"`` — HTTP 401.
        * ``"PERMISSION_DENIED"`` — HTTP 403.
        * ``"NOT_FOUND"`` — HTTP 404 or service absent from the
          catalog.
        * ``"TRANSPORT_ERROR"`` — network / parse failure.
    summary:
        Sanitized summary fields (record counts, date ranges,
        currency codes).  Never contains raw customer billing
        record fields.
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
    """Read-only client for the Rackspace Billing API."""

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
        """Probe every known Billing endpoint.  Read-only.

        The method never raises; it returns a list of
        :class:`BillingEndpointResult` describing what was tried
        and what came back.  This is the entry point used by the
        Phase 9A validator script.
        """

        return [
            await self.get_account_metadata(),
            await self.get_payments(),
            await self.get_invoices(),
        ]

    async def get_account_metadata(self) -> BillingEndpointResult:
        """``GET /v1/billing`` (account metadata)."""

        return await self._safe_get(
            endpoint="billing_root",
            service_types=BILLING_SERVICE_TYPES,
            path="/v1/billing",
            summary_keys=("account_number", "billing_account_id", "currency", "contact_email", "name"),
            list_key=None,
        )

    async def get_payments(self) -> BillingEndpointResult:
        """``GET /v1/billing/payments``."""

        return await self._safe_get(
            endpoint="billing_payments",
            service_types=BILLING_SERVICE_TYPES,
            path="/v1/billing/payments",
            summary_keys=("amount", "currency", "date"),
            list_key="payments",
        )

    async def get_invoices(self) -> BillingEndpointResult:
        """``GET /v1/billing/invoices``."""

        return await self._safe_get(
            endpoint="billing_invoices",
            service_types=BILLING_SERVICE_TYPES,
            path="/v1/billing/invoices",
            summary_keys=("id", "total", "currency", "status", "period"),
            list_key="invoices",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _safe_get(
        self,
        *,
        endpoint: str,
        service_types: List[str],
        path: str,
        summary_keys: tuple,
        list_key: Optional[str],
    ) -> BillingEndpointResult:
        try:
            session = await self._identity.current_session()
        except (RackspaceConfigError, RackspaceAuthError) as exc:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="AUTH_FAILED",
                summary={},
                detail=str(exc.message),
            )

        # Resolve the endpoint URL from the Identity catalog.
        base_url: Optional[str] = None
        for service_type in service_types:
            url = session.catalog.public_url_for(service_type)
            if url:
                base_url = url
                break
        if not base_url:
            return BillingEndpointResult(
                endpoint=endpoint,
                http_status=None,
                status="NOT_FOUND",
                summary={"service_types_checked": service_types},
                detail="Billing service not present in Identity catalog",
            )

        token_provider = self._identity.current_token
        try:
            response = await self._http.get(
                base_url, path, token_provider=token_provider,
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
            response.body, summary_keys=summary_keys, list_key=list_key,
        )
        return BillingEndpointResult(
            endpoint=endpoint,
            http_status=response.status_code,
            status="OK",
            summary=summary,
            detail="",
        )


def _summarise_billing_response(
    body: Dict[str, Any],
    *,
    summary_keys: tuple,
    list_key: Optional[str],
) -> Dict[str, Any]:
    """Produce a sanitized summary of a Billing response.

    The Billing API returns either:

    * a flat object whose keys we project onto ``summary_keys``, or
    * an object containing a list under ``list_key`` whose elements
      we project onto ``summary_keys`` while recording the count,
      currency set, and date range (when available).

    Raw record fields are NEVER included — only counts, currency
    codes, and date ranges.
    """

    if list_key and isinstance(body, dict):
        records = body.get(list_key)
        if isinstance(records, list):
            return _summarise_records(records, summary_keys=summary_keys, list_key=list_key)
        if records is None:
            # Some legacy tenants return the list at the top level.
            if isinstance(body.get("data"), list):
                return _summarise_records(body["data"], summary_keys=summary_keys, list_key=list_key)
    return _summarise_object(body, summary_keys=summary_keys)


def _summarise_object(
    body: Dict[str, Any], *, summary_keys: tuple,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key in summary_keys:
        if key in body:
            summary[key] = body[key]
    # The Billing API returns ``account_number`` and
    # ``billing_account_id`` interchangeably; record whichever is
    # present, never both as raw strings.
    if "account_number" in summary or "billing_account_id" in summary:
        identifier = summary.get("account_number") or summary.get("billing_account_id")
        summary["billing_account_identifier_present"] = bool(identifier)
        summary.pop("account_number", None)
        summary.pop("billing_account_id", None)
    return summary


def _summarise_records(
    records: List[Dict[str, Any]],
    *,
    summary_keys: tuple,
    list_key: Optional[str],
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
        for key in summary_keys:
            value = record.get(key)
            if value is None:
                continue
            if key == "currency" and isinstance(value, str):
                currencies.add(value.upper())
            elif key in ("date", "period") and isinstance(value, str):
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


__all__ = [
    "BILLING_SERVICE_TYPES",
    "BillingEndpointResult",
    "RackspaceBillingClient",
]
