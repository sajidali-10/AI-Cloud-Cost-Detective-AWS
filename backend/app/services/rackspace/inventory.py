"""Rackspace multi-region read-only inventory client — Phase 9B.2.

Extends the Phase 9A transport/authentication infrastructure with the
product-collection primitives the canonical resource surface needs:

* **Service catalog resolution** — translate a service identifier
  (``cloudServersOpenStack``, ``volumev2``, ``rax:load-balancer``,
  ...) into the set of *regional* public endpoints the Identity
  catalog publishes for the authenticated tenant.  No regional
  product URL is ever hard-coded here; the catalog is the single
  source of truth (the Identity URL itself is the one pinned
  endpoint, exactly as in Phase 9A).
* **Paginated collection fetching** — a generic marker-based page
  walker for the OpenStack-style list endpoints
  (``/servers/detail``, ``/flavors/detail``, ``/volumes/detail``,
  ``/loadbalancers``).  Pagination links appear as
  ``<collection>_links: [{"rel": "next", "href": "...?marker=X"}]``
  (Nova uses ``servers_links``, Cinder ``volumes_links``, Cloud Load
  Balancers ``loadBalancer_links``); the walker accepts all three
  shapes plus a bare ``links`` key.

Read-only guarantee
-------------------

Exactly like :class:`~app.services.rackspace.servers.RackspaceServersClient`,
this module exposes no product write verbs.  The underlying
:class:`~app.services.rackspace.client.RackspaceHttpClient` only
implements ``get`` (plus the Identity authentication ``POST``), so no
POST / PUT / PATCH / DELETE against a product API can be issued
through this client.  The Phase 9B.2 validator asserts this surface.

Security
--------

Tokens are obtained per-request from the shared
:class:`~app.services.rackspace.identity.RackspaceIdentity` client
via the ``token_provider`` callback and are never stored here.
Nothing in this module logs credentials, tokens, the RAN, or raw
response bodies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

from app.services.rackspace.client import RackspaceHttpClient
from app.services.rackspace.identity import IdentitySession, RackspaceIdentity

logger = logging.getLogger("cost-detective.rackspace.inventory")


#: Hard upper bound on pages fetched per collection.  Defensive only —
#: the walker terminates on "no next link" / "empty page" / "marker
#: did not advance" long before this in practice.  Prevents unbounded
#: loops against a hostile or misbehaving endpoint.
MAX_PAGES_PER_COLLECTION = 50


@dataclass(frozen=True)
class CatalogEndpoint:
    """One regional public endpoint resolved from the Identity catalog."""

    region: str  # normalised upper-case ("DFW", "ORD", ...) — may be ""
    service_type: str
    service_name: str
    public_url: str


def _dedupe_endpoints(endpoints: Sequence[CatalogEndpoint]) -> List[CatalogEndpoint]:
    """Drop duplicate URLs (a service may publish alias entries)."""
    seen: set = set()
    out: List[CatalogEndpoint] = []
    for endpoint in endpoints:
        if not endpoint.public_url or endpoint.public_url in seen:
            continue
        seen.add(endpoint.public_url)
        out.append(endpoint)
    return out


def resolve_service_endpoints(
    session: IdentitySession,
    service_identifiers: Sequence[str],
) -> List[CatalogEndpoint]:
    """Return every regional catalog endpoint matching ``identifiers``.

    Matches on either the catalog ``type`` or ``name`` field (the
    Phase 9A convention — Rackspace has shipped both shapes across
    its product generations).  Deduplicates by public URL and
    preserves catalog order so the canonical aggregation is
    deterministic.  An empty result means the tenant's catalog does
    not offer the service — the canonical layer translates that into
    an explicit per-service failure, never into a silent empty
    inventory.
    """

    wanted = {identifier for identifier in service_identifiers if identifier}
    matches = [
        CatalogEndpoint(
            region=(entry.region or "").strip().upper(),
            service_type=entry.service_type,
            service_name=entry.service_name,
            public_url=entry.public_url,
        )
        for entry in session.catalog.endpoints
        if entry.service_type in wanted or entry.service_name in wanted
    ]
    return _dedupe_endpoints(matches)


def _candidate_link_keys(collection_key: str) -> List[str]:
    """Link-envelope key candidates for a Nova/Cinder/LB collection.

    * ``servers``      → ``servers_links`` (Nova)
    * ``volumes``      → ``volumes_links`` (Cinder)
    * ``flavors``      → ``flavors_links`` (Nova)
    * ``loadBalancers``→ ``loadBalancers_links`` / ``loadBalancer_links`` (CLB v1)
    * any collection   → ``links`` (generic fallback)
    """

    keys = [f"{collection_key}_links", "links"]
    if collection_key.endswith("s"):
        keys.insert(1, f"{collection_key[:-1]}_links")
    return keys


def extract_next_marker(body: Dict[str, Any], collection_key: str) -> Optional[str]:
    """Extract the ``marker`` of the next page from a response body.

    Returns ``None`` when the body carries no usable next link (last
    page).  A malformed link entry (missing href / unparseable query)
    is treated as "no next page" so pagination can never loop
    unbounded on a hostile payload.
    """

    links: List[Any] = []
    for key in _candidate_link_keys(collection_key):
        value = body.get(key)
        if isinstance(value, list):
            links = value
            break
    for link in links:
        if not isinstance(link, dict):
            continue
        rel = str(link.get("rel") or "").lower()
        if rel != "next":
            continue
        # Some implementations inline the marker on the link object.
        inline_marker = link.get("marker")
        if isinstance(inline_marker, (str, int)) and str(inline_marker).strip():
            return str(inline_marker).strip()
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        try:
            query = parse_qs(urlsplit(href).query)
        except ValueError:
            continue
        values = query.get("marker") or []
        if values and str(values[0]).strip():
            return str(values[0]).strip()
    return None


class RackspaceInventoryClient:
    """Read-only, catalog-driven, multi-region inventory fetcher."""

    def __init__(
        self,
        *,
        http_client: RackspaceHttpClient,
        identity: RackspaceIdentity,
        max_pages: int = MAX_PAGES_PER_COLLECTION,
    ) -> None:
        self._http = http_client
        self._identity = identity
        self._max_pages = max_pages

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def current_session(self) -> IdentitySession:
        """Return the cached Identity session (authenticating on demand)."""

        return await self._identity.current_session()

    def endpoints_for(
        self, session: IdentitySession, service_identifiers: Sequence[str]
    ) -> List[CatalogEndpoint]:
        """Public regional endpoints for a service (catalog-resolved)."""

        return resolve_service_endpoints(session, service_identifiers)

    async def fetch_collection(
        self,
        *,
        base_url: str,
        path: str,
        collection_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch every page of an OpenStack-style list endpoint.

        Returns the concatenated raw item dicts (shape validation is
        the canonical layer's job).  Issues at most
        ``self._max_pages`` GETs; terminates early when the body
        carries no next link, a page is empty, the marker fails to
        advance, or a page repeats only already-seen ids (duplicate
        prevention).  Transport failures propagate as the Phase 9A
        typed exceptions.
        """

        items: List[Dict[str, Any]] = []
        seen_ids: set = set()
        marker: Optional[str] = None
        for _page in range(self._max_pages):
            params: Optional[Dict[str, Any]] = (
                {"marker": marker} if marker else None
            )
            response = await self._http.get(
                base_url,
                path,
                token_provider=self._identity.current_token,
                params=params,
            )
            body = response.body
            batch = body.get(collection_key)
            if not isinstance(batch, list):
                # Shape validation belongs to the canonical layer, but a
                # non-list collection makes pagination meaningless — stop
                # here and let the caller decide what to do with the
                # pages already fetched.
                break
            new_items: List[Dict[str, Any]] = []
            for item in batch:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id") or "").strip()
                if item_id:
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                new_items.append(item)
            items.extend(new_items)

            if not new_items:
                # Empty page or pure duplicates — nothing further to fetch.
                break
            next_marker = extract_next_marker(body, collection_key)
            if not next_marker or next_marker == marker:
                break
            marker = next_marker
        return items

    async def fetch_json(
        self,
        *,
        base_url: str,
        path: str,
    ) -> Dict[str, Any]:
        """Issue a single authenticated GET and return the raw body."""

        response = await self._http.get(
            base_url,
            path,
            token_provider=self._identity.current_token,
        )
        return response.body

    async def reauthenticate_after_401(self) -> None:
        """Drop the cached Identity session (retry after a product 401).

        Mirrors the Phase 9A servers client behaviour: a 401 from a
        product endpoint invalidates the cached token so the next
        call re-authenticates.  Re-raising the original error is the
        caller's choice — this helper only clears local state.
        """

        await self._identity.invalidate()


__all__ = [
    "CatalogEndpoint",
    "MAX_PAGES_PER_COLLECTION",
    "RackspaceInventoryClient",
    "extract_next_marker",
    "resolve_service_endpoints",
]
