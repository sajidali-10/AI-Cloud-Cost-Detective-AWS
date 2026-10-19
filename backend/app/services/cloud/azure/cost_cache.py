"""Azure cost read-through cache with FRESH + STALE-LAST-GOOD (raw SQL).

Phase 7D.3.3 introduces server-side caching for the canonical
Azure cost envelope.  The module is **separate from** the
Phase-2 AWS read-through cache (``app.services.cost_cache``)
because the contract is wider:

* Azure responses must distinguish "fresh" from "stale fallback"
  so the frontend can render "Stale · last successful <time>"
  instead of pretending the live total is authoritative.
* Azure throttling (HTTP 429) is the dominant operational
  hazard, so the cache layer absorbs one refresh failure
  without losing the previously-authoritative payload.
* Azure keys are namespaced separately so AWS and Azure rows can
  never collide on the same ``(account_id, cache_key)`` tuple.

Implementation note
-------------------

The cache reads / writes ``cost_cache`` rows via raw SQL
(``session.execute(text(...))``) instead of the SQLAlchemy ORM
because the deployed environment has a pre-existing
SQLAlchemy 2.0.36 + Python 3.14 typing bug in
``_extract_mappable_attributes`` that breaks the declarative
mapper for any model that uses ``Mapped[X | None]``.  Raw SQL
sidesteps the broken mapper path entirely while still using the
existing ``cost_cache`` table and Alembic migration.

The contract
------------

``get_or_refresh_azure_canonical(...)`` returns a
:class:`AzureCacheEntry` whose ``cache_status`` is one of:

* ``HIT`` — fresh row in cache, ``now < expires_at``, no upstream
  call made.  ``data_freshness = CACHED``.
* ``REFRESHED`` — cache miss (or stale + refresh succeeded).
  Upstream call made, ``payload`` + ``stale_payload`` updated.
  ``data_freshness = LIVE``.
* ``STALE`` — ``now >= expires_at``, refresh returned a throttle /
  5xx, ``stale_payload`` exists AND ``now < stale_until``.  The
  LKG payload is returned; ``data_freshness = STALE``;
  ``last_successful_at`` is preserved.
* ``MISS`` — no cache row AND loader raised a Cost Management
  error.  No LKG exists; ``data_freshness = UNAVAILABLE`` and
  the original error is re-raised so the route layer can map it
  to a 429 / 502 HTTP response.

Single-flight
-------------

Same-key concurrent calls coalesce on a single
:class:`asyncio.Lock` keyed by
``(cache_namespace, storage_account_id, cache_key)``.  Different
keys are independent — MCPP, Sponsorship, All Azure, October,
September, MCPP-with-region all have distinct cache keys.  AWS
keys are namespaced to ``"aws"`` and never collide with
``"azure"`` keys.

Failure semantics
-----------------

* A loader exception is **never cached**.  Only authoritative
  successes (see :func:`_is_authoritative_success`) advance the
  last-known-good payload.  Throttled zeros, all-failed
  responses, and 5xx responses NEVER overwrite a known-good row.
* A DB exception during the read is **not fatal**.  The cache
  layer logs a warning and falls back to the loader; this
  prevents the cost-cache DB from becoming a single point of
  failure for the cost API.
* Successful ``$0.00`` totals (an authoritative
  ``status='SUCCESS', successful_accounts>0`` envelope with
  ``total_cost='0.00'``) ARE cached as authoritative LKG.  This
  preserves Phase 7D.3.2 semantics.

Configuration
-------------

All TTL knobs are env-driven so the deployment can tune them
without code changes:

* ``AZURE_COST_CACHE_FRESH_TTL_SECONDS`` (default ``600`` — 10 min)
* ``AZURE_COST_CACHE_STALE_TTL_SECONDS`` (default ``3600`` — 1 h)

Security
--------

* No secrets are cached.  The payload is the provider-neutral
  ``CloudCostReport`` envelope, already sanitised at the
  adapter boundary.
* No bearer tokens, federated JWTs, or auth headers are ever
  logged.
* Cache keys are SHA-256 hashes of the input dimensions — they
  never expose subscription GUIDs, account ids, or any other
  identifying material.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

logger = logging.getLogger("cost-detective.azure.cost_cache")


CACHE_NAMESPACE: str = "azure"


# We type the ``db`` parameter as ``Any`` (not ``sqlalchemy.orm.Session``)
# so importing this module does NOT trigger the SQLAlchemy ORM mapper
# scan.  The deployed environment has a pre-existing
# SQLAlchemy 2.0.36 + Python 3.14 typing bug in
# ``_extract_mappable_attributes`` that breaks ``CostCache`` (and any
# other model with ``Mapped[X | None]``).  We sidestep the broken path
# entirely by reading / writing via raw SQL.


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------


def _fresh_ttl_seconds() -> int:
    return int(os.getenv("AZURE_COST_CACHE_FRESH_TTL_SECONDS", "600"))


def _stale_ttl_seconds() -> int:
    return int(os.getenv("AZURE_COST_CACHE_STALE_TTL_SECONDS", "3600"))


# ---------------------------------------------------------------------------
# Public enums
# ---------------------------------------------------------------------------


class AzureCacheStatus(str, Enum):
    """Public status for an Azure cache lookup."""

    HIT = "HIT"
    REFRESHED = "REFRESHED"
    STALE = "STALE"
    MISS = "MISS"


class AzureDataFreshness(str, Enum):
    """Coarse freshness signal the frontend renders."""

    LIVE = "LIVE"
    CACHED = "CACHED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AzureCacheEntry:
    """The result of a cache lookup.

    ``payload`` is the cached report envelope (a dict that the
    route layer hands to ``CloudCostReport.model_validate``).
    ``cache_status`` and ``data_freshness`` are surfaced through
    the API.  ``last_successful_at`` is ``None`` for MISS; for
    HIT / REFRESHED it is the row's ``last_successful_at``
    column; for STALE it is preserved from the existing row.
    ``azure_post_count`` is the count of upstream POSTs in the
    last successful refresh (``0`` for HIT).
    """

    payload: dict
    cache_status: AzureCacheStatus
    data_freshness: AzureDataFreshness
    last_successful_at: Optional[datetime] = None
    azure_post_count: int = 0
    cache_key: str = ""
    storage_account_id: str = ""


# ---------------------------------------------------------------------------
# Single-flight (in-process)
# ---------------------------------------------------------------------------


_Locks: Dict[Tuple[str, str, str], asyncio.Lock] = {}
_LocksGuard = asyncio.Lock()


async def _lock_for(
    cache_namespace: str,
    storage_account_id: str,
    cache_key: str,
) -> asyncio.Lock:
    """Return (or create) the in-process single-flight lock for
    this cache key.
    """
    async with _LocksGuard:
        key = (cache_namespace, storage_account_id, cache_key)
        lock = _Locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _Locks[key] = lock
        return lock


# ---------------------------------------------------------------------------
# Cache key + storage_id derivation
# ---------------------------------------------------------------------------


def build_azure_cache_key(
    *,
    provider: str,
    scope: str,
    region: str,
    kind: str,
    signature: str,
) -> str:
    """Build the SHA-256 cache key for an Azure canonical request."""
    raw = "|".join(
        ["v1", provider.upper(), scope, region, kind, signature]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def build_azure_storage_account_id(scope: str) -> str:
    """Translate ``scope`` (e.g. ``sub:<guid>`` or ``all``) into a
    storage_id used in the ``cost_cache.account_id`` column.

    Azure keys are stored under ``azure:sub:<guid>`` /
    ``azure:all`` so the storage column agrees with the
    ``cache_namespace`` discriminator and no future AWS row
    could collide with an Azure row.
    """
    if scope == "all":
        return "azure:all"
    if scope.startswith("sub:"):
        sub = scope[len("sub:"):]
        # Never persist a raw GUID — hash it so a leaked DB row
        # can't be re-correlated to a subscription id.
        h = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:32]
        return f"azure:sub:{h}"
    return f"azure:{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:32]}"


# ---------------------------------------------------------------------------
# Authoritative-success predicate
# ---------------------------------------------------------------------------


def _is_authoritative_success(payload: dict) -> bool:
    """Decide whether a payload may advance the LKG.

    Rules follow Phase 7D.3.2 + Phase 7D.3.3 §13:

    * ``status='SUCCESS'`` AND ``successful_accounts > 0``  → LKG
    * ``status='PARTIAL_SUCCESS'`` AND ``successful_accounts > 0`` → LKG
    * ``status='NO_DATA'`` with no account errors AND ``successful > 0`` → LKG
    * everything else (throttled, all-failed, ERROR) → NOT LKG

    The payload is the serialised canonical Azure envelope
    (``CloudCostReport.model_dump(mode='json')``), which is a flat
    dict with ``status``, ``successful_accounts``, ``failed_accounts``,
    ``account_errors`` at the top level — NOT nested under
    ``payload['report']``.  Phase 7D.3.3 (closure fix) had a bug
    here: the predicate read ``payload.get('status')`` which always
    returned ``None`` (the field is at ``payload['status']`` on the
    flat envelope), so EVERY response was classified as non-LKG
    and the stale-LKG fallback was never promoted.  That made the
    `stale_payload` column never advance for invalid throttled-zero
    responses either — but the bad response was still written to
    the ``payload`` column as a fresh entry, so the next call HIT
    it.  This fix restores the contract.
    """
    status = str(payload.get("status", "")).upper()
    successful = payload.get("successful_accounts")
    failed = payload.get("failed_accounts")
    account_errors = payload.get("account_errors") or []
    if successful is None:
        successful = 1 if status == "SUCCESS" else 0
    if failed is None:
        failed = len(account_errors)
    if status == "SUCCESS" and successful > 0:
        return True
    if status == "PARTIAL_SUCCESS" and successful > 0:
        return True
    if status == "NO_DATA" and successful > 0:
        return True
    return False


# ---------------------------------------------------------------------------
# Raw-SQL cache operations (avoid the broken SQLAlchemy ORM mapper).
# ---------------------------------------------------------------------------


# Read a single cache row by (namespace, storage_account_id, cache_key).
# Returns a dict with the row fields, or None if not present.
_SELECT_ROW_SQL = text(
    """
    SELECT
        payload,
        expires_at,
        last_successful_at,
        stale_payload,
        stale_until,
        azure_post_count,
        created_at
    FROM cost_cache
    WHERE cache_namespace = :namespace
      AND account_id = :account_id
      AND cache_key = :cache_key
    LIMIT 1
    """
)

# We deliberately do NOT use ``CAST(:payload AS jsonb)`` because
# SQLite (the in-memory test backend) does not understand ``jsonb``
# and silently truncates the value to ``'0'``.  Postgres auto-casts
# TEXT to JSONB on insert when the column is JSONB; sqlite stores
# the raw text as-is.  The cache layer always re-parses the value
# in Python via ``json.loads`` so the storage representation does
# not matter.
_INSERT_ROW_SQL = text(
    """
    INSERT INTO cost_cache (
        cache_namespace,
        account_id,
        cache_key,
        query_type,
        period_start,
        period_end,
        payload,
        expires_at,
        last_successful_at,
        stale_payload,
        stale_until,
        azure_post_count
    ) VALUES (
        :namespace,
        :account_id,
        :cache_key,
        :query_type,
        :period_start,
        :period_end,
        :payload,
        :expires_at,
        :last_successful_at,
        :stale_payload,
        :stale_until,
        :azure_post_count
    )
    """
)

_UPDATE_ROW_SQL = text(
    """
    UPDATE cost_cache
       SET payload = :payload,
           query_type = :query_type,
           period_start = :period_start,
           period_end = :period_end,
           expires_at = :expires_at,
           last_successful_at = :last_successful_at,
           stale_payload = :stale_payload,
           stale_until = :stale_until,
           azure_post_count = :azure_post_count
     WHERE cache_namespace = :namespace
       AND account_id = :account_id
       AND cache_key = :cache_key
    """
)


def _read_row(
    session,
    *,
    cache_namespace: str,
    storage_account_id: str,
    cache_key: str,
) -> Optional[Dict[str, Any]]:
    """Read a cache row as a dict (raw SQL; sidesteps the broken
    SQLAlchemy ORM mapper for this project).
    """
    result = session.execute(
        _SELECT_ROW_SQL,
        {
            "namespace": cache_namespace,
            "account_id": storage_account_id,
            "cache_key": cache_key,
        },
    ).first()
    if result is None:
        return None
    # SQLAlchemy Row supports both indexing and ._mapping; we
    # normalise to a plain dict so the rest of this module is
    # portable across DB drivers.  JSONB columns (Postgres) return
    # already-parsed Python values; TEXT columns (SQLite) return
    # JSON strings — we parse both forms so the cache layer
    # always sees dicts.
    return {
        "payload": _coerce_jsonb(result.payload),
        "expires_at": result.expires_at,
        "last_successful_at": result.last_successful_at,
        "stale_payload": _coerce_jsonb(result.stale_payload),
        "stale_until": result.stale_until,
        "azure_post_count": int(result.azure_post_count or 0),
        "created_at": result.created_at,
    }


def _coerce_jsonb(value: Any) -> Any:
    """Coerce a column value to the Python type we expect.

    Postgres JSONB / sqlite TEXT columns may return either a
    parsed Python object (Postgres + native JSONB type) or a JSON
    string (sqlite + TEXT column).  Both forms are accepted; an
    unparseable string is treated as a cache miss (returns None).
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _write_row(
    session,
    *,
    row: Optional[Dict[str, Any]],
    cache_namespace: str,
    storage_account_id: str,
    cache_key: str,
    payload: dict,
    query_type: str,
    period_start: datetime,
    period_end: datetime,
    expires_at: datetime,
    last_successful_at: datetime,
    stale_payload: Optional[dict],
    stale_until: Optional[datetime],
    azure_post_count: int,
) -> None:
    """Insert (if ``row`` is None) or update (if ``row`` exists)
    a cache row.  Uses raw SQL to sidestep the broken ORM mapper.
    """
    params = {
        "namespace": cache_namespace,
        "account_id": storage_account_id,
        "cache_key": cache_key,
        "query_type": query_type,
        "period_start": period_start,
        "period_end": period_end,
        "payload": json.dumps(payload),
        "expires_at": expires_at,
        "last_successful_at": last_successful_at,
        "stale_payload": json.dumps(stale_payload) if stale_payload is not None else None,
        "stale_until": stale_until,
        "azure_post_count": int(azure_post_count),
    }
    if row is None:
        session.execute(_INSERT_ROW_SQL, params)
    else:
        session.execute(_UPDATE_ROW_SQL, params)


# ---------------------------------------------------------------------------
# Read-through entry point
# ---------------------------------------------------------------------------


AzureLoader = Callable[[], Awaitable[dict]]


async def get_or_refresh_azure_canonical(
    *,
    scope: str,
    region: str,
    kind: str,
    signature: str,
    provider: str = "AZURE",
    loader: AzureLoader,
    db: Optional["object"] = None,
    fresh_ttl_seconds: Optional[int] = None,
    stale_ttl_seconds: Optional[int] = None,
) -> AzureCacheEntry:
    """Read-through cache lookup with FRESH + STALE-LKG semantics."""
    fresh = fresh_ttl_seconds if fresh_ttl_seconds is not None else _fresh_ttl_seconds()
    stale = stale_ttl_seconds if stale_ttl_seconds is not None else _stale_ttl_seconds()
    cache_key = build_azure_cache_key(
        provider=provider,
        scope=scope,
        region=region,
        kind=kind,
        signature=signature,
    )
    storage_account_id = build_azure_storage_account_id(scope)
    lock = await _lock_for(CACHE_NAMESPACE, storage_account_id, cache_key)

    async with lock:
        own_session = db is None
        # Lazy import so the module can be loaded even when the
        # ``app.db.models`` mapper is broken on Python 3.14 +
        # SQLAlchemy 2.0.36 (a pre-existing baseline issue).
        if db is None:
            from app.db.session import SessionLocal as _SessionLocal
            session = _SessionLocal()
        else:
            session = db
        try:
            try:
                row = _read_row(
                    session,
                    cache_namespace=CACHE_NAMESPACE,
                    storage_account_id=storage_account_id,
                    cache_key=cache_key,
                )
            except (OperationalError, SQLAlchemyError) as exc:
                # DB outage must not become a cost-API 500.  Degrade
                # to the live path and log a warning.
                logger.warning(
                    "azure.cost.cache.read_failed code=%s — falling back to live",
                    type(exc).__name__,
                )
                payload = await _safe_loader_call(loader)
                return AzureCacheEntry(
                    payload=payload,
                    cache_status=AzureCacheStatus.REFRESHED,
                    data_freshness=AzureDataFreshness.LIVE,
                    cache_key=cache_key,
                    storage_account_id=storage_account_id,
                )

            now = datetime.now(timezone.utc)

            # FRESH — ``expires_at > now`` → no upstream call.
            #
            # Phase 7D.3.3 (closure fix) — a legacy row (written by
            # a buggy predicate in the previous phase, or surviving
            # from before the authority check) may carry a
            # throttled-zero payload.  We MUST NOT serve that as an
            # authoritative LKG.  Re-validate the cached payload
            # against ``_is_authoritative_success`` here; treat a
            # non-authoritative row as a cache MISS so the next
            # upstream call can replace it with a real success.
            if (
                row is not None
                and row["payload"] is not None
                and row["expires_at"] is not None
                and _ts(row["expires_at"]) > now
                and _is_authoritative_success(row["payload"])
            ):
                logger.info(
                    "azure.cost.cache HIT key=%s namespace=%s",
                    cache_key, CACHE_NAMESPACE,
                )
                return AzureCacheEntry(
                    payload=row["payload"],
                    cache_status=AzureCacheStatus.HIT,
                    data_freshness=AzureDataFreshness.CACHED,
                    last_successful_at=_ts_or_none(row["last_successful_at"]) or _ts_or_none(row["created_at"]),
                    azure_post_count=int(row["azure_post_count"] or 0),
                    cache_key=cache_key,
                    storage_account_id=storage_account_id,
                )
            elif row is not None and row["payload"] is not None:
                # Legacy / invalid row present.  Fall through to
                # the loader so the next call can refresh it.
                logger.warning(
                    "azure.cost.cache legacy_invalid_payload key=%s — treating as MISS",
                    cache_key,
                )

            # MISS / STALE — call the loader
            try:
                fresh_payload = await _safe_loader_call(loader)
            except Exception as loader_exc:  # noqa: BLE001 — sanitised at route layer
                # If we have a still-valid stale row, fall back.
                if (
                    row is not None
                    and row["stale_payload"] is not None
                    and row["stale_until"] is not None
                    and _ts(row["stale_until"]) > now
                ):
                    logger.warning(
                        "azure.cost.cache STALE key=%s code=%s — serving LKG",
                        cache_key, type(loader_exc).__name__,
                    )
                    return AzureCacheEntry(
                        payload=row["stale_payload"],
                        cache_status=AzureCacheStatus.STALE,
                        data_freshness=AzureDataFreshness.STALE,
                        last_successful_at=_ts_or_none(row["last_successful_at"]),
                        azure_post_count=int(row["azure_post_count"] or 0),
                        cache_key=cache_key,
                        storage_account_id=storage_account_id,
                    )
                logger.warning(
                    "azure.cost.cache MISS key=%s code=%s",
                    cache_key, type(loader_exc).__name__,
                )
                raise

            # REFRESHED — persist the payload + LKG copy
            #
            # Phase 7D.3.3 (closure fix) — only authoritative
            # responses may advance the cache.  Non-authoritative
            # responses (throttled-zero, all-failed, ERROR) MUST
            # NOT overwrite the ``payload`` column because that
            # column is what ``cache_status=HIT`` returns to the
            # next caller.  If we wrote a throttled-zero here, the
            # next request would HIT-serve it as authoritative LKG.
            #
            # The fix: skip the DB write entirely for non-authoritative
            # responses and return the live payload without
            # advancing ``stale_payload`` / ``stale_until``.  The
            # next request will try again from the loader.
            authoritative = _is_authoritative_success(fresh_payload)
            if not authoritative:
                logger.warning(
                    "azure.cost.cache non_authoritative_skip key=%s status=%s successful=%s",
                    cache_key,
                    fresh_payload.get("status"),
                    fresh_payload.get("successful_accounts"),
                )
                return AzureCacheEntry(
                    payload=fresh_payload,
                    cache_status=AzureCacheStatus.REFRESHED,
                    data_freshness=AzureDataFreshness.LIVE,
                    last_successful_at=None,
                    azure_post_count=int(
                        fresh_payload.get("azure_post_count") or 0
                    ),
                    cache_key=cache_key,
                    storage_account_id=storage_account_id,
                )
            post_count = int(fresh_payload.get("azure_post_count") or 0)
            new_expires_at = now + timedelta(seconds=fresh)
            new_stale_until = now + timedelta(seconds=stale)
            try:
                _write_row(
                    session,
                    row=row,
                    cache_namespace=CACHE_NAMESPACE,
                    storage_account_id=storage_account_id,
                    cache_key=cache_key,
                    payload=fresh_payload,
                    query_type=str(fresh_payload.get("source") or "AZURE_COST_MANAGEMENT"),
                    period_start=_parse_iso(fresh_payload.get("period", {}).get("start")) or now,
                    period_end=_parse_iso(fresh_payload.get("period", {}).get("end")) or now,
                    expires_at=new_expires_at,
                    last_successful_at=now,
                    stale_payload=fresh_payload,
                    stale_until=new_stale_until,
                    azure_post_count=post_count,
                )
                session.commit()
            except (OperationalError, SQLAlchemyError) as exc:
                session.rollback()
                logger.warning(
                    "azure.cost.cache.write_failed code=%s — returning live payload",
                    type(exc).__name__,
                )
            logger.info(
                "azure.cost.cache REFRESHED key=%s authoritative=%s posts=%d",
                cache_key, authoritative, post_count,
            )
            return AzureCacheEntry(
                payload=fresh_payload,
                cache_status=AzureCacheStatus.REFRESHED,
                data_freshness=AzureDataFreshness.LIVE,
                last_successful_at=now,
                azure_post_count=post_count,
                cache_key=cache_key,
                storage_account_id=storage_account_id,
            )
        finally:
            if own_session:
                try:
                    session.close()
                except Exception:  # noqa: BLE001 — defensive
                    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _safe_loader_call(loader: AzureLoader) -> dict:
    """Invoke ``loader()`` and surface its exception type verbatim."""
    payload = await loader()
    if not isinstance(payload, dict):
        raise TypeError("azure loader returned a non-dict payload")
    return payload


def _ts(value) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if isinstance(value, str):
        # SQLite returns TIMESTAMP columns as strings; parse them
        # defensively.  Postgres (TIMESTAMPTZ) returns datetime.
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ts_or_none(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return _ts_or_none(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def reset_locks_for_tests() -> None:
    """Drop the in-process single-flight lock map."""
    _Locks.clear()


__all__ = [
    "AzureCacheEntry",
    "AzureCacheStatus",
    "AzureDataFreshness",
    "CACHE_NAMESPACE",
    "build_azure_cache_key",
    "build_azure_storage_account_id",
    "get_or_refresh_azure_canonical",
    "reset_locks_for_tests",
]
