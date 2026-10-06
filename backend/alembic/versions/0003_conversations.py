"""create conversations and conversation_messages

Revision ID: 0003_conversations
Revises: 0002_app_users
Create Date: 2026-10-06

Phase 5B — authenticated, user-owned conversation + AI history
persistence.

This migration introduces two tables:

    conversations
        id            BIGSERIAL PRIMARY KEY
        user_id       BIGINT  NOT NULL   -> app_users.id (CASCADE)
        title         VARCHAR(200) NOT NULL DEFAULT 'New Cost Analysis'
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        last_message_at TIMESTAMPTZ NULL
        is_archived   BOOLEAN NOT NULL DEFAULT false

    conversation_messages
        id                BIGSERIAL PRIMARY KEY
        conversation_id   BIGINT  NOT NULL -> conversations.id (CASCADE)
        role              VARCHAR(16) NOT NULL (CHECK USER|ASSISTANT|SYSTEM_EVENT)
        content           TEXT NOT NULL
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        operation_type    VARCHAR(32) NULL
        model_alias       VARCHAR(64) NULL
        grounding_metadata JSONB NULL
        evidence_references JSONB NULL
        warnings          JSONB NULL
        token_usage       JSONB NULL
        error_code        VARCHAR(64) NULL  (for SYSTEM_EVENT rows)

Ownership / tenant isolation:

* Every query that reads or mutates a conversation is scoped by
  ``user_id`` at the application layer.  The schema itself does
  NOT enforce it (Postgres RLS would be the right tool, but is
  intentionally out of scope for Phase 5B — the application
  layer's tests prove the scoping invariant).
* ``ON DELETE CASCADE`` keeps user and conversation deletion
  deterministic: removing a user removes their conversations and
  every message; removing a conversation removes its messages.

The migration is deliberately IDEMPOTENT — ``CREATE TABLE IF NOT
EXISTS`` / ``DROP TABLE IF EXISTS`` / ``DROP INDEX IF EXISTS`` /
``DROP CONSTRAINT IF EXISTS`` — so re-running ``alembic upgrade
head`` on a partially-migrated database is a no-op rather than an
error (matching the convention from ``0001_cost_cache.py`` and
``0002_app_users.py``).

The CHECK constraint on ``role`` prevents a programming bug from
inserting an unrecognised role into the message table.

No raw provider payloads, no Authorization headers, no JWTs, and
no AWS credentials are stored here — the metadata columns are
intentionally narrow and the application layer only writes safe
provenance.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_conversations"
down_revision = "0002_app_users"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Upgrade DDL (idempotent).
# ---------------------------------------------------------------------------

_UPGRADE_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT  NOT NULL,
    title           VARCHAR(200) NOT NULL DEFAULT 'New Cost Analysis',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_message_at TIMESTAMPTZ NULL,
    is_archived     BOOLEAN NOT NULL DEFAULT false,
    CONSTRAINT ck_conversations_title_length
        CHECK (length(title) > 0 AND length(title) <= 200)
)
"""

_UPGRADE_CONVERSATIONS_USER_FK = (
    "ALTER TABLE conversations "
    "DROP CONSTRAINT IF EXISTS fk_conversations_user_id"
)
_UPGRADE_CONVERSATIONS_USER_FK_ADD = (
    "ALTER TABLE conversations "
    "ADD CONSTRAINT fk_conversations_user_id "
    "FOREIGN KEY (user_id) REFERENCES app_users(id) "
    "ON DELETE CASCADE"
)

_UPGRADE_IX_CONVERSATIONS_USER_ID = (
    "CREATE INDEX IF NOT EXISTS ix_conversations_user_id "
    "ON conversations (user_id)"
)
_UPGRADE_IX_CONVERSATIONS_USER_UPDATED = (
    "CREATE INDEX IF NOT EXISTS ix_conversations_user_updated "
    "ON conversations (user_id, updated_at)"
)
_UPGRADE_IX_CONVERSATIONS_USER_ARCHIVED_LASTMSG = (
    "CREATE INDEX IF NOT EXISTS ix_conversations_user_archived_lastmsg "
    "ON conversations (user_id, is_archived, last_message_at)"
)

_UPGRADE_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    id                  BIGSERIAL PRIMARY KEY,
    conversation_id     BIGINT  NOT NULL,
    role                VARCHAR(16) NOT NULL,
    content             TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation_type      VARCHAR(32) NULL,
    model_alias         VARCHAR(64) NULL,
    grounding_metadata  JSONB NULL,
    evidence_references JSONB NULL,
    warnings            JSONB NULL,
    token_usage         JSONB NULL,
    error_code          VARCHAR(64) NULL,
    CONSTRAINT ck_conversation_messages_role
        CHECK (role IN ('USER', 'ASSISTANT', 'SYSTEM_EVENT'))
)
"""

_UPGRADE_MESSAGES_CONV_FK = (
    "ALTER TABLE conversation_messages "
    "DROP CONSTRAINT IF EXISTS fk_conversation_messages_conv_id"
)
_UPGRADE_MESSAGES_CONV_FK_ADD = (
    "ALTER TABLE conversation_messages "
    "ADD CONSTRAINT fk_conversation_messages_conv_id "
    "FOREIGN KEY (conversation_id) REFERENCES conversations(id) "
    "ON DELETE CASCADE"
)

_UPGRADE_IX_MESSAGES_CONV_CREATED = (
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_conv_created "
    "ON conversation_messages (conversation_id, created_at)"
)
_UPGRADE_IX_MESSAGES_CONV_ROLE = (
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_conv_role "
    "ON conversation_messages (conversation_id, role)"
)


# ---------------------------------------------------------------------------
# Downgrade DDL (idempotent).
# ---------------------------------------------------------------------------

_DOWNGRADE_IX_MESSAGES_CONV_ROLE = (
    "DROP INDEX IF EXISTS ix_conversation_messages_conv_role"
)
_DOWNGRADE_IX_MESSAGES_CONV_CREATED = (
    "DROP INDEX IF EXISTS ix_conversation_messages_conv_created"
)
_DOWNGRADE_MESSAGES_FK = (
    "ALTER TABLE IF EXISTS conversation_messages "
    "DROP CONSTRAINT IF EXISTS fk_conversation_messages_conv_id"
)
_DOWNGRADE_MESSAGES_TABLE = "DROP TABLE IF EXISTS conversation_messages"

_DOWNGRADE_IX_CONVERSATIONS_USER_ARCHIVED_LASTMSG = (
    "DROP INDEX IF EXISTS ix_conversations_user_archived_lastmsg"
)
_DOWNGRADE_IX_CONVERSATIONS_USER_UPDATED = (
    "DROP INDEX IF EXISTS ix_conversations_user_updated"
)
_DOWNGRADE_IX_CONVERSATIONS_USER_ID = (
    "DROP INDEX IF EXISTS ix_conversations_user_id"
)
_DOWNGRADE_CONVERSATIONS_FK = (
    "ALTER TABLE IF EXISTS conversations "
    "DROP CONSTRAINT IF EXISTS fk_conversations_user_id"
)
_DOWNGRADE_CONVERSATIONS_TABLE = "DROP TABLE IF EXISTS conversations"


def upgrade() -> None:
    # Conversations first — messages reference it via FK.
    op.execute(_UPGRADE_CONVERSATIONS_DDL)
    op.execute(_UPGRADE_CONVERSATIONS_USER_FK)
    op.execute(_UPGRADE_CONVERSATIONS_USER_FK_ADD)
    op.execute(_UPGRADE_IX_CONVERSATIONS_USER_ID)
    op.execute(_UPGRADE_IX_CONVERSATIONS_USER_UPDATED)
    op.execute(_UPGRADE_IX_CONVERSATIONS_USER_ARCHIVED_LASTMSG)

    op.execute(_UPGRADE_MESSAGES_DDL)
    op.execute(_UPGRADE_MESSAGES_CONV_FK)
    op.execute(_UPGRADE_MESSAGES_CONV_FK_ADD)
    op.execute(_UPGRADE_IX_MESSAGES_CONV_CREATED)
    op.execute(_UPGRADE_IX_MESSAGES_CONV_ROLE)


def downgrade() -> None:
    # Reverse order: messages first (depends on conversations), then
    # the conversations table itself.
    op.execute(_DOWNGRADE_IX_MESSAGES_CONV_ROLE)
    op.execute(_DOWNGRADE_IX_MESSAGES_CONV_CREATED)
    op.execute(_DOWNGRADE_MESSAGES_FK)
    op.execute(_DOWNGRADE_MESSAGES_TABLE)

    op.execute(_DOWNGRADE_IX_CONVERSATIONS_USER_ARCHIVED_LASTMSG)
    op.execute(_DOWNGRADE_IX_CONVERSATIONS_USER_UPDATED)
    op.execute(_DOWNGRADE_IX_CONVERSATIONS_USER_ID)
    op.execute(_DOWNGRADE_CONVERSATIONS_FK)
    op.execute(_DOWNGRADE_CONVERSATIONS_TABLE)
