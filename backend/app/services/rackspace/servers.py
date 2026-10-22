"""Rackspace Cloud Servers client — Phase 9A POC (read-only).

Wraps two read-only endpoints:

* ``GET /servers`` — minimal list (server name + id + links).
* ``GET /servers/detail`` — full detail (status, flavor, image,
  addresses, etc.).

The client is read-only by construction — there are no public
methods that issue ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE``.  The
Phase 9A validator script enforces this contract by importing the
module and asserting that only ``GET`` and ``HEAD`` verbs are
present in the public surface.

Returned aggregate information
------------------------------

The :meth:`get_aggregate_summary` method returns a sanitized
:class:`CloudServersSummary` that contains:

* total server count,
* status counts (``ACTIVE``, ``BUILD``, ``ERROR``, ...),
* regions discovered from server ``DC`` / region metadata,
* flavor IDs (the SKU labels, not their per-server metadata).

It deliberately excludes IP addresses, hostnames, server names,
and any user-data payload.  These fields can contain
customer-sensitive information and are not part of the POC.
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


#: Identifiers Rackspace ships in the Identity catalog for Cloud
#: Servers.  The ``name`` field is stable across tenants; the
#: ``type`` field differs between the modern (``compute``) and
#: legacy (``cloudServers`` / ``cloudServersOpenStack``) catalog
#: shapes.  Phase 9A does not hard-code the endpoint URL — it
#: resolves the URL from the catalog by matching either field.
SERVERS_SERVICE_NAMES: List[str] = [
    "cloudServersOpenStack",
    "cloudServers",
    "compute",
]

#: Backwards-compatible alias used by tests and external callers
#: that prefer the ``type`` field label.
SERVERS_SERVICE_TYPES: List[str] = list(SERVERS_SERVICE_NAMES)


@dataclass(frozen=True)
class CloudServersSummary:
    """Sanitized aggregate summary of the Cloud Servers inventory."""

    total_count: int
    status_counts: Dict[str, int] = field(default_factory=dict)
    regions: List[str] = field(default_factory=list)
    flavor_ids: List[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_count": self.total_count,
            "status_counts": dict(sorted(self.status_counts.items())),
            "regions": sorted(self.regions),
            "flavor_ids": sorted(set(self.flavor_ids)),
        }


class RackspaceServersClient:
    """Read-only client for Rackspace Cloud Servers."""

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

    async def list_servers(self) -> Dict[str, Any]:
        """``GET /servers`` — minimal list.

        Returns the raw ``{"servers": [...]}`` envelope (the list
        contains only ``id``, ``name``, and ``links`` entries).
        Callers that want a sanitized summary should use
        :meth:`get_aggregate_summary`.
        """

        return await self._get_collection(path="/servers")

    async def list_servers_detail(self) -> Dict[str, Any]:
        """``GET /servers/detail`` — full detail."""

        return await self._get_collection(path="/servers/detail")

    async def get_aggregate_summary(self) -> CloudServersSummary:
        """Return a sanitized aggregate summary of the tenant's servers.

        Uses :meth:`list_servers_detail` so status / flavor are
        available without an extra round-trip.  IP addresses and
        user-data payloads are explicitly excluded.
        """

        try:
            payload = await self.list_servers_detail()
        except RackspaceAuthError as exc:
            return CloudServersSummary(
                total_count=0,
                detail=f"AUTH_FAILED: {exc.message}",
            )
        except RackspacePermissionError as exc:
            return CloudServersSummary(
                total_count=0,
                detail=f"PERMISSION_DENIED: {exc.message}",
            )
        except RackspaceNotFoundError as exc:
            return CloudServersSummary(
                total_count=0,
                detail=f"NOT_FOUND: {exc.message}",
            )
        except RackspaceTransportError as exc:
            return CloudServersSummary(
                total_count=0,
                detail=f"TRANSPORT_ERROR: {exc.message}",
            )
        except RackspaceConfigError as exc:
            return CloudServersSummary(
                total_count=0,
                detail=f"NOT_CONFIGURED: {exc.message}",
            )

        servers = payload.get("servers") or []
        if not isinstance(servers, list):
            return CloudServersSummary(
                total_count=0,
                detail="unexpected response shape",
            )

        status_counts: Dict[str, int] = {}
        regions: set = set()
        flavor_ids: List[str] = []
        for server in servers:
            if not isinstance(server, dict):
                continue
            status = str(server.get("status") or "").upper()
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
            region = (
                server.get("region")
                or (server.get("links") or [{}])[0].get("region")  # type: ignore[index]
                or server.get("OS-EXT-AZ:availability_zone")
                or ""
            )
            if isinstance(region, str) and region:
                regions.add(region.upper())
            flavor = server.get("flavor")
            if isinstance(flavor, dict):
                flavor_id = flavor.get("id") or flavor.get("links", [{}])[0].get("id")  # type: ignore[index]
                if flavor_id:
                    flavor_ids.append(str(flavor_id))

        return CloudServersSummary(
            total_count=len(servers),
            status_counts=status_counts,
            regions=sorted(regions),
            flavor_ids=flavor_ids,
            detail="",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_collection(self, *, path: str) -> Dict[str, Any]:
        session = await self._identity.current_session()

        base_url: Optional[str] = None
        for service_type in SERVERS_SERVICE_TYPES:
            url = session.catalog.public_url_for(service_type)
            if url:
                base_url = url
                break
        if not base_url:
            raise RackspaceNotFoundError(
                "Cloud Servers service not present in Identity catalog"
            )

        token_provider = self._identity.current_token
        try:
            response = await self._http.get(
                base_url, path, token_provider=token_provider,
            )
        except RackspaceAuthError:
            await self._identity.invalidate()
            raise
        # Return the raw envelope so callers can perform their
        # own summarisation.  Read-only guarantee is enforced by
        # the verb surface of :class:`RackspaceHttpClient`.
        return response.body


__all__ = [
    "CloudServersSummary",
    "RackspaceServersClient",
    "SERVERS_SERVICE_TYPES",
]
