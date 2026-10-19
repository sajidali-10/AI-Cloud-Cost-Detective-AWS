"""azure cost cache — invalidate invalid LKG rows

Revision ID: 0007_azure_invalidate_invalid_lkg
Revises: 0006_azure_cost_cache_resilience
Create Date: 2026-10-19

Phase 7D.3.3 (live closure fix) — delete any Azure cost-cache
rows whose ``stale_payload`` or ``payload`` is NOT an authoritative
success response.  These rows were created by an earlier
authority-predicate bug that read ``payload.get('status')``
against the wrong shape (the canonical envelope serialises the
status at the top level, not under ``payload['report']``).

An invalid row looks like::

    payload: { "provider": "AZURE", ..., "status": "PARTIAL_SUCCESS",
               "successful_accounts": 0, ... }
    stale_payload: NULL  (because the predicate always returned False)

If a later request hits this row while ``expires_at > now``, the
old code returned it as ``cache_status=HIT`` — a throttled-zero
served as authoritative LKG.  That violates the Phase 7D.3.2 +
7D.3.3 contract.

The fix is two-fold:

1. The code-level fix (in ``cost_cache.py``) is in the same
   commit — non-authoritative responses no longer overwrite the
   ``payload`` column and the read path rejects non-authoritative
   cached payloads.

2. This migration cleans up any pre-existing bad rows so the fix
   takes effect immediately on the next request without requiring
   every read to fall through to the loader.  AWS rows are NOT
   affected (``cache_namespace='aws'`` is the predicate).

A row is considered "valid" when:

* ``payload`` has ``status='SUCCESS'`` / ``'PARTIAL_SUCCESS'`` /
  ``'NO_DATA'`` AND ``successful_accounts > 0``, OR
* ``payload`` is NULL (row only carries ``stale_payload``).

A row is "invalid" when ``payload`` has ``status='PARTIAL_SUCCESS'``
or ``'ERROR'`` or ``'NO_DATA'`` AND ``successful_accounts = 0``.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0007_azure_lkg_cleanup"
down_revision = "0006_azure_cost_cache_resilience"
branch_labels = None
depends_on = None


_UPGRADE_SQL = """
DELETE FROM cost_cache
WHERE cache_namespace = 'azure'
  AND payload IS NOT NULL
  AND (
    (
      (payload->>'status') IN ('PARTIAL_SUCCESS', 'ERROR', 'NO_DATA')
      AND COALESCE((payload->>'successful_accounts')::int, 0) = 0
    )
    OR (payload->>'status') IS NULL
    OR (payload->>'provider') IS DISTINCT FROM 'AZURE'
  );
"""

_DOWNGRADE_SQL = """
-- No-op: this migration deletes data, there is nothing to restore.
SELECT 1;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
