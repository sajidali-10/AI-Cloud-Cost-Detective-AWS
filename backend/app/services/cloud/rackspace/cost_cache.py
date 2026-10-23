"""Rackspace cost read-through cache — Phase 9B.1.

A thin wrapper over the shared FRESH + STALE-LAST-KNOWN-GOOD
machinery in :mod:`app.services.cloud.azure.cost_cache` (Phase
7D.3.3).  The contract is identical; only the namespace and the
storage-account derivation differ:

* every Rackspace row lives under ``cache_namespace = "rackspace"``
  so AWS / Azure / Rackspace rows can never collide on the same
  ``(account_id, cache_key)`` tuple;
* the storage account id is derived from the canonical scope with
  the tenant id hashed (SHA-256, first 32 hex chars) — no plaintext
  identifiers are persisted;
* TTL knobs are env-driven:

  * ``RACKSPACE_COST_CACHE_FRESH_TTL_SECONDS`` (default ``600`` —
    10 minutes, matching the established Azure convention; the
    estimated-charge snapshot advances during the billing cycle,
    so a conservative fresh window keeps totals current without
    hammering the Billing API)
  * ``RACKSPACE_COST_CACHE_STALE_TTL_SECONDS`` (default ``3600`` —
    1 hour of bounded stale fallback)

Authoritative data rule
-----------------------

Only a valid successful snapshot may advance the last-known-good
payload.  A true ``$0`` response IS authoritative.  Authentication
(401), permission (403), not-found (404), throttling (429), timeout,
5xx, and malformed responses NEVER overwrite last-good data — they
propagate as typed errors and the cache serves the still-valid
LKG payload as STALE when one exists.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from app.services.cloud.azure.cost_cache import (
    AzureCacheEntry,
    AzureLoader,
    get_or_refresh_azure_canonical,
)


logger = logging.getLogger("cost-detective.rackspace.cost_cache")


#: Cache namespace for every Rackspace canonical cost row.
CACHE_NAMESPACE: str = "rackspace"


def _fresh_ttl_seconds() -> int:
    return int(os.getenv("RACKSPACE_COST_CACHE_FRESH_TTL_SECONDS", "600"))


def _stale_ttl_seconds() -> int:
    return int(os.getenv("RACKSPACE_COST_CACHE_STALE_TTL_SECONDS", "3600"))


def build_rackspace_storage_account_id(scope: str) -> str:
    """Translate ``scope`` into a storage_id for the ``cost_cache`` table.

    Mirrors the Azure derivation: ``rackspace:all`` for the
    provider-wide scope, ``rackspace:acct:<sha256(tenant)>`` for a
    single-account scope.  The tenant id is hashed so a leaked DB
    row cannot be re-correlated to the Cloud account identifier —
    and the Billing Account Number (RAN) is never part of any cache
    key at all.
    """

    if scope == "all":
        return "rackspace:all"
    if scope.startswith("acct:"):
        tenant = scope[len("acct:"):]
        digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:32]
        return f"rackspace:acct:{digest}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]
    return f"rackspace:{digest}"


async def get_or_refresh_rackspace_canonical(
    *,
    scope: str,
    region: str,
    kind: str,
    signature: str,
    loader: AzureLoader,
    db: Optional[Any] = None,
    fresh_ttl_seconds: Optional[int] = None,
    stale_ttl_seconds: Optional[int] = None,
) -> AzureCacheEntry:
    """Read-through cache lookup for the canonical Rackspace cost envelope.

    Delegates to the shared machinery with the Rackspace namespace,
    hashed storage id, provider-tagged cache key, and env-driven
    TTLs.  See the module docstring for the authoritative-data
    contract.
    """

    fresh = fresh_ttl_seconds if fresh_ttl_seconds is not None else _fresh_ttl_seconds()
    stale = stale_ttl_seconds if stale_ttl_seconds is not None else _stale_ttl_seconds()
    return await get_or_refresh_azure_canonical(
        scope=scope,
        region=region,
        kind=kind,
        signature=signature,
        provider="RACKSPACE",
        loader=loader,
        db=db,
        fresh_ttl_seconds=fresh,
        stale_ttl_seconds=stale,
        cache_namespace=CACHE_NAMESPACE,
        storage_account_id=build_rackspace_storage_account_id(scope),
    )


__all__ = [
    "CACHE_NAMESPACE",
    "build_rackspace_storage_account_id",
    "get_or_refresh_rackspace_canonical",
]
