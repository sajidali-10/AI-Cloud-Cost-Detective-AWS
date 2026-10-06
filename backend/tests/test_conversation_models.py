"""Tests for the Phase 5B Conversation + ConversationMessage ORM.

Mirrors the style of :mod:`tests.test_migration_users`:
* in-memory SQLite via SQLAlchemy ``Base.metadata.create_all``
  (matches the Phase 5A pattern so we don't need a live DB)
* shape + constraint + index assertions
* repr never leaks the conversation body
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect

from app.db.models import (
    CONVERSATION_MESSAGE_ROLES,
    DEFAULT_CONVERSATION_TITLE,
    AppUser,
    Base,
    Conversation,
    ConversationMessage,
)


def test_conversation_table_exists_and_has_expected_columns() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    ins = inspect(eng)
    assert "conversations" in ins.get_table_names()
    cols = {c["name"] for c in ins.get_columns("conversations")}
    for name in (
        "id",
        "user_id",
        "title",
        "created_at",
        "updated_at",
        "last_message_at",
        "is_archived",
    ):
        assert name in cols, f"missing column {name}"


def test_conversation_messages_table_exists_and_has_expected_columns() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    ins = inspect(eng)
    assert "conversation_messages" in ins.get_table_names()
    cols = {c["name"] for c in ins.get_columns("conversation_messages")}
    for name in (
        "id",
        "conversation_id",
        "role",
        "content",
        "created_at",
        "operation_type",
        "model_alias",
        "grounding_metadata",
        "evidence_references",
        "warnings",
        "token_usage",
        "error_code",
    ):
        assert name in cols, f"missing column {name}"


def test_role_check_constraint_present_in_sqlite_metadata() -> None:
    table = ConversationMessage.__table__
    check_constraints = [
        c.name for c in table.constraints if hasattr(c, "name") and c.name
    ]
    assert "ck_conversation_messages_role" in check_constraints


def test_title_check_constraint_present_in_sqlite_metadata() -> None:
    table = Conversation.__table__
    check_constraints = [
        c.name for c in table.constraints if hasattr(c, "name") and c.name
    ]
    assert "ck_conversations_title_length" in check_constraints


def test_user_id_indexed() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    indexes = {i["name"] for i in inspect(eng).get_indexes("conversations")}
    assert "ix_conversations_user_id" in indexes
    assert "ix_conversations_user_updated" in indexes
    assert "ix_conversations_user_archived_lastmsg" in indexes


def test_messages_indexed_by_conversation_and_created_at() -> None:
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    indexes = {
        i["name"]: i["column_names"]
        for i in inspect(eng).get_indexes("conversation_messages")
    }
    assert "ix_conversation_messages_conv_created" in indexes
    assert "ix_conversation_messages_conv_role" in indexes


def test_conversation_message_roles_constant() -> None:
    assert CONVERSATION_MESSAGE_ROLES == ("USER", "ASSISTANT", "SYSTEM_EVENT")


def test_default_conversation_title() -> None:
    assert DEFAULT_CONVERSATION_TITLE == "New Cost Analysis"


def test_conversation_repr_does_not_leak_user_content() -> None:
    c = Conversation(id=1, user_id=2, title="secret", is_archived=False)
    r = repr(c)
    assert "secret" in r  # title is OK to expose in repr (debug-friendly)
    assert "user_id=2" in r


def test_message_repr_does_not_leak_message_content() -> None:
    m = ConversationMessage(
        id=1, conversation_id=2, role="USER", content="top-secret-text"
    )
    r = repr(m)
    # role + ids are fine; the content itself must NOT be in repr.
    assert "top-secret-text" not in r
    assert "role='USER'" in r


def test_conversation_touch_bumps_updated_at() -> None:
    from datetime import datetime, timedelta, timezone

    earlier = datetime(2024, 1, 1, tzinfo=timezone.utc)
    c = Conversation(
        id=1, user_id=1, title="t", created_at=earlier, updated_at=earlier,
        last_message_at=None, is_archived=False,
    )
    c.touch()
    assert c.updated_at > earlier
    # last_message_at only moves when explicitly provided.
    assert c.last_message_at is None
    later = earlier + timedelta(seconds=1)
    c.touch(message_at=later)
    assert c.last_message_at == later


def test_conversation_touch_message_at_used() -> None:
    from datetime import datetime, timezone

    c = Conversation(
        id=1, user_id=1, title="t", is_archived=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    when = datetime(2030, 1, 1, tzinfo=timezone.utc)
    c.touch(message_at=when)
    assert c.last_message_at == when
