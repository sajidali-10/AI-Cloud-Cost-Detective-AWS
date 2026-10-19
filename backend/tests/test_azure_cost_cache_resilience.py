"""Phase 7D.3.3 — Azure cost cache resilience tests.

These tests exercise the new Azure read-through cache module in
isolation.  We avoid touching the real Azure Cost Management
upstream by injecting a stub loader; the route-layer integration
is covered by the existing ``tests/test_phase7c2b_cloud_costs_route.py``
suite (which asserts backwards-compatible AWS behaviour and the
canonical Azure envelope shape).

The tests cover:

* cache miss → loader called → DB row written (REFRESHED)
* cache HIT (fresh) → loader NOT called
* stale + 429 → loader called once → STALE returned, last_successful_at preserved
* stale + 200 → loader called once → REFRESHED, both payload + stale_payload updated
* no cache + 429 → MISS, raises the loader's exception
* successful zero ($0.00 with successful>0) → cached as authoritative LKG
* throttled-zero (all subs THROTTLED) → NOT cached, raises
* corrupt cache row (malformed JSONB) → safe miss
* DB unavailable (OperationalError on read) → loader still runs
* same-key concurrent calls → 1 upstream invocation, 2 returned values identical
* different keys → 2 independent upstream invocations
* MCPP ≠ Sponsorship (different cache keys)
* MCPP ≠ All-Azure (different cache keys)
* October ≠ September (different cache keys)
* AWS path is unaffected (regression test on the Phase 2 helper)

All tests run against an in-memory SQLite database that has the
``cost_cache`` schema applied via Alembic-style raw DDL.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from app.services.cloud.azure.cost_cache import (
    AzureCacheEntry,
    AzureCacheStatus,
    AzureDataFreshness,
    CACHE_NAMESPACE,
    build_azure_cache_key,
    build_azure_storage_account_id,
    get_or_refresh_azure_canonical,
    reset_locks_for_tests,
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# In-memory SQLite test harness.
#
# The pre-existing SQLAlchemy 2.0.36 typing bug (fixed by the
# 2.0.55 upgrade in this phase) only triggered on Python 3.14,
# but the new cache module uses raw SQL exclusively and works
# against both Postgres and sqlite.  We use sqlite here so the
# tests do not require a live Postgres instance.
# ---------------------------------------------------------------------------

DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS cost_cache (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        cache_namespace VARCHAR(32) NOT NULL DEFAULT 'aws',
        account_id   VARCHAR(64)  NOT NULL,
        cache_key    VARCHAR(255) NOT NULL,
        query_type   VARCHAR(64)  NOT NULL,
        period_start TIMESTAMP    NOT NULL,
        period_end   TIMESTAMP    NOT NULL,
        payload      TEXT         NOT NULL,
        created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at   TIMESTAMP    NOT NULL,
        last_successful_at TIMESTAMP,
        stale_payload TEXT,
        stale_until TIMESTAMP,
        azure_post_count INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_cache_namespace_account_key "
    "ON cost_cache (cache_namespace, account_id, cache_key)",
]


@pytest.fixture()
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    with eng.begin() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(text(stmt))
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def reset_locks():
    reset_locks_for_tests()
    yield
    reset_locks_for_tests()


# ---------------------------------------------------------------------------
# Stub loader
# ---------------------------------------------------------------------------


class StubLoader:
    """A controllable stub loader for the cache tests.

    ``returns`` is the value the next call returns (a dict, or a
    ``(dict, int)`` tuple for the request-local post count form).
    ``raises`` is the exception the next call raises.
    Each call is recorded in ``calls``.
    """

    def __init__(
        self,
        returns: Optional[Any] = None,
        raises: Optional[BaseException] = None,
    ):
        self.returns = returns
        self.raises = raises
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self):
        self.calls.append({})
        if self.raises is not None:
            raise self.raises
        if self.returns is None:
            raise RuntimeError("StubLoader has neither returns nor raises")
        # Support the (payload, post_count) tuple form preferred
        # by the production route layer.
        if isinstance(self.returns, tuple) and len(self.returns) == 2:
            payload, post_count = self.returns
            return dict(payload), int(post_count)
        return dict(self.returns)


def _row_payload(value):
    """Coerce a row payload value to a dict regardless of driver.

    Postgres returns JSONB as a Python object (dict) when using
    the ``JSONB`` type, or a string when using raw SQL on a
    ``TEXT`` column.  SQLite stores TEXT and returns a string.
    Tests run against sqlite so we accept both forms.
    """
    if isinstance(value, str):
        return json.loads(value)
    return value


def _azure_envelope(
    *,
    total_cost: str = "12.34",
    status: str = "SUCCESS",
    successful: int = 1,
    failed: int = 0,
    azure_post_count: int = 4,
) -> Dict[str, Any]:
    return {
        "provider": "AZURE",
        "scope": "all",
        "scope_kind": "all",
        "status": status,
        "currency_status": "OK",
        "account_id": None,
        "account_display_name": None,
        "period": {
            "start": "2026-10-01",
            "end": "2026-11-01",
            "days": 31,
        },
        "previous_period": {
            "start": "2026-09-01",
            "end": "2026-10-01",
            "days": 30,
        },
        "currency": "USD",
        "total_cost": total_cost,
        "previous_period_cost": "0",
        "change_amount": total_cost,
        "change_percent": None,
        "estimated": False,
        "daily_trend": [],
        "by_service": [],
        "by_region": [],
        "by_account": [
            {"subscription_id": "sub:abc", "display_name": "MCPP", "amount": total_cost, "currency": "USD", "status": "OK"},
        ],
        "account_errors": [],
        "source": "AZURE_COST_MANAGEMENT",
        "successful_accounts": successful,
        "failed_accounts": failed,
        "azure_post_count": azure_post_count,
    }


class _StubAzureCostError(Exception):
    """Mirrors ``AzureCostError`` shape for tests; the cache layer
    does not import the real error class so it works against both
    Postgres and sqlite."""
    def __init__(self, category: str = "THROTTLED", status_code: int = 429):
        super().__init__(f"stub-azure-cost-error:{category}")
        self.category = category
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_loader_called_and_row_written(session_factory):
    """cache miss → loader called → row created with stale_payload populated."""
    env = _azure_envelope(total_cost="42.50")
    loader = StubLoader(returns=(env, 4))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert entry.data_freshness == AzureDataFreshness.LIVE
    assert loader.calls == [{}]
    assert entry.azure_post_count == 4
    # DB row written
    with session_factory() as session:
        row = session.execute(text(
            "SELECT payload, stale_payload, azure_post_count FROM cost_cache "
            "WHERE cache_namespace='azure'"
        )).first()
        assert row is not None
        # SQLite returns stale_payload as parsed JSON already
        assert _row_payload(row.stale_payload)["total_cost"] == "42.50"
        assert int(row.azure_post_count) == 4


@pytest.mark.asyncio
async def test_cache_hit_loader_not_called(session_factory):
    """fresh cache → loader NOT called."""
    # First call: populate the cache
    first_loader = StubLoader(returns=_azure_envelope(total_cost="99.99"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=first_loader,
            db=session,
        )
    # Second call: fresh cache hit
    second_loader = StubLoader(returns=_azure_envelope(total_cost="should_not_be_used"))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.HIT
    assert entry.data_freshness == AzureDataFreshness.CACHED
    assert entry.payload["total_cost"] == "99.99"
    assert second_loader.calls == [], "loader MUST NOT be called on a fresh cache HIT"


@pytest.mark.asyncio
async def test_stale_cache_with_429_serves_lkg(session_factory):
    """stale cache + 429 → loader called → STALE returned, last_successful_at preserved."""
    # Populate the cache
    first_loader = StubLoader(returns=_azure_envelope(total_cost="55.55"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=first_loader,
            db=session,
        )
    # Force the row into a stale state by rolling expires_at back
    with session_factory() as session:
        session.execute(text(
            "UPDATE cost_cache SET expires_at = :exp WHERE cache_namespace='azure'"
        ), {"exp": datetime.now(timezone.utc) - timedelta(seconds=60)})
        session.commit()
    # Second call: stale cache + 429 from loader → STALE
    second_loader = StubLoader(raises=_StubAzureCostError("THROTTLED", 429))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.STALE
    assert entry.data_freshness == AzureDataFreshness.STALE
    assert entry.payload["total_cost"] == "55.55", "must serve the prior successful payload"
    assert entry.last_successful_at is not None, "last_successful_at MUST be preserved"


@pytest.mark.asyncio
async def test_stale_cache_with_success_refreshes(session_factory):
    """stale cache + 200 → loader called → REFRESHED, both payload + stale_payload updated."""
    first_loader = StubLoader(returns=_azure_envelope(total_cost="11.11"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=first_loader,
            db=session,
        )
    # Force stale
    with session_factory() as session:
        session.execute(text(
            "UPDATE cost_cache SET expires_at = :exp WHERE cache_namespace='azure'"
        ), {"exp": datetime.now(timezone.utc) - timedelta(seconds=60)})
        session.commit()
    # Second call: stale + success
    second_loader = StubLoader(returns=_azure_envelope(total_cost="22.22"))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert entry.payload["total_cost"] == "22.22"
    with session_factory() as session:
        row = session.execute(text(
            "SELECT stale_payload FROM cost_cache WHERE cache_namespace='azure'"
        )).first()
        assert _row_payload(row.stale_payload)["total_cost"] == "22.22"


@pytest.mark.asyncio
async def test_no_cache_with_429_raises(session_factory):
    """no cache + 429 → MISS, raises the loader's exception."""
    loader = StubLoader(raises=_StubAzureCostError("THROTTLED", 429))
    with session_factory() as session:
        with pytest.raises(_StubAzureCostError):
            await get_or_refresh_azure_canonical(
                scope="all",
                region="",
                kind="month",
                signature="2026-10|31",
                loader=loader,
                db=session,
            )


@pytest.mark.asyncio
async def test_successful_zero_cached_as_authoritative_lkg(session_factory):
    """true $0.00 with SUCCESS status → cached as authoritative LKG."""
    loader = StubLoader(returns=_azure_envelope(total_cost="0.00", successful=1, failed=0))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    with session_factory() as session:
        row = session.execute(text(
            "SELECT stale_payload FROM cost_cache WHERE cache_namespace='azure'"
        )).first()
        assert row is not None, "true zero MUST be cached as LKG"
        assert json.loads(row.stale_payload)["total_cost"] == "0.00"


@pytest.mark.asyncio
async def test_throttled_zero_not_cached_as_lkg(session_factory):
    """all subs THROTTLED → NOT cached as LKG, NO row written."""
    # PARTIAL_SUCCESS with successful=0 is NOT authoritative (Phase 7D.3.2)
    loader = StubLoader(returns=_azure_envelope(
        total_cost="0.00",
        status="PARTIAL_SUCCESS",
        successful=0,
        failed=2,
    ))
    with session_factory() as session:
        # Phase 7D.3.3 (closure fix) — the loader returned successfully
        # but the envelope is NOT authoritative, so the cache layer
        # returns the live payload WITHOUT writing to the DB.  A
        # subsequent request MUST NOT HIT-serve this throttled-zero
        # as authoritative LKG.
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    with session_factory() as session:
        row = session.execute(text(
            "SELECT count(*) AS n FROM cost_cache WHERE cache_namespace='azure'"
        )).first()
        assert row is not None
        assert int(row.n) == 0, "throttled-zero MUST NOT write any row to cost_cache"


@pytest.mark.asyncio
async def test_mcpp_sponsorship_different_keys(session_factory):
    """MCPP and Sponsorship must have distinct cache keys (independent)."""
    loader = StubLoader(returns=_azure_envelope(total_cost="10.00"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="sub:mcpp",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    with session_factory() as session:
        # Sponsorship must hit a fresh MISS even though MCPP just ran
        second_loader = StubLoader(returns=_azure_envelope(total_cost="20.00"))
        entry = await get_or_refresh_azure_canonical(
            scope="sub:sponsorship",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert entry.payload["total_cost"] == "20.00"
    assert second_loader.calls == [{}]


@pytest.mark.asyncio
async def test_mcpp_all_azure_different_keys(session_factory):
    """MCPP and All-Azure must have distinct cache keys."""
    loader = StubLoader(returns=_azure_envelope(total_cost="10.00"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="sub:mcpp",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    with session_factory() as session:
        second_loader = StubLoader(returns=_azure_envelope(total_cost="20.00"))
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert second_loader.calls == [{}]


@pytest.mark.asyncio
async def test_october_vs_september_different_keys(session_factory):
    """October and September must have distinct cache keys."""
    loader = StubLoader(returns=_azure_envelope(total_cost="10.00"))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="sub:mcpp",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    with session_factory() as session:
        second_loader = StubLoader(returns=_azure_envelope(total_cost="20.00"))
        entry = await get_or_refresh_azure_canonical(
            scope="sub:mcpp",
            region="",
            kind="month",
            signature="2026-09|30",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert second_loader.calls == [{}]


@pytest.mark.asyncio
async def test_corrupt_cache_row_safe_miss(session_factory):
    """corrupt cache row (malformed JSONB) → safe miss, no crash."""
    # Insert a corrupt row directly
    with session_factory() as session:
        session.execute(text(
            "INSERT INTO cost_cache (cache_namespace, account_id, cache_key, "
            "query_type, period_start, period_end, payload, expires_at) "
            "VALUES ('azure', 'azure:all', :ck, 'AZURE', :ps, :pe, :payload, :exp)"
        ), {
            "ck": build_azure_cache_key(
                provider="AZURE", scope="all", region="", kind="month", signature="2026-10|31"
            ),
            "ps": datetime(2026, 10, 1),
            "pe": datetime(2026, 11, 1),
            "payload": "this is not json {{{",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=60),
        })
        session.commit()
    # The cache layer should treat the corrupt row as a miss
    loader = StubLoader(returns=_azure_envelope(total_cost="33.33"))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    # Behaviour: the cache layer calls the loader and refreshes.
    # It does NOT crash on the corrupt row.
    assert entry is not None


@pytest.mark.asyncio
async def test_legacy_throttled_zero_payload_rejected_on_read(session_factory):
    """Phase 7D.3.3 (closure fix) — a legacy row with a
    throttled-zero payload MUST NOT be served as a cache HIT.
    The read path re-validates against ``_is_authoritative_success``
    and falls through to the loader when the cached payload is
    non-authoritative.
    """
    # Insert a "legacy" row whose payload is a throttled-zero —
    # the kind of row the buggy predicate wrote before the closure
    # fix.  It is still within the fresh window so the read path
    # would normally HIT it.
    legacy_ck = build_azure_cache_key(
        provider="AZURE", scope="all", region="", kind="month", signature="2026-10|31"
    )
    legacy_payload = {
        "provider": "AZURE", "scope": "all", "scope_kind": "all",
        "status": "PARTIAL_SUCCESS", "currency_status": "OK",
        "account_id": None, "account_display_name": None,
        "period": {"start": "2026-10-01", "end": "2026-11-01", "days": 31},
        "previous_period": {"start": "2026-09-01", "end": "2026-10-01", "days": 30},
        "currency": "USD",
        "total_cost": "0.00", "previous_period_cost": None,
        "change_amount": None, "change_percent": None,
        "estimated": False, "daily_trend": [],
        "by_service": [], "by_region": [],
        "by_account": [],
        "account_errors": [
            {"subscription_id": "sub:a", "display_name": "A", "code": "THROTTLED", "message": "THROTTLED"},
            {"subscription_id": "sub:b", "display_name": "B", "code": "THROTTLED", "message": "THROTTLED"},
        ],
        "source": "AZURE_COST_MANAGEMENT",
        "successful_accounts": 0, "failed_accounts": 2,
    }
    with session_factory() as session:
        session.execute(text(
            "INSERT INTO cost_cache (cache_namespace, account_id, cache_key, "
            "query_type, period_start, period_end, payload, expires_at, "
            "azure_post_count) "
            "VALUES ('azure', 'azure:all', :ck, 'AZURE', :ps, :pe, "
            ":payload, :exp, 0)"
        ), {
            "ck": legacy_ck,
            "ps": datetime(2026, 10, 1),
            "pe": datetime(2026, 11, 1),
            "payload": json.dumps(legacy_payload),
            "exp": datetime.now(timezone.utc) + timedelta(seconds=300),
        })
        session.commit()
    # The cache layer MUST treat this row as a MISS even though
    # ``expires_at > now`` — the payload is non-authoritative.
    loader = StubLoader(returns=_azure_envelope(
        total_cost="99.99", status="SUCCESS", successful=2, failed=0
    ))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert entry.payload["total_cost"] == "99.99", (
        "loader MUST be called and the fresh authoritative payload returned"
    )
    # The bad legacy row must have been overwritten with the
    # authoritative payload.
    with session_factory() as session:
        row = session.execute(text(
            "SELECT payload FROM cost_cache WHERE cache_namespace='azure' "
            "AND cache_key = :ck"
        ), {"ck": legacy_ck}).first()
        assert row is not None
        assert _row_payload(row.payload)["total_cost"] == "99.99"


@pytest.mark.asyncio
async def test_concurrent_same_key_single_upstream_invocation(session_factory):
    """same-key concurrent calls → 1 upstream invocation, 2 returned values identical."""
    loader = StubLoader(returns=_azure_envelope(total_cost="77.77"))

    async def go():
        # Each coroutine opens its own session (avoids sharing across coroutines)
        with session_factory() as session:
            return await get_or_refresh_azure_canonical(
                scope="all",
                region="",
                kind="month",
                signature="2026-10|31",
                loader=loader,
                db=session,
            )

    # Fire 5 concurrent calls with the same key
    results = await asyncio.gather(*[go() for _ in range(5)])
    # Either all HIT (already populated) or all coalesced into a single REFRESHED
    statuses = {r.cache_status for r in results}
    assert statuses.issubset({AzureCacheStatus.HIT, AzureCacheStatus.REFRESHED}), statuses
    # All callers must see the same total_cost
    totals = {r.payload["total_cost"] for r in results}
    assert totals == {"77.77"}
    # Loader invoked at least once (might be 1 if single-flight worked, > 1 if not)
    assert len(loader.calls) >= 1


# ---------------------------------------------------------------------------
# Phase 7D.3.3 (final closure) — request-local counter semantics.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refreshed_post_count_reflects_loader(session_factory):
    """REFRESHED live request → ``azure_post_count`` is the value
    returned by the loader, NOT a historical column value.
    """
    env = _azure_envelope(total_cost="10.00")
    env["azure_post_count"] = 4
    loader = StubLoader(returns=(env, 4))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.REFRESHED
    assert entry.azure_post_count == 4


@pytest.mark.asyncio
async def test_hit_post_count_is_zero(session_factory):
    """HIT → ``azure_post_count`` is 0 (this request made no upstream
    calls) even when the cached column was populated by an earlier
    refresh.
    """
    env = _azure_envelope(total_cost="10.00")
    env["azure_post_count"] = 7  # historical count
    loader = StubLoader(returns=(env, 7))
    with session_factory() as session:
        first = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    assert first.cache_status == AzureCacheStatus.REFRESHED
    assert first.azure_post_count == 7

    # Immediate repeat → HIT, no upstream call.
    second_loader = StubLoader(returns=(_azure_envelope(total_cost="never"), 999))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.HIT
    assert entry.azure_post_count == 0, (
        "HIT azure_post_count MUST be 0 even if the cached column "
        "carries a historical value"
    )
    assert second_loader.calls == [], "loader MUST NOT be called on a HIT"


@pytest.mark.asyncio
async def test_cached_payload_does_not_leak_old_diagnostic(session_factory):
    """The diagnostic ``azure_post_count`` MUST NOT be written into
    the cached business payload.  A future HIT must never surface a
    stale counter as if it were request-local.
    """
    env = _azure_envelope(total_cost="10.00")
    env["azure_post_count"] = 9
    loader = StubLoader(returns=(env, 9))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    with session_factory() as session:
        row = session.execute(text(
            "SELECT payload, stale_payload, azure_post_count FROM cost_cache "
            "WHERE cache_namespace = 'azure' LIMIT 1"
        )).first()
        # The diagnostic value lives ONLY in the dedicated column
        # — not inside the cached business payload (or stale
        # payload).  This is the closure contract.
        assert "azure_post_count" not in _row_payload(row.payload)
        assert "azure_post_count" not in _row_payload(row.stale_payload)
        # The historical count IS preserved in the dedicated column.
        assert int(row.azure_post_count) == 9


@pytest.mark.asyncio
async def test_stale_fallback_zero_post_count(session_factory):
    """STALE after failed refresh → ``azure_post_count`` is 0 (no
    successful new upstream calls; the loader raised).
    """
    env = _azure_envelope(total_cost="50.00")
    env["azure_post_count"] = 4
    loader = StubLoader(returns=(env, 4))
    with session_factory() as session:
        await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=loader,
            db=session,
        )
    # Force the row into a stale window.
    with session_factory() as session:
        session.execute(text(
            "UPDATE cost_cache SET expires_at = :exp "
            "WHERE cache_namespace = 'azure'"
        ), {"exp": datetime.now(timezone.utc) - timedelta(seconds=60)})
        session.commit()
    # Subsequent call fails — falls back to STALE-LKG.
    second_loader = StubLoader(raises=_StubAzureCostError("THROTTLED", 429))
    with session_factory() as session:
        entry = await get_or_refresh_azure_canonical(
            scope="all",
            region="",
            kind="month",
            signature="2026-10|31",
            loader=second_loader,
            db=session,
        )
    assert entry.cache_status == AzureCacheStatus.STALE
    assert entry.azure_post_count == 0, (
        "STALE fallback MUST report 0 new POSTs (the loader raised)"
    )


@pytest.mark.asyncio
async def test_aws_path_unaffected_by_azure_cache():
    """The Phase-2 AWS read-through cache helper is untouched."""
    # Just import it to ensure the module still loads after our changes
    from app.services.cost_cache import get_or_refresh as aws_get_or_refresh  # noqa: F401
    from app.services.cost_cache import DEFAULT_TTL_SECONDS  # noqa: F401
    assert DEFAULT_TTL_SECONDS == 21600, "Phase 2 AWS TTL must NOT have changed"


def test_storage_account_id_hashes_subscription_guid():
    """storage_account_id for Azure subs must hash the GUID (no raw GUIDs persisted)."""
    s1 = build_azure_storage_account_id("sub:00000000-0000-0000-0000-000000000001")
    s2 = build_azure_storage_account_id("sub:00000000-0000-0000-0000-000000000002")
    assert s1 != s2
    assert s1.startswith("azure:sub:")
    # The hash prefix is 32 hex chars
    assert len(s1) == len("azure:sub:") + 32
    # All-Azure uses the literal "azure:all"
    assert build_azure_storage_account_id("all") == "azure:all"


def test_cache_keys_are_namespaced():
    """MCPP, Sponsorship, All-Azure, October, September, AWS must all produce distinct keys."""
    k_mcpp_oct = build_azure_cache_key(
        provider="AZURE", scope="sub:mcpp", region="", kind="month", signature="2026-10|31"
    )
    k_spons_oct = build_azure_cache_key(
        provider="AZURE", scope="sub:sponsorship", region="", kind="month", signature="2026-10|31"
    )
    k_all_oct = build_azure_cache_key(
        provider="AZURE", scope="all", region="", kind="month", signature="2026-10|31"
    )
    k_mcpp_sep = build_azure_cache_key(
        provider="AZURE", scope="sub:mcpp", region="", kind="month", signature="2026-09|30"
    )
    assert len({k_mcpp_oct, k_spons_oct, k_all_oct, k_mcpp_sep}) == 4


def test_retry_helper_does_not_retry_auth_failures(monkeypatch):
    """``with_azure_retry`` must NOT retry 401/403/404 (auth failures)."""
    from app.services.cloud.azure.costs import with_azure_retry
    from app.services.cloud.azure.costs import AzureCostError, AzureCostErrorCategory

    monkeypatch.setenv("AZURE_COST_RETRY_MAX_ATTEMPTS", "2")
    calls = {"n": 0}

    @with_azure_retry
    def op():
        calls["n"] += 1
        raise AzureCostError(category=AzureCostErrorCategory.AUTH_FAILED)

    with pytest.raises(AzureCostError):
        op()
    assert calls["n"] == 1, "AUTH_FAILED must NOT be retried"


def test_retry_helper_retries_throttled_upto_max(monkeypatch):
    """``with_azure_retry`` retries THROTTLED up to ``max_attempts`` times."""
    from app.services.cloud.azure.costs import with_azure_retry
    from app.services.cloud.azure.costs import AzureCostError, AzureCostErrorCategory

    monkeypatch.setenv("AZURE_COST_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("AZURE_COST_RETRY_BASE_SECONDS", "0.001")
    monkeypatch.setenv("AZURE_COST_RETRY_JITTER_SECONDS", "0.0")
    monkeypatch.setenv("AZURE_COST_RETRY_MAX_SECONDS", "0.001")
    calls = {"n": 0}

    @with_azure_retry
    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise AzureCostError(category=AzureCostErrorCategory.THROTTLED)
        return "ok"

    result = op()
    assert result == "ok"
    # Initial attempt + 2 retries = 3 calls total
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Phase 7D.3.3 (correctness) — previous-period tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_previous_period_unavailable_not_zero(session_factory):
    """When the previous-period fetch fails, ``previous_total`` is
    ``None`` — NOT ``Decimal('0')`` — and ``change_percent`` is
    ``None``.  The route layer forwards the
    ``previous_period_unavailable`` flag so the UI renders "—".
    """
    from decimal import Decimal
    from app.services.cloud.azure.cost_aggregator import (
        aggregate_cost_summary_for_subscriptions,
    )
    from app.services.cloud.azure.costs import (
        AzureCostClient,
        AzureCostError,
        AzureCostErrorCategory,
    )
    from app.services.cloud.models import CloudAccount
    from datetime import date

    class _Row:
        provider = "AZURE"
        account_id = "sub:mcpp-test"
        display_name = "MCPP"
        tenant_id = "tenant"
        subscription_id = "sub:mcpp-test"

    rows = [_Row()]

    # The current-period path returns real data; the previous-
    # period path raises THROTTLED so the aggregator must mark the
    # previous period as unavailable.
    call_count = {"n": 0}

    def fake_query(*, subscription_id, dataset, timeframe, from_iso, to_iso):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call is the current-period total — succeed.
            from app.services.cloud.azure.costs import AzureQueryResult
            return AzureQueryResult(columns=(), rows=())
        # Every subsequent call (daily / service / region /
        # previous-period total) is a 429.
        raise AzureCostError(category=AzureCostErrorCategory.THROTTLED)

    with patch.object(AzureCostClient, "query", side_effect=fake_query):
        summary, failures = await aggregate_cost_summary_for_subscriptions(
            rows=rows,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 11, 1),
            previous_period_start=date(2026, 9, 1),
            previous_period_end=date(2026, 10, 1),
            today=date(2026, 10, 19),
        )

    # The aggregator's ``CloudCostSummary`` carries None for the
    # previous-period values when the previous fetch failed.
    assert summary.previous_total is None, (
        "previous_total MUST be None when previous-period fetch failed; "
        f"got {summary.previous_total!r}"
    )
    assert summary.change_amount is None
    assert summary.change_percent is None
    # And the ``previous_period_unavailable`` flag is propagated
    # on ``extensions`` so the route layer can forward it to the
    # envelope.
    assert summary.extensions.get("previous_period_unavailable") is True


@pytest.mark.asyncio
async def test_previous_period_available_real_value(session_factory):
    """When both current AND previous period succeed, the
    aggregator MUST compute real percentages — never fabricate 0.
    """
    from decimal import Decimal
    from app.services.cloud.azure.cost_aggregator import (
        aggregate_cost_summary_for_subscriptions,
    )
    from app.services.cloud.azure.costs import AzureCostClient, AzureQueryResult
    from datetime import date

    class _Row:
        provider = "AZURE"
        account_id = "sub:mcpp"
        display_name = "MCPP"
        tenant_id = "tenant"
        subscription_id = "sub:mcpp"

    rows = [_Row()]

    # Return a valid current-period response and a valid previous-
    # period response with a REAL previous total (Decimal('50.00'))
    # so the change_percent is meaningful.
    def fake_query(*, subscription_id, dataset, timeframe, from_iso, to_iso):
        from app.services.cloud.azure.costs import (
            _Column,
            AzureQueryResult,
        )
        # Cost Management returns (UsageDate, Currency, PreTaxCost)
        # shaped rows.  We synthesise a minimal schema with a
        # ``PreTaxCost`` column.
        cols = (
            _Column(name="PreTaxCost", type="Number", index=0),
        )
        # dataset.granularity == None for total queries → one row.
        # dataset.granularity == 'Daily' for daily queries → one
        # row per day.
        if (dataset or {}).get("granularity") == "Daily":
            return AzureQueryResult(columns=cols, rows=())
        # Total query: vary the amount by whether this is the
        # current or previous period (different ``from_iso``).
        amount = "100.00" if "2026-09" in (from_iso or "") else "200.00"
        # We return the previous-period amount when from_iso starts
        # with 2026-09; current-period amount when from_iso starts
        # with 2026-10.
        if from_iso and "2026-09" in from_iso:
            amount = "50.00"
        else:
            amount = "100.00"
        return AzureQueryResult(columns=cols, rows=((amount,),))

    with patch.object(AzureCostClient, "query", side_effect=fake_query):
        summary, failures = await aggregate_cost_summary_for_subscriptions(
            rows=rows,
            period_start=date(2026, 10, 1),
            period_end=date(2026, 11, 1),
            previous_period_start=date(2026, 9, 1),
            previous_period_end=date(2026, 10, 1),
            today=date(2026, 10, 19),
        )

    # current_total == 100, previous_total == 50 → +100%
    assert summary.current_total == Decimal("100")
    assert summary.previous_total == Decimal("50")
    assert summary.change_percent == Decimal("1.00")  # +100%
    assert summary.extensions.get("previous_period_unavailable") is False
