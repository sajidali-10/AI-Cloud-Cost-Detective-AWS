"""create cost_cache

Revision ID: 0001_cost_cache
Revises:
Create Date: 2026-10-05

This is the first Phase 2 migration.  It introduces the ``cost_cache``
table that backs the read-through cache in front of AWS Cost Explorer.

The migration is deliberately IDEMPOTENT — both ``upgrade()`` and
``downgrade()`` use ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` / ``DROP ... IF EXISTS`` — so re-running
``alembic upgrade head`` on a partially-migrated database is a no-op
rather than an error.  Phase 2's ``phase2_verify.sh`` exercises this
guarantee explicitly.

The DDL mirrors the SQLAlchemy ORM in ``app.db.models.CostCache``
exactly; if the two ever diverge, this migration is the source of
truth (the ORM is for reads/writes only).
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_cost_cache"
down_revision = None
branch_labels = None
depends_on = None


# DDL is kept in module-level constants so it is grep-able and easy
# to audit in code review (vs. hidden inside an op.execute call).
_UPGRADE_DDL = """
CREATE TABLE IF NOT EXISTS cost_cache (
    id           BIGSERIAL PRIMARY KEY,
    account_id   VARCHAR(64)  NOT NULL,
    cache_key    VARCHAR(255) NOT NULL,
    query_type   VARCHAR(64)  NOT NULL,
    period_start TIMESTAMPTZ  NOT NULL,
    period_end   TIMESTAMPTZ  NOT NULL,
    payload      JSONB        NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ  NOT NULL,
    CONSTRAINT uq_cost_cache_account_key UNIQUE (account_id, cache_key)
)
"""

_UPGRADE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_cost_cache_expires_at "
    "ON cost_cache (expires_at)"
)

_DOWNGRADE_INDEX = "DROP INDEX IF EXISTS ix_cost_cache_expires_at"
_DOWNGRADE_TABLE = "DROP TABLE IF EXISTS cost_cache"


def upgrade() -> None:
    op.execute(_UPGRADE_DDL)
    op.execute(_UPGRADE_INDEX)


def downgrade() -> None:
    op.execute(_DOWNGRADE_INDEX)
    op.execute(_DOWNGRADE_TABLE)
