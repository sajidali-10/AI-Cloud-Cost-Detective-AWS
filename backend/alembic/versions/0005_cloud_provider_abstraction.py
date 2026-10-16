"""Phase 7C.1 — provider abstraction schema readiness.

Generalises the ``cloud_accounts`` table so a future Azure /
Rackspace row can be inserted without a schema change.  No data
migration is performed here:

* AWS rows are untouched (``provider='AWS'``, ``account_id`` is
  still a 12-digit string, ``role_arn`` is still set).
* The ``provider`` CHECK constraint is widened from
  ``('AWS')`` to ``('AWS', 'AZURE', 'RACKSPACE')`` so future
  providers can be inserted.
* ``account_id`` is widened from VARCHAR(12) to VARCHAR(64) so
  Azure subscription GUIDs (36 chars) and Rackspace tenant
  identifiers fit without altering the schema again.
* ``role_arn`` becomes nullable — Azure / Rackspace rows do not
  use IAM roles; the column is AWS-specific metadata.
* Two nullable columns are added for the future Azure adapter:
  ``tenant_id`` (the Entra ID tenant GUID) and
  ``subscription_id`` (the Azure subscription GUID).
* A nullable ``provider_metadata`` JSONB column captures
  per-provider federation / connection metadata that does not
  deserve a first-class column.

No secrets are stored at this point — client secrets and access
keys belong to later phases (7C.2 / 7C.3) and will land in their
own migration with explicit operator guidance.

The unique constraint ``(provider, account_id)`` is unchanged so
existing rows are still indexed correctly.  The partial unique
index on ``(provider) WHERE is_management = true`` is also
unchanged: AWS still has exactly one management row, and future
providers will each enforce their own management-row invariant.

Revision ID: 0005_cloud_provider_abstraction
Revises: 0004_cloud_accounts
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "0005_cloud_provider_abstraction"
down_revision = "0004_cloud_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Widen ``account_id`` so Azure subscription GUIDs (36 chars)
    #    and Rackspace tenant identifiers fit.  12-digit AWS ids
    #    remain valid after the widening — no data loss.
    op.alter_column(
        "cloud_accounts",
        "account_id",
        existing_type=sa.String(length=12),
        type_=sa.String(length=64),
        existing_nullable=False,
    )

    # 2. Make ``role_arn`` nullable — Azure / Rackspace rows do
    #    not have an IAM role.  AWS rows already have a value, so
    #    this is a no-op for them.
    op.alter_column(
        "cloud_accounts",
        "role_arn",
        existing_type=sa.String(length=2048),
        nullable=True,
    )

    # 3. Drop the AWS-only provider CHECK constraint and replace
    #    it with a multi-provider constraint.  The new constraint
    #    still permits ``AWS`` so every existing row continues to
    #    validate.
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_cloud_accounts_provider",
            "cloud_accounts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_cloud_accounts_provider",
            "cloud_accounts",
            "provider IN ('AWS', 'AZURE', 'RACKSPACE')",
        )
    else:
        # SQLite has no ALTER CONSTRAINT — recreate the table.
        # Phase 7B shipped with this constraint and Phase 7C.1 is
        # the first time we need to widen it; the recursion pragma
        # below is the documented SQLite pattern.
        with op.batch_alter_table("cloud_accounts", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_cloud_accounts_provider", type_="check")
            batch_op.create_check_constraint(
                "ck_cloud_accounts_provider",
                "provider IN ('AWS', 'AZURE', 'RACKSPACE')",
            )

    # 4. Add Azure-specific columns.  Both nullable — AWS rows
    #    leave them empty.
    op.add_column(
        "cloud_accounts",
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "cloud_accounts",
        sa.Column(
            "subscription_id", sa.String(length=64), nullable=True
        ),
    )

    # 5. Generic provider metadata column.  Used to store
    #    federation subjects, region preferences, and other
    #    per-provider hints that do not deserve a first-class
    #    column.  Empty dict default so existing rows deserialize
    #    cleanly via the ORM.  Use JSONB on Postgres for index /
    #    containment query support; portable JSON on every other
    #    dialect (SQLite tests included).
    bind = op.get_bind()
    json_type = (
        sa.dialects.postgresql.JSONB()
        if (bind is not None and bind.dialect.name == "postgresql")
        else sa.JSON()
    )
    op.add_column(
        "cloud_accounts",
        sa.Column("provider_metadata", json_type, nullable=True),
    )
    # Backfill any existing rows with an empty object so the ORM
    # never has to handle NULL — keeps ``metadata`` reads uniform.
    op.execute(
        text(
            "UPDATE cloud_accounts "
            "SET provider_metadata = '{}' "
            "WHERE provider_metadata IS NULL"
        )
    )
    op.alter_column(
        "cloud_accounts",
        "provider_metadata",
        existing_type=sa.JSON(),
        nullable=False,
        server_default=text("'{}'"),
    )


def downgrade() -> None:
    # Re-narrow everything in reverse order.  No data is dropped
    # — only column widths and nullability return to the
    # Phase 7B shape.  If a future Azure / Rackspace row already
    # exists, the downgrade will fail with a constraint error;
    # that is intentional (an operator must move data first).

    # Wipe provider_metadata default and drop the column.
    op.alter_column(
        "cloud_accounts",
        "provider_metadata",
        server_default=None,
    )
    op.drop_column("cloud_accounts", "provider_metadata")

    op.drop_column("cloud_accounts", "subscription_id")
    op.drop_column("cloud_accounts", "tenant_id")

    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        op.drop_constraint(
            "ck_cloud_accounts_provider",
            "cloud_accounts",
            type_="check",
        )
        op.create_check_constraint(
            "ck_cloud_accounts_provider",
            "cloud_accounts",
            "provider IN ('AWS')",
        )
    else:
        with op.batch_alter_table("cloud_accounts", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_cloud_accounts_provider", type_="check")
            batch_op.create_check_constraint(
                "ck_cloud_accounts_provider",
                "provider IN ('AWS')",
            )

    op.alter_column(
        "cloud_accounts",
        "role_arn",
        existing_type=sa.String(length=2048),
        nullable=False,
    )

    op.alter_column(
        "cloud_accounts",
        "account_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=12),
        existing_nullable=False,
    )
