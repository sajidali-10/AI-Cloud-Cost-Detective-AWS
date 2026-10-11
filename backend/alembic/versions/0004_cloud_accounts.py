"""Phase 7B — cloud-account registry.

Adds the ``cloud_accounts`` table that drives the multi-AWS-account
dashboard.  No data migration is performed here; the management row
is inserted at application boot by
:func:`app.services.aws.account_provider.ensure_management_account_row`
so that a fresh deployment and an existing one behave identically.

Revision ID: 0004_cloud_accounts
Revises: 0003_conversations
Create Date: 2026-10-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0004_cloud_accounts"
down_revision = "0003_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cloud_accounts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("account_id", sa.String(length=12), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role_arn", sa.String(length=2048), nullable=False),
        sa.Column("external_id", sa.String(length=1224), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_management",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("preferred_region", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider",
            "account_id",
            name="uq_cloud_accounts_provider_account",
        ),
        sa.CheckConstraint(
            "provider IN ('AWS')",
            name="ck_cloud_accounts_provider",
        ),
    )

    # Partial unique index: enforce a single management row per
    # provider.  Postgres and SQLite express the partial predicate
    # differently; the inline DDL keeps both happy.
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_cloud_accounts_provider_management "
            "ON cloud_accounts (provider) WHERE is_management = true"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_cloud_accounts_provider_management "
            "ON cloud_accounts (provider) WHERE is_management = 1"
        )

    op.create_index(
        "ix_cloud_accounts_provider_enabled",
        "cloud_accounts",
        ["provider", "enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cloud_accounts_provider_enabled", table_name="cloud_accounts"
    )
    op.drop_index(
        "uq_cloud_accounts_provider_management", table_name="cloud_accounts"
    )
    op.drop_table("cloud_accounts")
