"""Unit tests for ``app.services.cost_cache`` — Phase 2.

These tests exercise the read-through cache layer against an in-memory
SQLite database (no Postgres required) so they can run in CI without
docker compose.

Spec coverage:

* cache miss -> MISS path persists
* cache hit -> HIT path returns the same payload
* expired entry -> REFRESHED path re-loads
* loader error -> NOT cached, exception propagates
* concurrent identical requests -> only one loader call (dedup)
* lazy probabilistic GC -> bounded DELETE runs occasionally
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, CostCache
from app.schemas.cost import (
    CacheStatus,
    CostPeriod,
    CostReport,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)


# ---------------------------------------------------------------------------
# Fixtures: in-memory SQLite + monkey-patched session
# ---------------------------------------------------------------------------


@pytest.fixture
def test_engine(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionTesting = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    monkeypatch.setattr("app.services.cost_cache.SessionLocal", SessionTesting)
    return engine


def _make_report(total: str = "10.00", days: int = 7) -> CostReport:
    today = datetime.now(timezone.utc).date()
    period = CostPeriod(start=today - timedelta(days=days), end=today, days=days)
    return CostReport(
        account_id="111122223333",
        period=period,
        previous_period=CostPeriod(
            start=today - timedelta(days=days * 2),
            end=today - timedelta(days=days),
            days=days,
        ),
        currency="USD",
        total_cost=Decimal(total),
        previous_period_cost=Decimal("5.00"),
        change_amount=Decimal(total) - Decimal("5.00"),
        change_percent=Decimal("1.00"),
        estimated=False,
        daily_trend=[
            DailyCostPoint(date=period.start + timedelta(days=i), amount=Decimal("1.00"), unit="USD")
            for i in range(days)
        ],
        by_service=[ServiceCost(service="Amazon EC2", amount=Decimal(total), unit="USD")],
        by_region=[RegionCost(region="us-east-1", amount=Decimal(total), unit="USD")],
        source="AWS_COST_EXPLORER",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_then_hit(test_engine) -> None:
    from app.services.cost_cache import get_or_refresh

    calls = {"n": 0}

    async def loader() -> CostReport:
        calls["n"] += 1
        return _make_report(total="10.00")

    first = await get_or_refresh(account_id="acct", cache_key="k1", loader=loader)
    assert first.cache_status == CacheStatus.REFRESHED
    assert calls["n"] == 1

    second = await get_or_refresh(account_id="acct", cache_key="k1", loader=loader)
    assert second.cache_status == CacheStatus.HIT
    assert second.report.total_cost == Decimal("10.00")
    assert calls["n"] == 1  # loader was NOT called again


@pytest.mark.asyncio
async def test_expired_entry_refreshes(test_engine) -> None:
    from app.services.cost_cache import get_or_refresh

    # Seed an already-expired row.
    from app.services.cost_cache import SessionLocal

    session = SessionLocal()
    now = datetime.now(timezone.utc)
    row = CostCache(
        account_id="acct",
        cache_key="k1",
        query_type="AWS_COST_EXPLORER",
        period_start=now - timedelta(days=30),
        period_end=now - timedelta(days=23),
        payload={"_placeholder": True},
        expires_at=now - timedelta(seconds=1),
    )
    session.add(row)
    session.commit()
    session.close()

    calls = {"n": 0}

    async def loader() -> CostReport:
        calls["n"] += 1
        return _make_report(total="42.00")

    out = await get_or_refresh(account_id="acct", cache_key="k1", loader=loader)
    assert out.cache_status == CacheStatus.REFRESHED
    assert calls["n"] == 1
    assert out.report.total_cost == Decimal("42.00")


@pytest.mark.asyncio
async def test_loader_error_is_not_cached(test_engine) -> None:
    from app.services.cost_cache import get_or_refresh, SessionLocal

    async def loader() -> CostReport:
        raise RuntimeError("AWS is down")

    with pytest.raises(RuntimeError):
        await get_or_refresh(account_id="acct", cache_key="k1", loader=loader)

    # Confirm nothing was persisted.
    session = SessionLocal()
    rows = session.query(CostCache).filter_by(account_id="acct", cache_key="k1").all()
    session.close()
    assert rows == []


@pytest.mark.asyncio
async def test_concurrent_identical_requests_deduped(test_engine) -> None:
    from app.services.cost_cache import get_or_refresh

    calls = {"n": 0}
    started = threading.Event()

    async def loader() -> CostReport:
        calls["n"] += 1
        # Hold the lock so the second caller has to wait.
        import asyncio
        await asyncio.sleep(0.05)
        return _make_report(total="7.00")

    import asyncio
    task_a = asyncio.create_task(get_or_refresh(account_id="acct", cache_key="k1", loader=loader))
    task_b = asyncio.create_task(get_or_refresh(account_id="acct", cache_key="k1", loader=loader))
    a, b = await asyncio.gather(task_a, task_b)
    assert calls["n"] == 1
    # Both callers get the same payload; one is REFRESHED, the other HIT.
    statuses = {a.cache_status, b.cache_status}
    assert statuses == {CacheStatus.HIT, CacheStatus.REFRESHED}


@pytest.mark.asyncio
async def test_lazy_probabilistic_gc_runs_and_is_bounded(test_engine, monkeypatch) -> None:
    from app.services.cost_cache import get_or_refresh, maybe_collect_garbage, SessionLocal

    # Seed 5 expired rows.
    session = SessionLocal()
    now = datetime.now(timezone.utc)
    for i in range(5):
        session.add(
            CostCache(
                account_id="acct",
                cache_key=f"expired-{i}",
                query_type="AWS_COST_EXPLORER",
                period_start=now - timedelta(days=30),
                period_end=now - timedelta(days=23),
                payload={"_placeholder": True},
                expires_at=now - timedelta(seconds=10),
            )
        )
    session.commit()
    session.close()

    # Force the dice roll to land at 1.0 so GC runs deterministically.
    monkeypatch.setattr("app.services.cost_cache.random.random", lambda: 0.0)

    from app.services.cost_cache import SessionLocal as S
    session = S()
    deleted = maybe_collect_garbage(session, probability=1.0, batch=3)
    session.close()
    assert deleted == 3  # bounded by batch size


@pytest.mark.asyncio
async def test_lazy_probabilistic_gc_skipped_when_probability_zero(test_engine) -> None:
    """GC is short-circuited entirely when probability <= 0.

    This is the deterministic counterpart to the probabilistic test
    above: when the operator sets ``COST_CACHE_GC_PROBABILITY=0`` (or
    the in-process caller passes ``probability=0``), GC must do
    NOTHING regardless of what ``random.random()`` would return.
    """
    from app.services.cost_cache import maybe_collect_garbage, SessionLocal

    session = SessionLocal()
    now = datetime.now(timezone.utc)
    session.add(
        CostCache(
            account_id="acct",
            cache_key="expired",
            query_type="AWS_COST_EXPLORER",
            period_start=now - timedelta(days=30),
            period_end=now - timedelta(days=23),
            payload={"_placeholder": True},
            expires_at=now - timedelta(seconds=10),
        )
    )
    session.commit()
    session.close()

    session = SessionLocal()
    deleted = maybe_collect_garbage(session, probability=0.0, batch=100)
    session.close()
    assert deleted == 0
