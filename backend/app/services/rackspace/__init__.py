"""Rackspace read-only client — Phase 9A POC.

The :mod:`app.services.rackspace` package is intentionally *not*
registered with :class:`app.services.cloud.registry.ProviderRegistry`
during Phase 9A.  It is a stand-alone proof-of-concept that proves
secure read-only integration against Rackspace Cloud APIs without
touching the existing AWS / Azure provider model, dashboard, or
optimization engine.

Public surface
--------------

* :func:`get_identity_client` returns a process-wide
  :class:`RackspaceIdentity` client.
* :func:`get_billing_client` returns a process-wide
  :class:`RackspaceBillingClient` bound to the shared HTTP client
  and identity client.
* :func:`get_servers_client` returns a process-wide
  :class:`RackspaceServersClient`.
* :func:`reset_for_tests` drops the cached singletons (used by
  the unit tests to inject a fresh HTTP client).
"""

from __future__ import annotations

from typing import Optional

from app.services.rackspace.billing import RackspaceBillingClient
from app.services.rackspace.client import RackspaceHttpClient
from app.services.rackspace.identity import RackspaceIdentity
from app.services.rackspace.inventory import RackspaceInventoryClient
from app.services.rackspace.servers import RackspaceServersClient


_identity: Optional[RackspaceIdentity] = None
_http: Optional[RackspaceHttpClient] = None
_billing: Optional[RackspaceBillingClient] = None
_servers: Optional[RackspaceServersClient] = None
_inventory: Optional[RackspaceInventoryClient] = None


def _get_http() -> RackspaceHttpClient:
    global _http
    if _http is None:
        _http = RackspaceHttpClient()
    return _http


def get_identity_client() -> RackspaceIdentity:
    """Return the process-wide :class:`RackspaceIdentity` client."""

    global _identity
    if _identity is None:
        _identity = RackspaceIdentity(http_client=_get_http())
    return _identity


def get_billing_client() -> RackspaceBillingClient:
    """Return the process-wide billing client."""

    global _billing
    if _billing is None:
        _billing = RackspaceBillingClient(
            http_client=_get_http(),
            identity=get_identity_client(),
        )
    return _billing


def get_servers_client() -> RackspaceServersClient:
    """Return the process-wide cloud-servers client."""

    global _servers
    if _servers is None:
        _servers = RackspaceServersClient(
            http_client=_get_http(),
            identity=get_identity_client(),
        )
    return _servers


def get_inventory_client() -> RackspaceInventoryClient:
    """Return the process-wide multi-region inventory client (Phase 9B.2).

    Shares the SAME HTTP transport and Identity token cache as the
    Phase 9A product clients — there is exactly one Rackspace
    authentication implementation in this codebase.
    """

    global _inventory
    if _inventory is None:
        _inventory = RackspaceInventoryClient(
            http_client=_get_http(),
            identity=get_identity_client(),
        )
    return _inventory


def reset_for_tests() -> None:
    """Drop every cached singleton.  Tests only."""

    global _identity, _http, _billing, _servers, _inventory
    _identity = None
    _http = None
    _billing = None
    _servers = None
    _inventory = None


__all__ = [
    "RackspaceBillingClient",
    "RackspaceHttpClient",
    "RackspaceIdentity",
    "RackspaceInventoryClient",
    "RackspaceServersClient",
    "get_billing_client",
    "get_identity_client",
    "get_inventory_client",
    "get_servers_client",
    "reset_for_tests",
]
