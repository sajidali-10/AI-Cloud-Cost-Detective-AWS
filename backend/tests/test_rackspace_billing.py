"""Unit tests for the Rackspace Billing client — Phase 9A POC."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from app.services.rackspace.billing import RackspaceBillingClient
from app.services.rackspace.client import RackspaceHttpClient, RackspaceResponse
from app.services.rackspace.config import RackspaceCredentials
from app.services.rackspace.identity import (
    IdentitySession,
    RackspaceIdentity,
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


def _identity_with_catalog(*, service_type: Optional[str]) -> RackspaceIdentity:
    """Build a fake identity whose catalog has (or lacks) the
    given service type.
    """

    endpoints = []
    if service_type:
        endpoints.append(
            ServiceEndpoint(
                region="",
                service_type=service_type,
                service_name=service_type,
                public_url="https://billing.example/v1",
            )
        )
    session = IdentitySession(
        token="t",
        expires_at=10**12,
        issued_at=10**12,
        tenant_id="1",
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

    return _StubIdentity(session)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_billing_root_returns_ok(monkeypatch):
    """A 200 response summarises the billing account metadata."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "account_number": "ACC-REDACTED",
            "currency": "USD",
            "name": "Test Account",
            "contact_email": "ops@example.com",
            "raw_field_we_should_not_emit": "secret-stuff",
        },
        headers={},
    ))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account_metadata()
    assert result.ok
    assert result.http_status == 200
    assert result.summary.get("currency") == "USD"
    assert result.summary.get("billing_account_identifier_present") is True
    # Raw field must not leak through the summary.
    assert "raw_field_we_should_not_emit" not in result.summary
    assert "account_number" not in result.summary
    # Verb recorded as GET on the expected path.
    assert http.get_calls[0]["path"] == "/v1/billing"


@pytest.mark.asyncio
async def test_billing_payments_summary_counts_only(monkeypatch):
    """Payment summary reports record count and currency set, not raw rows."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "payments": [
                {"amount": "12.34", "currency": "USD", "date": "2024-09-01"},
                {"amount": "12.34", "currency": "USD", "date": "2024-10-01"},
                {"amount": "9.99", "currency": "EUR", "date": "2024-08-01"},
            ]
        },
        headers={},
    ))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_payments()
    assert result.ok
    assert result.summary["record_count"] == 3
    assert result.summary["currencies"] == ["EUR", "USD"]
    assert result.summary["date_range"]["earliest"] == "2024-08-01"
    assert result.summary["date_range"]["latest"] == "2024-10-01"


@pytest.mark.asyncio
async def test_billing_401_marks_auth_failed(monkeypatch):
    from app.services.rackspace.exceptions import RackspaceAuthError
    http = _StubHttp(get_error=RackspaceAuthError("HTTP 401"))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account_metadata()
    assert result.status == "AUTH_FAILED"
    assert result.http_status == 401


@pytest.mark.asyncio
async def test_billing_403_marks_permission_denied(monkeypatch):
    from app.services.rackspace.exceptions import RackspacePermissionError
    http = _StubHttp(get_error=RackspacePermissionError("HTTP 403"))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_invoices()
    assert result.status == "PERMISSION_DENIED"
    assert result.http_status == 403


@pytest.mark.asyncio
async def test_billing_404_marks_not_found(monkeypatch):
    from app.services.rackspace.exceptions import RackspaceNotFoundError
    http = _StubHttp(get_error=RackspaceNotFoundError("HTTP 404"))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_invoices()
    assert result.status == "NOT_FOUND"
    assert result.http_status == 404


@pytest.mark.asyncio
async def test_billing_missing_catalog_marks_not_found(monkeypatch):
    """A tenant without ``cloudBilling`` reports NOT_FOUND without issuing a request."""

    http = _StubHttp()
    identity = _identity_with_catalog(service_type=None)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account_metadata()
    assert result.status == "NOT_FOUND"
    assert result.http_status is None
    # No GET was issued when the service is absent from the catalog.
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_billing_transport_error(monkeypatch):
    from app.services.rackspace.exceptions import RackspaceTransportError
    http = _StubHttp(get_error=RackspaceTransportError("boom"))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    result = await client.get_account_metadata()
    assert result.status == "TRANSPORT_ERROR"


@pytest.mark.asyncio
async def test_billing_probe_all_returns_three_results(monkeypatch):
    """``probe_all`` returns one entry per probed endpoint."""

    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200, body={"hello": "world"}, headers={},
    ))
    identity = _identity_with_catalog(service_type="cloudBilling")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    client = RackspaceBillingClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    results = await client.probe_all()
    assert [r.endpoint for r in results] == [
        "billing_root", "billing_payments", "billing_invoices",
    ]
    assert all(r.ok for r in results)
