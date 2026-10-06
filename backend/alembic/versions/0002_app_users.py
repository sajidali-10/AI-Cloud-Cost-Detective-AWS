"""create app_users

Revision ID: 0002_app_users
Revises: 0001_cost_cache
Create Date: 2026-10-06

Phase 5A — application-local authentication + RBAC foundation.

This migration introduces the ``app_users`` table backing the
Phase 5A auth model:

    id            BIGSERIAL primary key
    email         VARCHAR(320) NOT NULL UNIQUE  (normalised lower-case)
    password_hash VARCHAR(255) NOT NULL         (Argon2id PHC string)
    display_name  VARCHAR(120) NOT NULL
    role          VARCHAR(16)  NOT NULL         (ADMIN | ANALYST | VIEWER)
    is_active     BOOLEAN      NOT NULL DEFAULT true
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
    last_login_at TIMESTAMPTZ  NULL

The migration is deliberately IDEMPOTENT — both ``upgrade()`` and
``downgrade()`` use ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` / ``DROP ... IF EXISTS`` — so re-
running ``alembic upgrade head`` on a partially-migrated database is
a no-op rather than an error (matching the convention from
``0001_cost_cache.py``).

The CHECK constraint enforces the three roles at the database level
so a programming bug cannot insert an unrecognised role.

No conversation / message table is created here — that belongs to
Phase 5B.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_app_users"
down_revision = "0001_cost_cache"
branch_labels = None
depends_on = None


_UPGRADE_DDL = """
CREATE TABLE IF NOT EXISTS app_users (
    id            BIGSERIAL PRIMARY KEY,
    email         VARCHAR(320) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name  VARCHAR(120) NOT NULL,
    role          VARCHAR(16)  NOT NULL,
    is_active     BOOLEAN      NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ  NULL,
    CONSTRAINT uq_app_users_email UNIQUE (email),
    CONSTRAINT ck_app_users_role CHECK (role IN ('ADMIN', 'ANALYST', 'VIEWER'))
)
"""

_UPGRADE_EMAIL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_email "
    "ON app_users (email)"
)
_UPGRADE_ROLE_INDEX = (
    "CREATE INDEX IF NOT EXISTS ix_app_users_role "
    "ON app_users (role)"
)

_DOWNGRADE_ROLE_INDEX = "DROP INDEX IF EXISTS ix_app_users_role"
_DOWNGRADE_EMAIL_INDEX = "DROP INDEX IF EXISTS uq_app_users_email"
_DOWNGRADE_EMAIL_CONSTRAINT = (
    "ALTER TABLE IF EXISTS app_users "
    "DROP CONSTRAINT IF EXISTS uq_app_users_email"
)
_DOWNGRADE_TABLE = "DROP TABLE IF EXISTS app_users"


def upgrade() -> None:
    op.execute(_UPGRADE_DDL)
    op.execute(_UPGRADE_EMAIL_INDEX)
    op.execute(_UPGRADE_ROLE_INDEX)


def downgrade() -> None:
    op.execute(_DOWNGRADE_ROLE_INDEX)
    # Postgres owns the unique index backing the UNIQUE constraint;
    # dropping it directly fails with "DependentObjectsStillExist".
    # Drop the constraint first, then the (now redundant) index.
    op.execute(_DOWNGRADE_EMAIL_CONSTRAINT)
    op.execute(_DOWNGRADE_EMAIL_INDEX)
    op.execute(_DOWNGRADE_TABLE)
