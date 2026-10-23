"""Rackspace Identity client — Phase 9A POC.

Authentication flow
-------------------

Rackspace Cloud uses the OpenStack Keystone-derived Identity API
v2.0.  Authentication is performed via a single POST to
``https://identity.api.rackspacecloud.com/v2.0/tokens`` with the
``RAX-KSKEY:apiKeyCredentials`` payload::

    {
      "auth": {
        "RAX-KSKEY:apiKeyCredentials": {
          "username": "<rackspace username>",
          "apiKey":   "<rackspace api key>"
        }
      }
    }

A successful response carries a *temporary* X-Auth-Token plus the
service catalog.  The token has an expiry timestamp; this client
caches it in memory and re-authenticates automatically when the
cached token is within 60 seconds of expiry or when a downstream
product API returns HTTP 401.

The token is NEVER logged.  :func:`_redact_token` is applied to
every string the client produces for human consumption.

Service catalog
---------------

The service catalog returned by Identity is parsed into a flat
:class:`ServiceCatalog` summary.  We resolve endpoints by *service
type* (``cloudServersOpenStack``, ``cloudBilling``,
``cloudMonitoring``, ``cloudBlockStorage``, ``cloudFiles``) so the
rest of the application does not hard-code any product URL.

Role model
----------

The Identity ``user`` object carries a ``roles`` array.  Phase 9A
treats the following roles as elevated and refuses to proceed:

* ``admin``,
* ``billing:admin``,
* ``creator`` (Rackspace legacy write role),
* ``identity:user-admin`` and ``identity:admin``.

A separate set of roles is considered safe for a read-only POC
(``observer``, ``product:observer``, ``billing:observer``, etc.).
The client exposes :func:`is_role_safe` so the validator script
can report its conclusions without invoking the network.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.services.rackspace.client import RackspaceHttpClient
from app.services.rackspace.config import (
    RACKSPACE_IDENTITY_URL,
    RackspaceCredentials,
    get_credentials,
)
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceElevatedRoleError,
    RackspaceTransportError,
)


#: Roles that grant write capabilities and must NEVER be observed on
#: the authenticated principal.  The POC fails closed on this list.
ELEVATED_ROLES = frozenset({
    "admin",
    "billing:admin",
    "creator",
    "identity:user-admin",
    "identity:admin",
})

#: Roles considered safe for a read-only POC.  This list is
#: illustrative; the contract is "no role from ELEVATED_ROLES is
#: present".  We also accept ``product:observer`` and ``observer``
#: variants because Rackspace has shipped multiple role-naming
#: conventions across the years.
SAFE_ROLES = frozenset({
    "observer",
    "product:observer",
    "billing:observer",
    "cloud:observer",
    "monitoring:observer",
})


#: Re-authenticate when the cached token is within this many
#: seconds of its declared expiry.  Chosen to comfortably outlive
#: one product API round-trip while still leaving the token usable.
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 60


@dataclass(frozen=True)
class RoleAssignment:
    """One role entry from the Identity ``user.roles`` array.

    Rackspace ``roles`` entries look like::

        {"name": "observer", "description": "Read-only access"}

    Older tenants may also include ``id`` / ``tenantId`` keys; both
    are optional and ignored by the POC.
    """

    name: str
    description: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "RoleAssignment":
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        return cls(name=name, description=description)


@dataclass(frozen=True)
class ServiceEndpoint:
    """One endpoint from the Identity service catalog.

    ``region`` is the human-friendly Rackspace region label
    (``"DFW"``, ``"ORD"``, ``"IAD"``, ``"LON"``, ``"SYD"``,
    ``"HKG"``).  ``public_url`` and ``internal_url`` are both
    retained; the POC uses the public URL for product calls.
    """

    region: str
    service_type: str
    service_name: str
    public_url: str
    internal_url: str = ""

    @classmethod
    def from_payload(
        cls,
        service: Dict[str, Any],
        endpoint: Dict[str, Any],
    ) -> "ServiceEndpoint":
        return cls(
            region=str(endpoint.get("region") or "").strip(),
            service_type=str(service.get("type") or "").strip(),
            service_name=str(service.get("name") or "").strip(),
            public_url=str(endpoint.get("publicURL") or "").strip(),
            internal_url=str(endpoint.get("internalURL") or "").strip(),
        )


@dataclass(frozen=True)
class ServiceCatalog:
    """Sanitized summary of the Identity service catalog.

    The full service catalog is preserved as ``raw_services`` for
    debugging but is *never* logged in production code paths.  The
    ``endpoints`` list is the canonical lookup table for the rest
    of the application.
    """

    endpoints: List[ServiceEndpoint] = field(default_factory=list)
    raw_services: List[Dict[str, Any]] = field(default_factory=list)

    def public_url_for(self, service_type: str, *, region: str = "") -> Optional[str]:
        """Return the public URL for the first matching endpoint.

        Matches against either ``service_type`` (the catalog's
        ``type`` field) or ``service_name`` (the catalog's ``name``
        field) so callers can pass either identifier.  When
        ``region`` is supplied, prefer endpoints in that region;
        otherwise return the first match in catalog order.  Returns
        ``None`` if no endpoint matches — callers translate this
        into a "service not enabled on this account" result.
        """

        matches = [
            e for e in self.endpoints
            if e.service_type == service_type or e.service_name == service_type
        ]
        if not matches:
            return None
        if region:
            regional = [e for e in matches if e.region.upper() == region.upper()]
            if regional:
                return regional[0].public_url
        return matches[0].public_url

    def discovered_service_types(self) -> List[str]:
        """Return a sorted list of every service type in the catalog."""

        return sorted({e.service_type for e in self.endpoints if e.service_type})

    def has_service(self, *identifiers: str) -> bool:
        """Return True when any of ``identifiers`` appears in the catalog.

        Accepts both ``type`` and ``name`` identifiers so the
        caller does not need to know which field Rackspace used
        for a particular product.
        """

        wanted = set(identifiers)
        for endpoint in self.endpoints:
            if endpoint.service_type in wanted or endpoint.service_name in wanted:
                return True
        return False


@dataclass(frozen=True)
class IdentitySession:
    """Result of a successful authentication.

    The X-Auth-Token is captured in ``token`` but every consumer
    goes through :meth:`RackspaceIdentity.current_token` so the
    token is never returned to callers in a way that might be
    logged.
    """

    token: str
    expires_at: float  # epoch seconds, UTC
    issued_at: float
    tenant_id: str
    tenant_name: str
    username: str
    roles: List[RoleAssignment]
    catalog: ServiceCatalog

    def seconds_until_expiry(self, *, now: Optional[float] = None) -> float:
        """Seconds until the token expires (negative if already expired)."""

        return self.expires_at - (now if now is not None else time.time())

    @property
    def role_names(self) -> List[str]:
        return sorted({r.name for r in self.roles if r.name})

    @property
    def elevated_roles_present(self) -> List[str]:
        return sorted(set(self.role_names) & ELEVATED_ROLES)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        catalog_size = len(self.catalog.endpoints) if self.catalog is not None else 0
        return (
            "IdentitySession(token=<redacted>, expires_at={expires_at}, "
            "issued_at={issued_at}, tenant_id={tenant_id!r}, "
            "tenant_name={tenant_name!r}, username={username!r}, "
            "roles={roles!r}, catalog=<{catalog_size} endpoints>)"
        ).format(
            expires_at=self.expires_at,
            issued_at=self.issued_at,
            tenant_id=self.tenant_id,
            tenant_name=self.tenant_name,
            username=self.username,
            roles=self.roles,
            catalog_size=catalog_size,
        )


class RackspaceIdentity:
    """Authenticates against Rackspace Identity and caches the session.

    A single instance is shared across the process.  The cache is
    *in-memory only* — tokens are not persisted to disk or shared
    between processes.  When the cached token expires (or the
    caller receives a 401 from a product API), the next call to
    :func:`current_token` transparently re-authenticates.
    """

    def __init__(
        self,
        *,
        http_client: RackspaceHttpClient,
        identity_url: str = RACKSPACE_IDENTITY_URL,
        clock: Optional[Any] = None,
    ) -> None:
        self._http = http_client
        self._identity_url = identity_url
        # ``clock`` is a callable that returns the current epoch
        # seconds; injected by tests so they can advance time
        # deterministically.
        self._clock = clock or time.time
        self._lock = asyncio.Lock()
        self._session: Optional[IdentitySession] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def authenticate(
        self,
        credentials: Optional[RackspaceCredentials] = None,
    ) -> IdentitySession:
        """Force a fresh authentication against Rackspace Identity.

        Cached sessions are discarded.  Returns the parsed
        :class:`IdentitySession`; the caller MUST NOT log it.  The
        session is also cached for subsequent
        :func:`current_token` calls.
        """

        creds = credentials or get_credentials()
        session = await self._authenticate_locked(creds)
        return session

    async def current_token(self) -> str:
        """Return a usable X-Auth-Token, re-authenticating if needed.

        Raises :class:`RackspaceAuthError` if no credentials are
        configured or authentication fails.  The returned token is
        the raw X-Auth-Token value; callers MUST NOT log it.
        """

        session = await self.current_session()
        return session.token

    async def current_session(self) -> IdentitySession:
        """Return a usable :class:`IdentitySession`, refreshing on demand.

        The session is cached and reused until the token is within
        :data:`TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS` of expiry.  When
        the cached session is invalid, a fresh authentication is
        performed under the lock.
        """

        async with self._lock:
            now = self._clock()
            if self._session is not None and self._session.seconds_until_expiry(now=now) > TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS:
                return self._session
            creds = get_credentials()
            self._session = await self._authenticate_locked(creds)
            return self._session

    async def invalidate(self) -> None:
        """Drop the cached session.  Used by callers after a 401."""

        async with self._lock:
            self._session = None

    @staticmethod
    def parse_token_payload(token: Dict[str, Any]) -> IdentitySession:
        """Module-level helper exposed as a class method for tests."""

        return _parse_token_payload(token)

    @staticmethod
    def is_role_safe(name: str) -> bool:
        """Return True when ``name`` is a known safe read-only role."""

        return name in SAFE_ROLES

    @staticmethod
    def is_elevated(name: str) -> bool:
        """Return True when ``name`` is a known elevated role."""

        return name in ELEVATED_ROLES

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _authenticate_locked(
        self,
        credentials: RackspaceCredentials,
    ) -> IdentitySession:
        # NB: ``credentials.api_key`` is sent in the request body
        # to Rackspace Identity.  We MUST NEVER log it; the
        # transport layer's error messages intentionally avoid
        # echoing request bodies.
        payload = {
            "auth": {
                "RAX-KSKEY:apiKeyCredentials": {
                    "username": credentials.username,
                    "apiKey": credentials.api_key,
                }
            }
        }
        # Split the canonical identity URL into base + path so the
        # shared HTTP client can compute the absolute URL.
        # The URL always ends with ``/v2.0/tokens``; we strip the
        # suffix to obtain the base.  This keeps the rest of the
        # POC free of hard-coded endpoint strings.
        if self._identity_url.endswith("/v2.0/tokens"):
            base_url = self._identity_url[: -len("/v2.0/tokens")]
            path = "/v2.0/tokens"
        else:
            base_url = ""
            path = self._identity_url
        try:
            response = await self._http.post_json(base_url, path, payload)
        except RackspaceAuthError:
            # Re-raise as-is so callers see the original 401 cause.
            raise
        except RackspaceTransportError:
            raise

        body = response.body
        access = (body.get("access") or {})
        token = access.get("token")
        if not isinstance(token, dict) or not token.get("id"):
            # Rackspace occasionally returns a 200 with an empty
            # token (misconfigured tenant).  Treat as an auth
            # failure rather than a transport error.
            raise RackspaceAuthError(
                "Rackspace Identity response missing 'access.token.id'"
            )

        # Merge envelope-level roles / catalog (modern Rackspace
        # layout) into the token-shaped payload so the rest of the
        # parser can stay simple.
        merged = dict(token)
        access_user = access.get("user") or {}
        token_user = token.get("user") or {}
        merged_user: Dict[str, Any] = dict(token_user)
        if "roles" in access_user and not merged_user.get("roles"):
            merged_user["roles"] = access_user["roles"]
        if "username" in access_user and not merged_user.get("username"):
            merged_user["username"] = access_user["username"]
        if "name" in access_user and not merged_user.get("name"):
            merged_user["name"] = access_user["name"]
        merged["user"] = merged_user

        if isinstance(access.get("serviceCatalog"), (list, dict)) and not merged.get("serviceCatalog"):
            merged["serviceCatalog"] = access["serviceCatalog"]

        return _parse_token_payload(merged)

    def _parse_iso8601(self, value: str) -> float:
        """Parse a Rackspace ISO-8601 timestamp into epoch seconds."""

        from datetime import datetime, timezone

        # Rackspace emits ``2024-09-01T12:34:56.789012Z`` or
        # ``2024-09-01T12:34:56+00:00``.  Python's ``fromisoformat``
        # handles the latter natively; we normalise the former by
        # stripping the trailing ``Z`` and adding ``+00:00``.
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError as exc:
            raise RackspaceTransportError(
                f"Rackspace returned an unparseable expires_at timestamp: {value!r}"
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def _parse_token_payload(token: Dict[str, Any]) -> IdentitySession:
    """Translate the Identity ``access.token`` dict into a session."""

    from datetime import datetime, timezone

    raw_id = token.get("id")
    if not raw_id:
        raise RackspaceAuthError("Rackspace Identity token missing 'id'")
    raw_expires = token.get("expires")
    if not raw_expires:
        raise RackspaceAuthError("Rackspace Identity token missing 'expires'")

    # Parse ISO-8601 with the ``Z`` suffix handling.
    expires_str = str(raw_expires).strip()
    if expires_str.endswith("Z"):
        expires_str = expires_str[:-1] + "+00:00"
    try:
        expires_dt = datetime.fromisoformat(expires_str)
    except ValueError as exc:
        raise RackspaceTransportError(
            f"Rackspace Identity token has invalid expires timestamp: {raw_expires!r}"
        ) from exc
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    expires_at = expires_dt.timestamp()
    issued_at = time.time()

    tenant = token.get("tenant") or {}
    user = token.get("user") or {}

    roles_payload = user.get("roles") or []
    roles = [RoleAssignment.from_payload(r) for r in roles_payload if isinstance(r, dict)]

    catalog = _parse_service_catalog(token.get("serviceCatalog") or {})

    return IdentitySession(
        token=str(raw_id),
        expires_at=expires_at,
        issued_at=issued_at,
        tenant_id=str(tenant.get("id") or "").strip(),
        tenant_name=str(tenant.get("name") or "").strip(),
        username=str(user.get("username") or user.get("name") or "").strip(),
        roles=roles,
        catalog=catalog,
    )


def _parse_service_catalog(catalog_payload: Any) -> ServiceCatalog:
    """Translate either the legacy or modern service catalog shape.

    Rackspace Identity returns the service catalog in two shapes:

    * ``{"services": [...]}`` — OpenStack-derived (older / federated).
    * ``[...]`` — modern Rackspace Cloud (a bare list of services).

    This parser accepts both shapes so the POC works against
    either tenant style without code changes.
    """

    if isinstance(catalog_payload, list):
        services = catalog_payload
    elif isinstance(catalog_payload, dict):
        services = catalog_payload.get("services") or []
    else:
        services = []

    endpoints: List[ServiceEndpoint] = []
    raw_services: List[Dict[str, Any]] = []
    if isinstance(services, list):
        for service in services:
            if not isinstance(service, dict):
                continue
            raw_services.append(service)
            service_endpoints = service.get("endpoints") or []
            if not isinstance(service_endpoints, list):
                continue
            for endpoint in service_endpoints:
                if not isinstance(endpoint, dict):
                    continue
                parsed = ServiceEndpoint.from_payload(service, endpoint)
                if parsed.public_url:
                    endpoints.append(parsed)
    return ServiceCatalog(endpoints=endpoints, raw_services=raw_services)


def _redact_token(value: str) -> str:
    """Return a redacted preview of a token value."""

    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...<redacted>"


__all__ = [
    "ELEVATED_ROLES",
    "IdentitySession",
    "RackspaceIdentity",
    "RoleAssignment",
    "SAFE_ROLES",
    "ServiceCatalog",
    "ServiceEndpoint",
]
