"""Unit tests for the Phase 5B ConversationService.

Uses in-memory SQLite via a custom SQLAlchemy session and the
production ORM.  Tests cover:

* CRUD happy paths.
* Ownership scoping (the IDOR defence).
* Title validation.
* Pagination.
* Message ordering (oldest first) and bounded history.
* Failure / sanitized metadata paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base
from app.services.conversation_service import (
    ConversationError,
    ConversationNotFound,
    ConversationService,
    HistoryTurn,
    InvalidConversationTitle,
    InvalidMessageContent,
    InvalidMessageRole,
    InvalidPagination,
    render_history_block,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def service(session):
    return ConversationService(db=session)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_uses_default_title_when_none(service: ConversationService) -> None:
    conv = service.create(user_id=1, title=None)
    assert conv.id is not None
    assert conv.title == "New Cost Analysis"
    assert conv.user_id == 1
    assert conv.is_archived is False


def test_create_uses_default_title_when_blank(service: ConversationService) -> None:
    conv = service.create(user_id=1, title="   ")
    assert conv.title == "New Cost Analysis"


def test_create_strips_and_keeps_title(service: ConversationService) -> None:
    conv = service.create(user_id=1, title="   Q4 cost review   ")
    assert conv.title == "Q4 cost review"


def test_create_rejects_oversized_title(service: ConversationService) -> None:
    with pytest.raises(InvalidConversationTitle):
        service.create(user_id=1, title="x" * 500)


def test_create_rejects_negative_or_zero_user_id(service: ConversationService) -> None:
    with pytest.raises(ValueError):
        service.create(user_id=0)
    with pytest.raises(ValueError):
        service.create(user_id=-1)


def test_list_filters_to_user(service: ConversationService) -> None:
    a1 = service.create(user_id=1, title="A1")
    a2 = service.create(user_id=1, title="A2")
    b1 = service.create(user_id=2, title="B1")
    rows, count = service.list_for_user(user_id=1, limit=50, offset=0)
    assert count == 2
    ids = {c.id for c in rows}
    assert ids == {a1.id, a2.id}
    rows_b, _ = service.list_for_user(user_id=2, limit=50, offset=0)
    assert [c.id for c in rows_b] == [b1.id]


def test_list_archived_filter(service: ConversationService) -> None:
    c1 = service.create(user_id=1, title="active")
    c2 = service.create(user_id=1, title="archived")
    service.set_archived(user_id=1, conversation_id=c2.id, archived=True)
    active, _ = service.list_for_user(user_id=1, archived=False)
    archived, _ = service.list_for_user(user_id=1, archived=True)
    assert {c.id for c in active} == {c1.id}
    assert {c.id for c in archived} == {c2.id}


def test_list_pagination(service: ConversationService) -> None:
    for i in range(5):
        service.create(user_id=1, title=f"c{i}")
    rows, count = service.list_for_user(user_id=1, limit=2, offset=0)
    assert count == 2
    assert len(rows) == 2
    rows2, _ = service.list_for_user(user_id=1, limit=2, offset=2)
    assert len(rows2) == 2
    # No overlap.
    assert {c.id for c in rows}.isdisjoint({c.id for c in rows2})


def test_list_invalid_pagination(service: ConversationService) -> None:
    with pytest.raises(InvalidPagination):
        service.list_for_user(user_id=1, limit=0, offset=0)
    with pytest.raises(InvalidPagination):
        service.list_for_user(user_id=1, limit=-1, offset=0)
    with pytest.raises(InvalidPagination):
        service.list_for_user(user_id=1, limit=2, offset=-1)
    with pytest.raises(InvalidPagination):
        service.list_for_user(user_id=1, limit=10_000, offset=0)


def test_rename(service: ConversationService) -> None:
    conv = service.create(user_id=1, title="old")
    updated = service.rename(user_id=1, conversation_id=conv.id, title="new")
    assert updated.title == "new"


def test_rename_rejects_empty(service: ConversationService) -> None:
    conv = service.create(user_id=1, title="ok")
    with pytest.raises(InvalidConversationTitle):
        service.rename(user_id=1, conversation_id=conv.id, title="   ")


def test_archive_unarchive(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    archived = service.set_archived(user_id=1, conversation_id=conv.id, archived=True)
    assert archived.is_archived is True
    back = service.set_archived(user_id=1, conversation_id=conv.id, archived=False)
    assert back.is_archived is False


# ---------------------------------------------------------------------------
# Ownership / IDOR
# ---------------------------------------------------------------------------


def test_get_other_users_conversation_raises_not_found(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1, title="only mine")
    with pytest.raises(ConversationNotFound):
        service.get(user_id=2, conversation_id=conv.id)


def test_rename_other_users_conversation_denied(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1, title="mine")
    with pytest.raises(ConversationNotFound):
        service.rename(user_id=2, conversation_id=conv.id, title="hack")


def test_delete_other_users_conversation_denied(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1, title="mine")
    with pytest.raises(ConversationNotFound):
        service.delete(user_id=2, conversation_id=conv.id)
    # The conversation still exists for the legitimate owner.
    assert service.get(user_id=1, conversation_id=conv.id).id == conv.id


def test_get_returns_owned_conversation(service: ConversationService) -> None:
    conv = service.create(user_id=1, title="mine")
    got = service.get(user_id=1, conversation_id=conv.id)
    assert got.id == conv.id


def test_delete_owned_conversation_removes_it(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    service.delete(user_id=1, conversation_id=conv.id)
    with pytest.raises(ConversationNotFound):
        service.get(user_id=1, conversation_id=conv.id)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_add_user_message_bumps_activity_timestamp(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    before = conv.updated_at
    msg = service.add_user_message(
        user_id=1, conversation_id=conv.id, content="hello"
    )
    assert msg.role == "USER"
    assert msg.content == "hello"
    # Re-read conversation and check timestamp moved.
    fresh = service.get(user_id=1, conversation_id=conv.id)
    assert fresh.updated_at >= before
    assert fresh.last_message_at is not None


def test_add_user_message_rejects_empty_content(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    with pytest.raises(InvalidMessageContent):
        service.add_user_message(
            user_id=1, conversation_id=conv.id, content="   "
        )


def test_add_assistant_message_persists_provenance(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    msg = service.add_assistant_message(
        user_id=1,
        conversation_id=conv.id,
        content="answer text",
        operation_type="analyze",
        model_alias="cost-detective-free",
        grounding_metadata={
            "account_id": "111111111111",
            "region": "us-east-1",
            "days": 30,
            "cost_evidence_used": True,
            "recommendations_used": 3,
            "capabilities_used": True,
        },
        evidence_references=[{"type": "recommendation", "id": "rec-1"}],
        warnings=["ok"],
        token_usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    assert msg.role == "ASSISTANT"
    assert msg.operation_type == "analyze"
    assert msg.model_alias == "cost-detective-free"
    assert msg.grounding_metadata["account_id"] == "111111111111"
    assert msg.evidence_references[0]["id"] == "rec-1"


def test_assistant_message_rejects_non_json_metadata(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    # Object values are not JSON-serialisable, so the service must
    # refuse rather than coerce.
    with pytest.raises(ValueError):
        service.add_assistant_message(
            user_id=1,
            conversation_id=conv.id,
            content="ok",
            operation_type="analyze",
            model_alias=None,
            grounding_metadata={"bad": object()},
            evidence_references=None,
            warnings=None,
            token_usage=None,
        )


def test_add_system_event(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    msg = service.add_system_event(
        user_id=1, conversation_id=conv.id, code="LITELLM_TIMEOUT"
    )
    assert msg.role == "SYSTEM_EVENT"
    assert msg.error_code == "LITELLM_TIMEOUT"
    assert "LITELLM_TIMEOUT" in msg.content


def test_add_system_event_rejects_blank_code(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    with pytest.raises(InvalidMessageRole):
        service.add_system_event(user_id=1, conversation_id=conv.id, code="   ")


def test_list_messages_orders_oldest_first(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    a = service.add_user_message(user_id=1, conversation_id=conv.id, content="a")
    b = service.add_assistant_message(
        user_id=1,
        conversation_id=conv.id,
        content="b",
        operation_type="analyze",
        model_alias=None,
        grounding_metadata=None,
        evidence_references=None,
        warnings=None,
        token_usage=None,
    )
    c = service.add_user_message(user_id=1, conversation_id=conv.id, content="c")
    rows, count, has_more = service.list_messages(
        user_id=1, conversation_id=conv.id, limit=10, offset=0
    )
    assert count == 3
    assert has_more is False
    assert [m.content for m in rows] == ["a", "b", "c"]
    assert [m.id for m in rows] == [a.id, b.id, c.id]


def test_list_messages_pagination(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    for i in range(5):
        service.add_user_message(
            user_id=1, conversation_id=conv.id, content=f"m{i}"
        )
    rows, count, has_more = service.list_messages(
        user_id=1, conversation_id=conv.id, limit=2, offset=0
    )
    assert count == 2
    assert has_more is True
    assert [m.content for m in rows] == ["m0", "m1"]
    rows2, _, has_more2 = service.list_messages(
        user_id=1, conversation_id=conv.id, limit=2, offset=2
    )
    assert [m.content for m in rows2] == ["m2", "m3"]
    assert has_more2 is True
    rows3, _, has_more3 = service.list_messages(
        user_id=1, conversation_id=conv.id, limit=2, offset=4
    )
    assert [m.content for m in rows3] == ["m4"]
    assert has_more3 is False


def test_list_messages_other_user_conversation_denied(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    service.add_user_message(user_id=1, conversation_id=conv.id, content="mine")
    with pytest.raises(ConversationNotFound):
        service.list_messages(
            user_id=2, conversation_id=conv.id, limit=10, offset=0
        )


def test_count_messages(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    assert service.count_messages(user_id=1, conversation_id=conv.id) == 0
    service.add_user_message(user_id=1, conversation_id=conv.id, content="x")
    service.add_user_message(user_id=1, conversation_id=conv.id, content="y")
    assert service.count_messages(user_id=1, conversation_id=conv.id) == 2


# ---------------------------------------------------------------------------
# Bounded history
# ---------------------------------------------------------------------------


def test_recent_history_chronological_and_user_assistant_only(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    service.add_user_message(user_id=1, conversation_id=conv.id, content="u1")
    service.add_assistant_message(
        user_id=1, conversation_id=conv.id, content="a1",
        operation_type="analyze", model_alias=None,
        grounding_metadata=None, evidence_references=None,
        warnings=None, token_usage=None,
    )
    service.add_user_message(user_id=1, conversation_id=conv.id, content="u2")
    service.add_system_event(user_id=1, conversation_id=conv.id, code="X")
    service.add_assistant_message(
        user_id=1, conversation_id=conv.id, content="a2",
        operation_type="analyze", model_alias=None,
        grounding_metadata=None, evidence_references=None,
        warnings=None, token_usage=None,
    )
    turns = service.get_recent_messages(
        user_id=1, conversation_id=conv.id, max_messages=10, max_chars=10_000
    )
    roles = [t.role for t in turns]
    # SYSTEM_EVENT must never appear.
    assert "SYSTEM_EVENT" not in roles
    # Oldest-first ordering.
    assert roles == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
    contents = [t.content for t in turns]
    assert contents == ["u1", "a1", "u2", "a2"]


def test_recent_history_message_budget(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    for i in range(7):
        service.add_user_message(
            user_id=1, conversation_id=conv.id, content=f"u{i}"
        )
    turns = service.get_recent_messages(
        user_id=1, conversation_id=conv.id, max_messages=3, max_chars=10_000
    )
    assert len(turns) == 3
    # Returns the most recent 3 in chronological order.
    assert [t.content for t in turns] == ["u4", "u5", "u6"]


def test_recent_history_char_budget_trims_oldest(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    for i in range(6):
        service.add_user_message(
            user_id=1, conversation_id=conv.id, content="x" * 100  # 100 chars each
        )
    turns = service.get_recent_messages(
        user_id=1, conversation_id=conv.id, max_messages=10, max_chars=250
    )
    # 250 chars fits 2 full messages + a third truncated would
    # exceed; the service keeps at least one then stops.
    assert len(turns) == 2
    assert all(t.content == "x" * 100 for t in turns)


def test_recent_history_empty_budget_returns_empty(
    service: ConversationService,
) -> None:
    conv = service.create(user_id=1)
    service.add_user_message(user_id=1, conversation_id=conv.id, content="x")
    assert (
        service.get_recent_messages(
            user_id=1, conversation_id=conv.id, max_messages=0, max_chars=10_000
        )
        == []
    )
    assert (
        service.get_recent_messages(
            user_id=1, conversation_id=conv.id, max_messages=10, max_chars=0
        )
        == []
    )


def test_recent_history_other_user_denied(service: ConversationService) -> None:
    conv = service.create(user_id=1)
    service.add_user_message(user_id=1, conversation_id=conv.id, content="mine")
    with pytest.raises(ConversationNotFound):
        service.get_recent_messages(
            user_id=2, conversation_id=conv.id, max_messages=5, max_chars=1000
        )


# ---------------------------------------------------------------------------
# History renderer
# ---------------------------------------------------------------------------


def test_render_history_block_empty() -> None:
    assert render_history_block([]) == ""


def test_render_history_block_wraps_in_delimiters() -> None:
    now = datetime.now(timezone.utc)
    text = render_history_block(
        [
            HistoryTurn(role="USER", content="hello", created_at=now),
            HistoryTurn(role="ASSISTANT", content="hi", created_at=now),
        ]
    )
    assert "<conversation_history>" in text
    assert "</conversation_history>" in text
    assert "USER" in text and "ASSISTANT" in text
    assert "hello" in text and "hi" in text


def test_render_history_block_indents_multiline_content() -> None:
    now = datetime.now(timezone.utc)
    text = render_history_block(
        [
            HistoryTurn(
                role="USER",
                content="line one\nline two",
                created_at=now,
            )
        ]
    )
    assert "  line one" in text
    assert "  line two" in text
