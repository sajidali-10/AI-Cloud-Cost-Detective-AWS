"""SQLAlchemy engine + session plumbing for the application database.

This module is the single source of truth for the application's
SQLAlchemy ``Engine``, ``SessionLocal``, and the FastAPI dependency
``get_db``.  Phase 1's ``main.py`` built its own ad-hoc engine inline
for the readiness probe — that engine is now replaced by the one
defined here.

Connection details come from :func:`app.core.config.get_settings`,
specifically the ``database_url`` property, which points at the
``cost_detective`` logical database (NOT the LiteLLM database).

Phase 2 does not run ``create_all`` on startup: the ``cost_cache``
table is created exclusively by Alembic
(``backend/alembic/versions/0001_cost_cache.py``).
"""
from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# ``pool_pre_ping=True`` matches the readiness-probe behavior in
# ``main.py``: stale connections are recycled transparently.  We avoid
# a connection pool much larger than the default — the Phase 2 cache
# is a write-heavy-but-small workload and the LiteLLM DB is not
# accessed here at all.
_engine: Engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    future=True,
)

# Module-level alias for callers (and tests) that want the bound engine
# without going through ``get_engine()``.  Phase 1's ``main.py``
# readiness probe used to build its own engine; that path now imports
# this name instead.
engine = _engine

SessionLocal = sessionmaker(
    bind=_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine.

    Exposed as a function (rather than re-exporting the module-level
    binding) so tests can monkey-patch the underlying DSN without
    having to re-import the symbol.
    """
    return _engine


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a request-scoped SQLAlchemy session.

    The session is closed deterministically when the request finishes,
    even on exception paths.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["get_engine", "engine", "SessionLocal", "get_db"]
