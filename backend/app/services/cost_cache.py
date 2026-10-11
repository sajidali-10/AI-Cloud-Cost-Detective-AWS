"""Read-through cost cache — Phase 2.

A small wrapper around the ``cost_cache`` ORM that enforces the three
invariants the Phase 2 spec calls out:

1. Errors are NEVER persisted.  ``get_or_refresh`` propagates the
   loader's exception to the caller and writes nothing to the DB.
2. Concurrent identical requests are deduplicated in-process via an
   ``asyncio.Lock`` keyed by ``(account_id, cache_key)`` so a thundering
   herd of callers cannot multiply Cost Explorer API charges.
3. The cache never silently grows without bound: after every
   successful write, with probability ``COST_CACHE_GC_PROBABILITY``
   (default ``0.01``), a bounded ``DELETE WHERE expires_at < now()
   LIMIT N`` runs to evict the oldest expired rows.

TTL is controlled by ``COST_CACHE_TTL_SECONDS`` (default ``21600`` ==
6 hours).  All knobs are env-driven so Phase 4 verification can pin
them deterministically.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CostCache
from app.db.session import SessionLocal
from app.schemas.cost import CacheStatus, CostReport, CostReportResponse

logger = logging.getLogger("cost-detective.cost_cache")


# ---------------------------------------------------------------------------
# Config knobs (env-driven; safe defaults match the spec)
# ---------------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 21600  # 6 hours, per Phase 2 spec
DEFAULT_GC_PROBABILITY = 0.01  # ~1% of writes trigger GC
DEFAULT_GC_BATCH = 1000  # rows per GC sweep


def _ttl_seconds() -> int:
    return int(os.getenv("COST_CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))


def _gc_probability() -> float:
    return float(os.getenv("COST_CACHE_GC_PROBABILITY", str(DEFAULT_GC_PROBABILITY)))


def _gc_batch() -> int:
    return int(os.getenv("COST_CACHE_GC_BATCH", str(DEFAULT_GC_BATCH)))


# ---------------------------------------------------------------------------
# In-process dedup
# ---------------------------------------------------------------------------

# One ``asyncio.Lock`` per (account_id, cache_key) tuple.  We keep them
# in a process-wide dict and create new locks on first use.  The dict
# itself is guarded by a regular lock for the brief window in which a
# new key is being inserted.
_locks: dict[Tuple[str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for(account_id: str, cache_key: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get((account_id, cache_key))
        if lock is None:
            lock = asyncio.Lock()
            _locks[(account_id, cache_key)] = lock
        return lock


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_payload(report: CostReport) -> dict[str, Any]:
    """Convert a Pydantic ``CostReport`` to a JSON-safe dict.

    Decimal values are encoded as strings so they round-trip through
    JSONB without binary-float corruption.
    """

    def _encode(obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return str(obj)
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        raise TypeError(f"Cannot encode {type(obj).__name__}")

    return json.loads(report.model_dump_json())


def _deserialize_payload(row_payload: dict[str, Any], *, model_cls: type = CostReport) -> Any:
    """Inverse of ``_serialize_payload`` — re-hydrate the cached report.

    ``model_cls`` defaults to ``CostReport`` for backwards-compatibility
    with Phase 7A; Phase 7B's multi-account path passes
    :class:`MultiAccountCostReport` so the cache row is interpreted
    with the correct schema.
    """
    return model_cls.model_validate(row_payload)


def _build_response(
    *,
    model_cls: type,
    report: Any,
    cache_status: CacheStatus,
    cached_at: datetime,
    expires_at: datetime,
) -> Any:
    """Build the correct response envelope for ``model_cls``.

    Phase 7B routes pass ``MultiAccountCostReport`` here so the
    response is wrapped in :class:`MultiAccountCostReportResponse`.
    """
    if model_cls is CostReport:
        return CostReportResponse(
            report=report,
            cache_status=cache_status,
            cached_at=cached_at,
            expires_at=expires_at,
        )
    # Lazy import to keep the cache module free of Phase 7B schemas
    # until they're actually used.
    from app.schemas.cost import MultiAccountCostReportResponse
    return MultiAccountCostReportResponse(
        report=report,
        cache_status=cache_status,
        cached_at=cached_at,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# Read-through entry point
# ---------------------------------------------------------------------------


LoaderType = Callable[[], Awaitable[Any]]


async def get_or_refresh(
    *,
    account_id: str,
    cache_key: str,
    loader: LoaderType,
    ttl_seconds: Optional[int] = None,
    db: Optional[Session] = None,
    scope: str = "acct",
    model_cls: type = CostReport,
) -> Any:
    """Return a cached or freshly-loaded report envelope.

    Phase 7B: ``scope`` is the account-selection token (``"acct"``
    for a specific 12-digit AWS account ID, ``"all"`` for the
    multi-account aggregate).  It is included in the database row
    so an "All AWS Accounts" response can never satisfy a request
    scoped to a specific account, and vice-versa.

    ``model_cls`` is the Pydantic model used to deserialize cached
    payloads (default ``CostReport`` for backwards-compatibility; the
    multi-account path passes :class:`MultiAccountCostReport`).

    The contract:

    * If a non-expired row exists, return it with ``cache_status=HIT``.
    * Otherwise call ``loader()`` and persist the result with
      ``cache_status=REFRESHED``.  If the loader raises, the cache is
      left untouched and the exception propagates.
    * Two concurrent callers with the same key share a single loader
      invocation (in-process dedup).
    * After every successful write, ``maybe_collect_garbage`` runs.

    The function returns either a :class:`CostReportResponse` or a
    :class:`MultiAccountCostReportResponse` depending on ``model_cls``.
    """
    ttl = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
    storage_account_id = f"{scope}:{account_id}"
    lock = await _lock_for(storage_account_id, cache_key)
    async with lock:
        own_session = db is None
        session = db or SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            existing = session.execute(
                select(CostCache).where(
                    CostCache.account_id == storage_account_id,
                    CostCache.cache_key == cache_key,
                )
            ).scalar_one_or_none()
            if existing is not None and not existing.is_expired(now):
                logger.info(
                    "cost cache HIT account=%s key=%s",
                    storage_account_id,
                    cache_key,
                )
                return _build_response(
                    model_cls=model_cls,
                    report=_deserialize_payload(existing.payload, model_cls=model_cls),
                    cache_status=CacheStatus.HIT,
                    cached_at=existing.created_at,
                    expires_at=existing.expires_at,
                )

            # MISS or expired — fetch fresh data.
            report = await loader()
            now_after_load = datetime.now(timezone.utc)
            expires_at = now_after_load + timedelta(seconds=ttl)
            payload = _serialize_payload(report)

            if existing is None:
                row = CostCache(
                    account_id=storage_account_id,
                    cache_key=cache_key,
                    query_type=report.source,
                    period_start=datetime.combine(
                        report.period.start, dt_time(0, 0), tzinfo=timezone.utc
                    ),
                    period_end=datetime.combine(
                        report.period.end, dt_time(0, 0), tzinfo=timezone.utc
                    ),
                    payload=payload,
                    expires_at=expires_at,
                )
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    # Another concurrent caller inserted the same key
                    # between our SELECT and INSERT (rare but possible
                    # if multiple processes share a DB).  Roll back and
                    # re-read the winner's row.
                    session.rollback()
                    existing = session.execute(
                        select(CostCache).where(
                            CostCache.account_id == storage_account_id,
                            CostCache.cache_key == cache_key,
                        )
                    ).scalar_one()
                    return _build_response(
                        model_cls=model_cls,
                        report=_deserialize_payload(existing.payload, model_cls=model_cls),
                        cache_status=CacheStatus.HIT,
                        cached_at=existing.created_at,
                        expires_at=existing.expires_at,
                    )
            else:
                existing.payload = payload
                existing.query_type = report.source
                existing.period_start = datetime.combine(
                    report.period.start, dt_time(0, 0), tzinfo=timezone.utc
                )
                existing.period_end = datetime.combine(
                    report.period.end, dt_time(0, 0), tzinfo=timezone.utc
                )
                existing.expires_at = expires_at
                session.commit()

            logger.info(
                "cost cache REFRESHED account=%s key=%s",
                storage_account_id,
                cache_key,
            )
            maybe_collect_garbage(session)
            return _build_response(
                model_cls=model_cls,
                report=report,
                cache_status=CacheStatus.REFRESHED,
                cached_at=now_after_load,
                expires_at=expires_at,
            )
        finally:
            if own_session:
                session.close()


# ---------------------------------------------------------------------------
# Lazy + probabilistic GC
# ---------------------------------------------------------------------------


def maybe_collect_garbage(
    db: Session,
    *,
    probability: Optional[float] = None,
    batch: Optional[int] = None,
) -> int:
    """Run a bounded DELETE of expired rows with low probability.

    Returns the number of rows deleted (0 when the dice roll fails or
    nothing was eligible).  Callers should NOT rely on this function
    for correctness — the cache layer reads with ``expires_at > now()``
    so even without GC, expired rows are simply ignored.
    """
    p = probability if probability is not None else _gc_probability()
    n = batch if batch is not None else _gc_batch()
    if p <= 0.0 or n <= 0:
        return 0
    if random.random() >= p:
        return 0
    now = datetime.now(timezone.utc)
    # SQLAlchemy's ``delete()`` does not support ``.limit()`` portably
    # (Postgres requires ``DELETE ... LIMIT`` to be expressed as a CTE
    # with row_number, SQLite has it natively).  We use a small
    # dialect-aware raw SQL that works against both.
    from sqlalchemy import text
    stmt = text(
        "DELETE FROM cost_cache "
        "WHERE id IN ("
        "  SELECT id FROM cost_cache "
        "  WHERE expires_at < :now "
        "  LIMIT :batch"
        ")"
    )
    result = db.execute(stmt, {"now": now, "batch": n})
    db.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        logger.info("cost cache GC deleted=%d rows", deleted)
    return deleted


__all__ = [
    "DEFAULT_GC_BATCH",
    "DEFAULT_GC_PROBABILITY",
    "DEFAULT_TTL_SECONDS",
    "get_or_refresh",
    "maybe_collect_garbage",
]
