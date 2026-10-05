"""SQLAlchemy ORM models for the application database.

Phase 2 adds a single table — ``cost_cache`` — used by the read-through
cache layer in front of AWS Cost Explorer.  The table is intentionally
narrow: a unique cache key (per ``account_id``), a query-type tag, the
period the cached payload covers, the JSON-serialized payload itself,
and the standard ``created_at`` / ``expires_at`` bookkeeping.

Phase 0 and Phase 1 do not own any DB tables.  This module is the
first persistence surface introduced by Phase 2.  The actual schema
is created and migrated by Alembic (``backend/alembic/versions/
0001_cost_cache.py``); this ORM is for application reads/writes only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator


# Dialect-aware primary-key column.  Postgres production deployments
# get a 64-bit BIGSERIAL; the in-memory SQLite used by the unit tests
# gets a plain ``INTEGER PRIMARY KEY`` (which SQLite aliases to
# ``ROWID`` and autoincrements).  Without the SQLite branch, the
# tests crash with ``NOT NULL constraint failed: cost_cache.id``.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class _JSONBType(TypeDecorator):
    """Postgres ``JSONB`` when available, portable ``JSON`` otherwise.

    Phase 2 production runs against Postgres where ``JSONB`` is the
    right choice for the ``cost_cache.payload`` column.  Unit tests
    exercise the same ORM against in-memory SQLite, which does not
    have ``JSONB``.  This decorator maps Postgres to ``JSONB`` and
    every other dialect to ``JSON`` so the ORM works in both worlds
    without conditional DDL.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    """Declarative base for all Phase 2+ ORM models."""


class CostCache(Base):
    """Read-through cache row for AWS Cost Explorer responses.

    A cache row is keyed by ``(account_id, cache_key)`` — the cache key
    already encodes every dimension that affects the AWS response
    (lookback days, region, query type, metric set).  The payload is
    stored as JSONB so the cached structure survives schema evolution
    without forcing a column migration every time we add a field.

    Errors are NEVER persisted: a failed Cost Explorer call must not
    poison the cache.  Only successful responses are written here, and
    the application layer enforces that invariant.
    """

    __tablename__ = "cost_cache"
    __table_args__ = (
        UniqueConstraint("account_id", "cache_key", name="uq_cost_cache_account_key"),
        Index("ix_cost_cache_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(255), nullable=False)
    query_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # The cached period.  Both are stored as UTC datetimes (not strings)
    # so Postgres handles timezone normalization consistently.
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # The serialized AWS response.  We keep the column narrow (numeric
    # bound on size is enforced at the application layer; Postgres does
    # not enforce a JSONB size cap).  Stored amounts inside the JSONB
    # are Decimal-encoded by the application layer before insertion.
    payload: Mapped[dict] = mapped_column(_JSONBType, nullable=False)

    # Server-side timestamps.  We rely on ``now()`` at the database
    # level so a misconfigured application clock cannot poison the TTL
    # math.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True if this row has passed its ``expires_at``.

        Defensive against SQLite returning offset-naive datetimes
        even when the column was written with ``TIMESTAMPTZ`` on
        Postgres.  When ``expires_at`` is naive we treat it as UTC,
        matching the convention used everywhere else in the app.
        """
        ts = now or datetime.now(timezone.utc)
        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return ts >= expires_at


__all__ = ["Base", "CostCache", "Decimal"]
