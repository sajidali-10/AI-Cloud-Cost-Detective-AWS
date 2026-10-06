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

Phase 5A adds the ``app_users`` table backing local application
authentication and RBAC.  The schema is created and migrated by
``backend/alembic/versions/0002_app_users.py``.

Phase 5B adds the ``conversations`` and ``conversation_messages``
tables backing durable, user-owned AI conversation history.  The
schema is created and migrated by
``backend/alembic/versions/0003_conversations.py``.  Conversation
history is non-authoritative: Phase 1–3 evidence remains the only
trusted AWS source.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
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


# Phase 5A — application users (local authentication + RBAC).
#
# The role is constrained at the database level by a CHECK so a
# programming bug cannot insert an unrecognised value.  Email is
# stored normalised (lowercased, trimmed) by the application layer
# before insertion; we still keep a UNIQUE constraint as a belt-and-
# braces invariant.  Password hashes are NEVER read back into the
# API surface — see :mod:`app.schemas.auth`.
APP_USER_ROLES: tuple[str, ...] = ("ADMIN", "ANALYST", "VIEWER")


class AppUser(Base):
    """Application user identity for local authentication + RBAC.

    The model is intentionally minimal: it stores enough to
    authenticate, authorise, and audit a login.  It deliberately does
    NOT store personal profile fields, profile pictures, SSO
    identities, MFA secrets, refresh tokens, or conversation history —
    those belong to later phases (5B/5C) and are out of scope for the
    Phase 5A authentication foundation.
    """

    __tablename__ = "app_users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_app_users_email"),
        CheckConstraint(
            "role IN ('ADMIN', 'ANALYST', 'VIEWER')",
            name="ck_app_users_role",
        ),
        Index("ix_app_users_role", "role"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # RFC 5321 caps local + domain at 320 chars; we never accept
    # anything longer so the column can be UNIQUE-indexed cheaply.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Argon2id encoded hash.  Typical PHC string:
    # ``$argon2id$v=19$m=65536,t=3,p=2$<salt>$<hash>``.  Always treat
    # as opaque — the only consumer is :class:`PasswordHasher`.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        # Deliberately omits password_hash so a stray ``print(user)``
        # in a debugger cannot leak the secret.
        return (
            f"AppUser(id={self.id!r}, email={self.email!r}, "
            f"role={self.role!r}, is_active={self.is_active!r})"
        )


# Phase 5B — conversations + AI message history.
#
# Design principles (mirrored from the Phase 5A block above):
#
#   * BigInt primary keys via ``BigIntPK`` so the SQLite unit tests
#     stay compatible with the Postgres production DSN.
#   * UTC datetimes + ``server_default=func.now()`` so a
#     misconfigured application clock cannot poison ordering.
#   * The dialect-aware ``_JSONBType`` for metadata columns.
#   * FKs declared with ``ON DELETE CASCADE`` so deleting a user
#     (or a conversation) is hard-delete with a deterministic
#     cascade — no orphan rows.
#   * CHECK constraints at the database level so a programming bug
#     cannot insert an unrecognised role.
#
# Conversation storage is private.  Every service-layer query is
# scoped by ``user_id``; ownership is part of the WHERE clause, not
# a post-fetch check.  Phase 4 authoritative evidence is preserved
# by the AI service layer; the message table is for *history*, not
# for evidence.

# Controlled role set for ``conversation_messages.role``.
CONVERSATION_MESSAGE_ROLES: tuple[str, ...] = ("USER", "ASSISTANT", "SYSTEM_EVENT")

# Bounded default title — the API layer rejects empty / oversized
# titles before they ever reach the ORM.
DEFAULT_CONVERSATION_TITLE: str = "New Cost Analysis"


class Conversation(Base):
    """A single AI Cost Analyst conversation owned by one user.

    Ownership is enforced at the service layer through every query
    (``WHERE id = :cid AND user_id = :uid``).  This table never
    carries messages — those live in :class:`ConversationMessage`
    so listing a conversation does not force a join.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "length(title) > 0 AND length(title) <= 200",
            name="ck_conversations_title_length",
        ),
        # Owner-scope index: every list/get-by-id query is keyed by
        # ``user_id``; an index on (user_id) is the minimum.
        Index("ix_conversations_user_id", "user_id"),
        # (user_id, updated_at DESC) supports the default
        # "newest/most-recently-active first" list ordering.
        Index(
            "ix_conversations_user_updated",
            "user_id", "updated_at",
        ),
        # (user_id, is_archived, last_message_at DESC) supports the
        # ``archived`` filter + activity sort.
        Index(
            "ix_conversations_user_archived_lastmsg",
            "user_id", "is_archived", "last_message_at",
        ),
        # FK declared inline so the table-level constraint shows up
        # in pg_constraint.  CASCADE keeps ownership deletion safe.
        ForeignKeyConstraint(
            ["user_id"],
            ["app_users.id"],
            name="fk_conversations_user_id",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=DEFAULT_CONVERSATION_TITLE
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    def touch(self, *, message_at: datetime | None = None) -> None:
        """Bump ``updated_at`` (and optionally ``last_message_at``).

        Called by the service layer after a successful message
        insert.  Uses a UTC ``now()`` so application clocks cannot
        skew the timestamp.
        """
        now = datetime.now(timezone.utc)
        self.updated_at = now
        if message_at is not None:
            self.last_message_at = message_at

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        return (
            f"Conversation(id={self.id!r}, user_id={self.user_id!r}, "
            f"title={self.title!r}, is_archived={self.is_archived!r})"
        )


class ConversationMessage(Base):
    """A single message inside a conversation.

    Role is constrained to ``USER``, ``ASSISTANT`` or
    ``SYSTEM_EVENT``.  ``SYSTEM_EVENT`` rows are created by the
    service layer for safe failure metadata (a sanitized
    ``error_code``); they are NEVER used to push arbitrary system
    instructions into the model's history — they are excluded from
    history rendering.

    Metadata columns are intentionally narrow JSONB blobs.  We do
    NOT store the raw LiteLLM response, the Authorization header,
    or any credential material.  The application layer validates
    the shape before insertion.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('USER', 'ASSISTANT', 'SYSTEM_EVENT')",
            name="ck_conversation_messages_role",
        ),
        Index(
            "ix_conversation_messages_conv_created",
            "conversation_id", "created_at",
        ),
        Index(
            "ix_conversation_messages_conv_role",
            "conversation_id", "role",
        ),
        ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_messages_conv_id",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Optional provenance for ASSISTANT rows.  All nullable; the
    # service layer only writes these for assistant messages.
    operation_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_alias: Mapped[str | None] = mapped_column(String(64), nullable=True)

    grounding_metadata: Mapped[dict | None] = mapped_column(_JSONBType, nullable=True)
    evidence_references: Mapped[list | None] = mapped_column(_JSONBType, nullable=True)
    warnings: Mapped[list | None] = mapped_column(_JSONBType, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(_JSONBType, nullable=True)

    # Sanitized error code for SYSTEM_EVENT rows (e.g. ``LITELLM_TIMEOUT``).
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover — debug helper
        return (
            f"ConversationMessage(id={self.id!r}, conversation_id={self.conversation_id!r}, "
            f"role={self.role!r})"
        )


__all__ = [
    "APP_USER_ROLES",
    "CONVERSATION_MESSAGE_ROLES",
    "Base",
    "Conversation",
    "ConversationMessage",
    "CostCache",
    "AppUser",
    "DEFAULT_CONVERSATION_TITLE",
    "Decimal",
]
