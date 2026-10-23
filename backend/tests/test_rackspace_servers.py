"""Unit tests for the Rackspace Cloud Servers client — Phase 9A POC."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from app.services.rackspace.client import RackspaceHttpClient, RackspaceResponse
from app.services.rackspace.config import RackspaceCredentials
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import (
    IdentitySession,
    ServiceCatalog,
    ServiceEndpoint,
)
from app.services.rackspace.servers import RackspaceServersClient


class _StubHttp:
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
        self.get_calls.append({"base_url": base_url, "path": path})
        if self.get_error is not None:
            raise self.get_error
        assert self.get_response is not None, "test must set get_response"
        return self.get_response

    async def post_json(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("servers tests must not POST")

    async def aclose(self) -> None:  # pragma: no cover
        return None


def _identity_with_catalog(*, service_type: Optional[str]) -> Any:
    endpoints = []
    if service_type:
        endpoints.append(
            ServiceEndpoint(
                region="IAD",
                service_type=service_type,
                service_name=service_type,
                public_url="https://iad.servers.example/v2/1",
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
            self.invalidated = False

        async def current_session(self) -> IdentitySession:
            return self._session

        async def current_token(self) -> str:
            return self._session.token

        async def invalidate(self):
            self.invalidated = True

    return _StubIdentity(session)


@pytest.mark.asyncio
async def test_servers_list_returns_envelope(monkeypatch):
    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={"servers": [{"id": "a", "name": "alpha", "links": []}]},
        headers={},
    ))
    identity = _identity_with_catalog(service_type="cloudServersOpenStack")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    payload = await client.list_servers()
    assert payload == {"servers": [{"id": "a", "name": "alpha", "links": []}]}
    assert http.get_calls[0]["path"] == "/servers"


@pytest.mark.asyncio
async def test_servers_detail_returns_full_payload(monkeypatch):
    http = _StubHttp(get_response=RackspaceResponse(
        status_code=200,
        body={
            "servers": [
                {
                    "id": "a",
                    "name": "alpha",
                    "status": "ACTIVE",
                    "flavor": {"id": "performance1-1"},
                    "region": "IAD",
                    "addresses": {"public": ["203.0.113.5"]},
                    "user-data": "should-not-leak",
                },
                {
                    "id": "b",
                    "status": "BUILD",
                    "flavor": {"id": "performance1-2"},
                    "OS-EXT-AZ:availability_zone": "DFW",
                },
                {
                    "id": "c",
                    "status": "ERROR",
                    "flavor": {"id": "performance1-1"},
                    "region": "ORD",
                },
            ]
        },
        headers={},
    ))
    identity = _identity_with_catalog(service_type="cloudServersOpenStack")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    summary = await client.get_aggregate_summary()
    assert summary.total_count == 3
    assert summary.status_counts == {"ACTIVE": 1, "BUILD": 1, "ERROR": 1}
    assert summary.regions == ["DFW", "IAD", "ORD"]
    assert sorted(set(summary.flavor_ids)) == ["performance1-1", "performance1-2"]


@pytest.mark.asyncio
async def test_servers_404_when_service_absent(monkeypatch):
    http = _StubHttp()
    identity = _identity_with_catalog(service_type=None)
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    with pytest.raises(RackspaceNotFoundError):
        await client.list_servers()


@pytest.mark.asyncio
async def test_servers_401_invalidates_identity(monkeypatch):
    http = _StubHttp(get_error=RackspaceAuthError("HTTP 401"))
    identity = _identity_with_catalog(service_type="cloudServersOpenStack")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    with pytest.raises(RackspaceAuthError):
        await client.list_servers()
    assert identity.invalidated is True


@pytest.mark.asyncio
async def test_servers_403_summary_reports_permission(monkeypatch):
    http = _StubHttp(get_error=RackspacePermissionError("HTTP 403"))
    identity = _identity_with_catalog(service_type="cloudServersOpenStack")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    summary = await client.get_aggregate_summary()
    assert summary.total_count == 0
    assert "PERMISSION_DENIED" in summary.detail


@pytest.mark.asyncio
async def test_servers_transport_error(monkeypatch):
    http = _StubHttp(get_error=RackspaceTransportError("boom"))
    identity = _identity_with_catalog(service_type="cloudServersOpenStack")
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: RackspaceCredentials("u", "k", "1"),
    )
    client = RackspaceServersClient(http_client=http, identity=identity)  # type: ignore[arg-type]
    summary = await client.get_aggregate_summary()
    assert summary.total_count == 0
    assert "TRANSPORT_ERROR" in summary.detail


def test_servers_client_exposes_only_read_methods():
    """The Cloud Servers client surface has no write methods.

    This is a structural check — the POC contract is "no POST /
    PUT / PATCH / DELETE on Rackspace product APIs".  The
    billing / identity clients have the same property by design.
    """

    forbidden = {"post", "put", "patch", "delete", "create", "update", "destroy"}
    for name in dir(RackspaceServersClient):
        if name.startswith("_"):
            continue
        if name in forbidden:
            pytest.fail(
                f"RackspaceServersClient must not expose write method {name!r}"
            )
