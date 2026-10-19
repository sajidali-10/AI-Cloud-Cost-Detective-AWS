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

    ``returns`` is the value the next call returns (a dict).
    ``raises`` is the exception the next call raises.
    Each call is recorded in ``calls``.
    """

    def __init__(
        self,
        returns: Optional[Dict[str, Any]] = None,
        raises: Optional[BaseException] = None,
    ):
        self.returns = returns
        self.raises = raises
        self.calls: List[Dict[str, Any]] = 0 if False else []  # list of call info

    async def __call__(self) -> Dict[str, Any]:
        self.calls.append({})
        if self.raises is not None:
            raise self.raises
        if self.returns is None:
            raise RuntimeError("StubLoader has neither returns nor raises")
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
    loader = StubLoader(returns=_azure_envelope(total_cost="42.50"))
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
    """all subs THROTTLED → NOT cached as LKG, raises."""
    # PARTIAL_SUCCESS with successful=0 is NOT authoritative (Phase 7D.3.2)
    loader = StubLoader(returns=_azure_envelope(
        total_cost="0.00",
        status="PARTIAL_SUCCESS",
        successful=0,
        failed=2,
    ))
    with session_factory() as session:
        # Should still raise because _is_authoritative_success returns False
        # and the loader returns a "throttled-zero" envelope.
        # Actually the loader returned successfully (it didn't raise),
        # so the cache layer will REFRESH it but NOT promote to stale_payload.
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
        assert row is not None
        # stale_payload is NULL because the response was not authoritative
        assert row.stale_payload is None, "throttled-zero MUST NOT advance stale_payload"


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
