"""Unit tests for the Rackspace Identity client — Phase 9A POC.

These tests mock every HTTP call.  No real Rackspace credentials
are required.  The HTTP layer is injected directly so the tests
exercise the Identity client without touching ``httpx``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.services.rackspace.client import RackspaceHttpClient, RackspaceResponse
from app.services.rackspace.config import RackspaceCredentials
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceElevatedRoleError,
    RackspaceTransportError,
)
from app.services.rackspace.identity import (
    ELEVATED_ROLES,
    IdentitySession,
    RackspaceIdentity,
    SAFE_ROLES,
)


class _StubHttp:
    """A drop-in replacement for :class:`RackspaceHttpClient`.

    The Identity client only ever calls ``post_json`` (for
    authentication) and ``get`` (for product APIs).  We expose
    just those two methods and let each test pre-program the
    responses / errors.
    """

    def __init__(
        self,
        *,
        post_response: Optional[RackspaceResponse] = None,
        post_error: Optional[Exception] = None,
    ) -> None:
        self.post_response = post_response
        self.post_error = post_error
        self.post_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []

    async def post_json(
        self, base_url: str, path: str, body: Dict[str, Any], *,
        headers: Optional[Dict[str, str]] = None,
    ) -> RackspaceResponse:
        self.post_calls.append(
            {"base_url": base_url, "path": path, "body": body, "headers": headers}
        )
        if self.post_error is not None:
            raise self.post_error
        assert self.post_response is not None, "test must set post_response"
        return self.post_response

    async def get(self, *args, **kwargs):
        self.get_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("identity tests must not issue GET")

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None


def _ok_token_payload(
    *,
    expires: str = "2030-01-01T00:00:00Z",
    roles: Optional[List[Dict[str, str]]] = None,
    catalog: Optional[List[Dict[str, Any]]] = None,
    tenant_id: str = "123456",
    tenant_name: str = "rackspace-test-tenant",
    username: str = "rackspace-test-user",
    token_id: str = "secret-token-value-do-not-leak",
) -> Dict[str, Any]:
    """Build a valid Rackspace Identity ``access.token`` payload."""

    return {
        "access": {
            "token": {
                "id": token_id,
                "expires": expires,
                "tenant": {"id": tenant_id, "name": tenant_name},
                "user": {
                    "id": "user-id",
                    "name": username,
                    "username": username,
                    "roles": roles if roles is not None else [
                        {"name": "observer", "description": "Observer role"},
                    ],
                },
                "serviceCatalog": {
                    "services": catalog if catalog is not None else [
                        {
                            "name": "cloudServersOpenStack",
                            "type": "cloudServersOpenStack",
                            "endpoints": [
                                {
                                    "region": "IAD",
                                    "publicURL": "https://iad.servers.api.rackspacecloud.com/v2/123456",
                                }
                            ],
                        }
                    ],
                },
            },
            "serviceCatalog": {"services": []},
            "user": {"name": username, "id": "user-id", "roles": []},
        }
    }


def _credentials() -> RackspaceCredentials:
    return RackspaceCredentials(
        username="rackspace-user",
        api_key="test-api-key",
        account_id="rackspace-account",
    )


@pytest.mark.asyncio
async def test_identity_success_parses_token(monkeypatch):
    """Successful authentication parses the token without leaking it."""

    http = _StubHttp(post_response=RackspaceResponse(
        status_code=200,
        body=_ok_token_payload(),
        headers={},
    ))
    identity = RackspaceIdentity(http_client=http)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    session = await identity.authenticate()
    assert session.token == "secret-token-value-do-not-leak"
    assert session.username == "rackspace-test-user"
    assert session.tenant_id == "123456"
    assert session.expires_at > 0
    # repr must not contain the token value.
    assert "secret-token-value-do-not-leak" not in repr(session)


@pytest.mark.asyncio
async def test_identity_authentication_failure(monkeypatch):
    """Identity 401 raises :class:`RackspaceAuthError`."""

    http = _StubHttp(post_error=RackspaceAuthError("HTTP 401"))
    identity = RackspaceIdentity(http_client=http)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    with pytest.raises(RackspaceAuthError):
        await identity.authenticate()


@pytest.mark.asyncio
async def test_identity_invalid_credential_payload(monkeypatch):
    """Identity 200 with missing ``access.token.id`` is treated as auth failure."""

    http = _StubHttp(post_response=RackspaceResponse(
        status_code=200, body={"access": {"token": {}}}, headers={},
    ))
    identity = RackspaceIdentity(http_client=http)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    with pytest.raises(RackspaceAuthError):
        await identity.authenticate()


@pytest.mark.asyncio
async def test_identity_missing_credentials_raise_config_error(monkeypatch):
    """Missing credentials surface as :class:`RackspaceConfigError`."""

    http = _StubHttp()
    identity = RackspaceIdentity(http_client=http)  # type: ignore[arg-type]
    from app.services.rackspace import config as rs_config
    monkeypatch.setattr(
        rs_config,
        "get_credentials",
        lambda: (_ for _ in ()).throw(RackspaceConfigError("missing")),
    )
    # Patch the symbol bound on the identity module directly so
    # the call inside ``current_session`` resolves to our stub.
    import app.services.rackspace.identity as rs_identity_module
    monkeypatch.setattr(
        rs_identity_module, "get_credentials",
        lambda: (_ for _ in ()).throw(RackspaceConfigError("missing")),
    )
    with pytest.raises(RackspaceConfigError):
        await identity.current_session()


def test_role_parsing_and_elevated_detection():
    """Roles are parsed; elevated roles are flagged."""

    roles = [
        {"name": "observer", "description": "Observer"},
        {"name": "admin", "description": "Tenant admin"},
        {"name": "billing:observer", "description": "Billing read"},
    ]
    parsed = []
    for r in roles:
        from app.services.rackspace.identity import RoleAssignment
        parsed.append(RoleAssignment.from_payload(r))
    assert parsed[0].name == "observer"
    assert parsed[1].name == "admin"
    # Elevated detection includes the admin role only.
    names = {p.name for p in parsed}
    assert names & ELEVATED_ROLES == {"admin"}


def test_role_safe_predicate():
    assert RackspaceIdentity.is_role_safe("observer") is True
    assert RackspaceIdentity.is_role_safe("billing:observer") is True
    assert RackspaceIdentity.is_role_safe("admin") is False
    assert RackspaceIdentity.is_role_safe("random") is False


def test_elevated_role_predicate():
    assert RackspaceIdentity.is_elevated("admin") is True
    assert RackspaceIdentity.is_elevated("billing:admin") is True
    assert RackspaceIdentity.is_elevated("identity:user-admin") is True
    assert RackspaceIdentity.is_elevated("identity:admin") is True
    assert RackspaceIdentity.is_elevated("creator") is True
    assert RackspaceIdentity.is_elevated("observer") is False
    assert RackspaceIdentity.is_elevated("billing:observer") is False


def test_elevated_role_in_session_detected():
    """``IdentitySession.elevated_roles_present`` reports admin roles."""

    from app.services.rackspace.identity import (
        RoleAssignment, ServiceCatalog,
    )
    session = IdentitySession(
        token="t",
        expires_at=10**12,
        issued_at=10**12,
        tenant_id="1",
        tenant_name="t",
        username="u",
        roles=[RoleAssignment(name="admin"), RoleAssignment(name="observer")],
        catalog=ServiceCatalog(),
    )
    assert session.elevated_roles_present == ["admin"]
    assert sorted(session.role_names) == ["admin", "observer"]


@pytest.mark.asyncio
async def test_token_expiry_triggers_reauthentication(monkeypatch):
    """An expired cached session triggers a fresh authentication."""

    # First response returns an already-expired token; we set the
    # clock to a moment well past the expiry so the cached session
    # is immediately considered stale.
    http = _StubHttp(post_response=RackspaceResponse(
        status_code=200,
        body=_ok_token_payload(expires="2024-01-01T00:00:00Z"),
        headers={},
    ))

    # Inject a deterministic clock that returns a far-future
    # ``now`` so the cached session is expired.
    future = 10**12

    identity = RackspaceIdentity(http_client=http, clock=lambda: future)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    # First call performs the authentication and caches the
    # (already-expired) session.
    first = await identity.current_session()
    assert first.expires_at < future
    # Second call observes the expiry and re-authenticates.
    await identity.current_session()
    assert len(http.post_calls) == 2


@pytest.mark.asyncio
async def test_token_caching_within_safety_margin(monkeypatch):
    """Within the safety margin the cached token is reused."""

    http = _StubHttp(post_response=RackspaceResponse(
        status_code=200,
        body=_ok_token_payload(expires="2030-01-01T00:00:00Z"),
        headers={},
    ))
    identity = RackspaceIdentity(http_client=http)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "app.services.rackspace.identity.get_credentials",
        lambda: _credentials(),
    )
    first = await identity.current_session()
    second = await identity.current_session()
    assert first is second
    assert len(http.post_calls) == 1


def test_service_catalog_parsing():
    """The service catalog is flattened into a :class:`ServiceCatalog`."""

    payload = {
        "access": {
            "token": {
                "id": "tok",
                "expires": "2030-01-01T00:00:00Z",
                "user": {"roles": [{"name": "observer"}]},
                "serviceCatalog": {
                    "services": [
                        {
                            "name": "cloudServersOpenStack",
                            "type": "cloudServersOpenStack",
                            "endpoints": [
                                {
                                    "region": "IAD",
                                    "publicURL": "https://iad.servers.example/v2/1",
                                },
                                {
                                    "region": "DFW",
                                    "publicURL": "https://dfw.servers.example/v2/1",
                                },
                            ],
                        },
                        {
                            "name": "cloudBilling",
                            "type": "cloudBilling",
                            "endpoints": [
                                {
                                    "region": "",
                                    "publicURL": "https://billing.example/v1",
                                }
                            ],
                        },
                    ]
                },
            }
        }
    }

    identity = RackspaceIdentity(http_client=_StubHttp())  # type: ignore[arg-type]
    session = RackspaceIdentity.parse_token_payload(payload["access"]["token"])
    catalog = session.catalog
    types = catalog.discovered_service_types()
    assert "cloudServersOpenStack" in types
    assert "cloudBilling" in types
    # Region preference resolves the IAD endpoint when requested.
    assert catalog.public_url_for(
        "cloudServersOpenStack", region="IAD",
    ) == "https://iad.servers.example/v2/1"
    # Default returns the first match (IAD in catalog order).
    assert catalog.public_url_for(
        "cloudServersOpenStack",
    ) == "https://iad.servers.example/v2/1"
    # Missing service type returns None.
    assert catalog.public_url_for("nonexistent") is None


def test_token_redaction_in_identity_session():
    """``repr`` of an :class:`IdentitySession` never contains the token."""

    session = IdentitySession(
        token="abcdef1234567890SECRET",
        expires_at=10**12,
        issued_at=10**12,
        tenant_id="1",
        tenant_name="t",
        username="u",
        roles=[],
        catalog=None,  # type: ignore[arg-type]
    )
    text = repr(session)
    assert "SECRET" not in text
    assert "abcdef" not in text
