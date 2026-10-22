"""Unit tests for the Rackspace Billing client — Phase 9A POC.

These tests mock every HTTP call.  No real Rackspace credentials
are required.  The HTTP layer is injected directly so the tests
exercise the Billing client without touching ``httpx``.

The Billing API is, per Rackspace documentation, intentionally
NOT exposed through the Identity service catalog.  The tests
therefore assert that:

* the client always calls ``https://billing.api.rackspacecloud.com``,
* the path uses the documented ``/v2/accounts/{ran}`` shape,
* the RAN is the ``RACKSPACE_BILLING_ACCOUNT`` env var (NOT
  ``RACKSPACE_ACCOUNT_ID``),
* a missing RAN produces ``NOT_CONFIGURED`` without any HTTP
  call,
* the catalog is never consulted for Billing endpoints.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from app.services.rackspace.billing import (
    BILLING_API_VERSION,
    BILLING_BASE_URL,
    BillingEndpointResult,
    RackspaceBillingClient,
)
from app.services.rackspace.client import RackspaceHttpClient, RackspaceResponse
from app.services.rackspace.config import RackspaceCredentials
from app.services.rackspace.identity import (
    IdentitySession,
    ServiceCatalog,
    ServiceEndpoint,
)


class _StubHttp:
    """Stubbed HTTP client that records every call and returns a
    pre-programmed response or raises a pre-programmed exception.
    """

    def __init__(
        self,
        *,
        get_response: Optional[RackspaceResponse] = None,
        get_error: Optional[Exception] = None,
    ) -> None:
        self.get_response = get_response
        self.get_error = get_error
        self.get_calls: list = []

    async def get(
        self, base_url, path, *, token_provider, params=None, headers=None,
    ) -> RackspaceResponse:
        self.get_calls.append(
            {
                "base_url": base_url,
                "path": path,
                "params": params,
                "headers": headers,
            }
        )
        if self.get_error is not None:
            raise self.get_error
        assert self.get_response is not None, "test must set get_response"
        return self.get_response

    async def post_json(self, *args, **kwargs):  # pragma: no cover - unused
        raise AssertionError("billing tests must not POST")

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _credentials() -> RackspaceCredentials:
    return RackspaceCredentials(
        username="rackspace-user",
        api_key="test-api-key",
        account_id="rackspace-account",
    )


def _set_ran(monkeypatch, ran: str = "020-123456") -> None:
    monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", ran)


def _identity_without_billing_catalog() -> Any:
    """Return a stub Identity whose catalog does NOT contain any
    billing entries — proving the Billing client does not look
    the catalog up for the billing URL.
    """

    endpoints = [
        ServiceEndpoint(
            region="IAD",
            service_type="cloudServersOpenStack",
            service_name="cloudServersOpenStack",
            public_url="https://iad.servers.example/v2/123456",
        ),
    ]
    session = IdentitySession(
        token="t",
        expires_at=10**12,
        issued_at=10**12,
        tenant_id="123456",
        tenant_name="t",
        username="u",
        roles=[],
        catalog=ServiceCatalog(endpoints=endpoints),
    )

    class _StubIdentity:
        def __init__(self, session):
            self._session = session
            self.token_calls = 0

        async def current_session(self) -> IdentitySession:
            return self._session

        async def current_token(self) -> str:
            self.token_calls += 1
            return self._session.token

        async def invalidate(self):
            pass

    return _StubIdentity(session)


# ---------------------------------------------------------------------------
# Billing base endpoint is external to the service catalog
# ---------------------------------------------------------------------------


def test_billing_base_url_is_documented_rackspace_endpoint():
    """The Billing base URL is the documented external endpoint,
    not something resolved from the Identity catalog.
    """

    assert BILLING_BASE_URL == "https://billing.api.rackspacecloud.com"


def test_billing_api_version_is_v2():
    """The Billing API version is V2 per Rackspace's documented API."""

    assert BILLING_API_VERSION == "v2"


@pytest.mark.asyncio
async def test_billing_calls_external_base_not_catalog(monkeypatch):
    """The GET request is made to the documented Billing base URL.

    This test deliberately uses an Identity catalog that has
    NO ``cloudBilling`` entry — proving the Billing client does
    not depend on the service catalog to find the endpoint.
    """

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200, body={"accountNumber": "020-123456"}, headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account()
    assert result.ok
    assert http.get_calls[0]["base_url"] == BILLING_BASE_URL
    # Path must contain the RAN, not the Cloud-API tenant ID.
    assert http.get_calls[0]["path"] == f"/{BILLING_API_VERSION}/accounts/020-123456"


@pytest.mark.asyncio
async def test_billing_uses_ran_not_tenant_id(monkeypatch):
    """The path uses ``RACKSPACE_BILLING_ACCOUNT`` (RAN) and NOT
    ``RACKSPACE_ACCOUNT_ID`` (Cloud tenant ID).
    """

    monkeypatch.setenv("RACKSPACE_ACCOUNT_ID", "999999")
    _set_ran(monkeypatch, ran="020-000001")
    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200, body={"ok": True}, headers={},
    ))
    identity = _identity_without_billing_catalog()
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_currency()
    assert result.ok
    # The RAN must appear in the path; the Cloud-API account ID
    # must NOT appear.
    assert "020-000001" in http.get_calls[0]["path"]
    assert "999999" not in http.get_calls[0]["path"]


# ---------------------------------------------------------------------------
# Account lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_lookup_success(monkeypatch):
    """``GET /v2/accounts/{ran}`` returns sanitized summary."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "accountNumber": "020-000001",
            "currency": "USD",
            "status": "OPEN",
            "currentPeriod": {"start": "2024-09-01", "end": "2024-09-30"},
            "contactEmail": "ops@example.com",
            "address": "123 Sensitive Street",
        },
        headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account()
    assert result.ok
    assert result.http_status == 200
    assert result.summary.get("currency") == "USD"
    assert result.summary.get("status") == "OPEN"
    assert result.summary.get("billing_account_identifier_present") is True
    assert result.summary.get("current_period_available") is True
    assert result.summary["current_period"]["start"] == "2024-09-01"
    # Raw PII fields must NOT leak through the summary.
    assert "contactEmail" not in result.summary
    assert "address" not in result.summary
    assert "020-000001" not in str(result.summary)


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_currency_success(monkeypatch):
    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={"currency": "usd"},
        headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_currency()
    assert result.ok
    assert result.summary["currency"] == "USD"
    assert http.get_calls[0]["path"] == f"/{BILLING_API_VERSION}/accounts/020-123456/currency"


# ---------------------------------------------------------------------------
# Estimated charges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimated_charges_success(monkeypatch):
    """Estimated charges summary contains sanitized total + period."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "currency": "USD",
            "estimatedTotal": 1234.567,
            "currentPeriod": {"start": "2024-09-01", "end": "2024-09-30"},
            "estimatedCharges": [
                {"service": "Cloud Servers", "amount": 100.0, "currency": "USD"},
                {"service": "Cloud Files", "amount": 50.0, "currency": "USD"},
            ],
        },
        headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_estimated_charges()
    assert result.ok
    assert result.summary["currency"] == "USD"
    # Sanitized total is rounded to 2 decimals — no per-resource
    # breakdown leaked.
    assert result.summary["total"] == 1234.57
    assert result.summary["sanitized_current_cost_available"] is True
    assert result.summary["record_count"] == 2


@pytest.mark.asyncio
async def test_estimated_charges_handles_list_envelope(monkeypatch):
    """A list-shaped ``estimatedCharges`` envelope is summarised too."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "currency": "EUR",
            "estimatedCharges": [
                {"amount": "12.34", "currency": "EUR", "date": "2024-09-01"},
                {"amount": "9.99", "currency": "USD", "date": "2024-08-01"},
            ],
        },
        headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_estimated_charges()
    assert result.ok
    assert result.summary["record_count"] == 2
    assert result.summary["currencies"] == ["EUR", "USD"]
    assert result.summary["date_range"]["earliest"] == "2024-08-01"
    assert result.summary["date_range"]["latest"] == "2024-09-01"


# ---------------------------------------------------------------------------
# Missing RAN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_ran_marks_not_configured(monkeypatch):
    """Without ``RACKSPACE_BILLING_ACCOUNT`` the client returns
    ``NOT_CONFIGURED`` without making any HTTP call.
    """

    # We SET to empty string rather than ``delenv`` so
    # pydantic-settings does not fall back to the host ``.env``
    # value.  An empty value is treated as "not configured".
    monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", "")
    http = _StubHttp()
    identity = _identity_without_billing_catalog()
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account()
    assert result.status == "NOT_CONFIGURED"
    assert result.http_status is None
    # No GET was issued when the RAN is missing.
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_empty_ran_marks_not_configured(monkeypatch):
    """An explicitly-empty RAN is treated as missing."""

    monkeypatch.setenv("RACKSPACE_BILLING_ACCOUNT", "   ")
    http = _StubHttp()
    identity = _identity_without_billing_catalog()
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_currency()
    assert result.status == "NOT_CONFIGURED"
    assert http.get_calls == []


# ---------------------------------------------------------------------------
# Status codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_billing_401_marks_auth_failed(monkeypatch):
    from app.services.rackspace.exceptions import RackspaceAuthError
    http = _StubHttp(get_error=RackspaceAuthError("HTTP 401"))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account()
    assert result.status == "AUTH_FAILED"
    assert result.http_status == 401


@pytest.mark.asyncio
async def test_billing_403_marks_permission_denied(monkeypatch):
    from app.services.rackspace.exceptions import RackspacePermissionError
    http = _StubHttp(get_error=RackspacePermissionError("HTTP 403"))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_currency()
    assert result.status == "PERMISSION_DENIED"
    assert result.http_status == 403


@pytest.mark.asyncio
async def test_billing_404_marks_not_found(monkeypatch):
    """A 404 means the RAN is invalid or Billing is not on this account."""

    from app.services.rackspace.exceptions import RackspaceNotFoundError
    http = _StubHttp(get_error=RackspaceNotFoundError("HTTP 404"))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_estimated_charges()
    assert result.status == "NOT_FOUND"
    assert result.http_status == 404


@pytest.mark.asyncio
async def test_billing_401_invalidates_identity(monkeypatch):
    """A 401 from Billing invalidates the cached Identity session."""

    from app.services.rackspace.exceptions import RackspaceAuthError

    invalidated = {"called": False}

    class _InvalidatingIdentity(_identity_without_billing_catalog().__class__):
        async def invalidate(self):
            invalidated["called"] = True

    http = _StubHttp(get_error=RackspaceAuthError("HTTP 401"))
    identity = _InvalidatingIdentity(
        _identity_without_billing_catalog()._session,
    )
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account()
    assert result.status == "AUTH_FAILED"
    assert invalidated["called"] is True


# ---------------------------------------------------------------------------
# probe_all ordering and structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_all_returns_five_results(monkeypatch):
    """``probe_all`` returns one entry per documented endpoint."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200, body={"hello": "world"}, headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    results = await client.probe_all()
    assert [r.endpoint for r in results] == [
        "billing_account",
        "billing_currency",
        "billing_estimated_charges",
        "billing_billing_summary",
        "billing_invoice_latest",
    ]
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_probe_all_safe_order(monkeypatch):
    """The probe order is account → currency → estimated_charges
    → billing_summary → latest_invoice.

    The validator script depends on this order so a "successful
    account lookup but failed invoice metadata" outcome is
    visible as a per-endpoint status without fabricating data.
    """

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200, body={}, headers={},
    ))
    identity = _identity_without_billing_catalog()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    await client.probe_all()
    paths = [call["path"] for call in http.get_calls]
    assert paths == [
        f"/{BILLING_API_VERSION}/accounts/020-123456",
        f"/{BILLING_API_VERSION}/accounts/020-123456/currency",
        f"/{BILLING_API_VERSION}/accounts/020-123456/estimated_charges",
        f"/{BILLING_API_VERSION}/accounts/020-123456/billing-summary",
        f"/{BILLING_API_VERSION}/accounts/020-123456/invoices/latest",
    ]
    # All GETs target the documented Billing base URL — never
    # anything resolved from the service catalog.
    for call in http.get_calls:
        assert call["base_url"] == BILLING_BASE_URL


# ---------------------------------------------------------------------------
# Token redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_redaction_in_billing_results(monkeypatch):
    """The Billing client never echoes the X-Auth-Token in summary/detail."""

    sentinel_token = "SECRET-X-AUTH-TOKEN-VALUE-1234567890"

    class _TokenIdentity:
        def __init__(self):
            self._session = IdentitySession(
                token=sentinel_token,
                expires_at=10**12,
                issued_at=10**12,
                tenant_id="t",
                tenant_name="t",
                username="u",
                roles=[],
                catalog=ServiceCatalog(),
            )

        async def current_session(self) -> IdentitySession:
            return self._session

        async def current_token(self) -> str:
            return sentinel_token

        async def invalidate(self):
            pass

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={"currency": "USD", "internalToken": sentinel_token},
        headers={},
    ))
    identity = _TokenIdentity()
    _set_ran(monkeypatch)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_currency()
    rendered = str(result.summary) + " " + result.detail
    assert sentinel_token not in rendered


# ---------------------------------------------------------------------------
# No product write methods
# ---------------------------------------------------------------------------


def test_billing_client_exposes_only_read_methods():
    """The Billing client has no write verbs on its public surface."""

    forbidden = {"post", "put", "patch", "delete", "create", "update", "destroy"}
    for name in dir(RackspaceBillingClient):
        if name.startswith("_"):
            continue
        if name in forbidden:
            pytest.fail(
                f"RackspaceBillingClient must not expose write method {name!r}"
            )


def test_billing_uses_get_verb_only(monkeypatch):
    """The HTTP client itself only exposes ``get`` and ``post_json``."""

    forbidden = {"put", "patch", "delete"}
    for name in forbidden:
        assert not hasattr(RackspaceHttpClient, name), (
            f"RackspaceHttpClient must not expose {name!r}"
        )


# ---------------------------------------------------------------------------
# Billing result dataclass immutability
# ---------------------------------------------------------------------------


def test_billing_endpoint_result_is_frozen():
    """``BillingEndpointResult`` is a frozen dataclass — the status
    and http_status fields cannot be mutated after construction.
    """

    result = BillingEndpointResult(
        endpoint="billing_account",
        http_status=200,
        status="OK",
        summary={"currency": "USD"},
        detail="",
    )
    assert result.ok is True
    with pytest.raises(Exception):
        # Frozen dataclass — this MUST raise.
        result.status = "FORGED"  # type: ignore[misc]
