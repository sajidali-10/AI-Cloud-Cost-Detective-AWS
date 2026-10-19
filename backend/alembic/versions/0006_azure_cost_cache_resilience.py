"""azure cost cache resilience — fresh + stale model

Revision ID: 0006_azure_cost_cache_resilience
Revises: 0005_cloud_provider_abstraction
Create Date: 2026-10-19

Phase 7D.3.3 extends the existing ``cost_cache`` table with a
FRESH + STALE-LAST-GOOD model for Azure Cost Management.

New columns
-----------

* ``cache_namespace`` — ``aws`` or ``azure``; isolates the two
  providers so AWS and Azure rows can never collide on
  ``(account_id, cache_key)``.  Existing AWS rows are preserved
  with the default ``aws`` value.
* ``last_successful_at`` — wall-clock of the most recent
  successful upstream call; surfaced to the API so the frontend
  can render "Stale · last successful <time>" without re-querying.
* ``stale_payload`` — JSONB copy of the last successful response;
  the row is only promoted to STALE-fallback when this is non-null.
* ``stale_until`` — hard cap of the stale-fallback window;
  beyond this the row is treated as empty (caller falls back to
  ``unavailable``).
* ``azure_post_count`` — number of Azure Cost Management POSTs in
  the last successful refresh; BEFORE/AFTER measurement of
  request-count reduction.

Indexes
-------

* ``(cache_namespace, account_id, cache_key)`` so Azure lookups
  don't scan AWS rows.
* ``(stale_until)`` so the GC can prune expired stale rows.

The migration is IDEMPOTENT — each ``ADD COLUMN`` uses
``IF NOT EXISTS`` and indexes use ``IF NOT EXISTS`` so re-running
``alembic upgrade head`` is a no-op.  Existing rows are left
untouched (``cache_namespace`` defaults to ``aws``).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0006_azure_cost_cache_resilience"
down_revision = "0005_cloud_provider_abstraction"
branch_labels = None
depends_on = None


_UPGRADE_DDL = """
ALTER TABLE cost_cache
    ADD COLUMN IF NOT EXISTS last_successful_at TIMESTAMPTZ;

ALTER TABLE cost_cache
    ADD COLUMN IF NOT EXISTS stale_payload JSONB;

ALTER TABLE cost_cache
    ADD COLUMN IF NOT EXISTS stale_until TIMESTAMPTZ;

ALTER TABLE cost_cache
    ADD COLUMN IF NOT EXISTS azure_post_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cost_cache
    ADD COLUMN IF NOT EXISTS cache_namespace VARCHAR(32) NOT NULL DEFAULT 'aws';

CREATE INDEX IF NOT EXISTS ix_cost_cache_namespace_account_key
    ON cost_cache (cache_namespace, account_id, cache_key);

CREATE INDEX IF NOT EXISTS ix_cost_cache_stale_until
    ON cost_cache (stale_until);
"""

_DOWNGRADE_DDL = """
DROP INDEX IF EXISTS ix_cost_cache_stale_until;
DROP INDEX IF EXISTS ix_cost_cache_namespace_account_key;
ALTER TABLE cost_cache DROP COLUMN IF EXISTS cache_namespace;
ALTER TABLE cost_cache DROP COLUMN IF EXISTS azure_post_count;
ALTER TABLE cost_cache DROP COLUMN IF EXISTS stale_until;
ALTER TABLE cost_cache DROP COLUMN IF EXISTS stale_payload;
ALTER TABLE cost_cache DROP COLUMN IF EXISTS last_successful_at;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_DDL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_DDL)
