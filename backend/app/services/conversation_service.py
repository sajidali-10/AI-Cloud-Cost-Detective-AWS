"""Conversation + AI message-history service — Phase 5B.

Responsibilities:

* Create / list / read / rename / archive / delete conversations.
* Persist USER, ASSISTANT and SYSTEM_EVENT messages.
* Enforce per-user ownership at the SQL layer (``WHERE user_id =
  :uid``) — never as a post-fetch check.  A conversation that
  does not exist OR is owned by another user produces the same
  :class:`ConversationNotFound` so a caller cannot enumerate IDs.
* Return bounded, deterministic recent-turn history for the AI
  service to attach to a LiteLLM call.
* Never persist raw LiteLLM payloads, Authorization headers, JWTs,
  AWS credentials, or chain-of-thought.  The
  :meth:`add_assistant_message` helper accepts only the safe
  provenance fields Phase 4 already exposes.

The service is intentionally side-effect-light: every public method
takes a ``Session`` and is responsible for its own transactions.
Callers (route layer) share the same ``Session`` per request so a
single user/assistant message pair lands in one transaction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    CONVERSATION_MESSAGE_ROLES,
    DEFAULT_CONVERSATION_TITLE,
    AppUser,
    Conversation,
    ConversationMessage,
)
from app.services.ai_system_prompt import history_block

logger = logging.getLogger("cost-detective-backend.conversation_service")


# ---------------------------------------------------------------------------
# History turn dataclass + renderer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryTurn:
    """One prior conversation turn (USER or ASSISTANT) to feed the model.

    SYSTEM_EVENT turns are NEVER returned by
    :meth:`ConversationService.get_recent_messages` — they are
    operational metadata, not dialogue, and would only confuse the
    model.
    """

    role: str
    content: str
    created_at: datetime


def render_history_block(turns: Sequence[HistoryTurn]) -> str:
    """Render a sequence of turns into the bounded history text.

    Returns an EMPTY string when ``turns`` is empty so callers can
    pass the result directly into the AI service.  When non-empty,
    the result is wrapped in the ``<conversation_history>``
    delimiters via :func:`app.services.ai_system_prompt.history_block`.
    """
    if not turns:
        return ""
    lines: List[str] = []
    for i, turn in enumerate(turns):
        if i > 0:
            lines.append("")
        # The role is constrained to USER/ASSISTANT by the caller,
        # but we still uppercase defensively in case the DB rows
        # ever drift from the canonical literal.
        role = (turn.role or "").upper()
        lines.append(f"turn {i + 1} ({role}):")
        # Indent multi-line content so the model can tell where
        # each turn ends.
        content = (turn.content or "").rstrip()
        for line in content.splitlines() or [""]:
            lines.append(f"  {line}")
    body = "\n".join(lines).rstrip()
    return history_block(body)


# ---------------------------------------------------------------------------
# Typed exceptions.  All sanitized — never include credentials, IDs
# from other users, or raw payloads.
# ---------------------------------------------------------------------------


class ConversationError(Exception):
    """Base class for service-layer conversation errors."""


class ConversationNotFound(ConversationError):
    """The conversation does not exist OR is owned by another user.

    Both cases collapse to the same exception class with the same
    sanitized message so a caller cannot enumerate IDs by
    distinguishing "not found" from "not yours".
    """


class InvalidConversationTitle(ConversationError):
    """Title was empty / oversized / non-string."""


class InvalidMessageRole(ConversationError):
    """Role was outside the controlled set."""


class InvalidMessageContent(ConversationError):
    """Message content was empty or not a string."""


class InvalidPagination(ConversationError):
    """Pagination parameters were invalid (limit/offset out of range)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_user_id(user_id: int) -> int:
    """Reject non-positive / non-int user IDs.

    The synthetic-admin user used when AUTH_ENABLED=false has id=-1
    and is rejected here so durable state never attaches to it.
    """
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("user_id must be an int")
    if user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    return user_id


def _validate_title(
    title: Optional[str],
    *,
    settings: Settings,
    allow_blank: bool = True,
) -> str:
    """Return a clean title or raise :class:`InvalidConversationTitle`.

    When ``allow_blank`` is True (the create flow), ``None`` /
    blank titles fall back to the default title.  When False
    (the rename flow), blank titles are rejected — the caller
    explicitly asked for a new title and we should never silently
    swap in the default.

    Empty / non-string values raise
    :class:`InvalidConversationTitle` in both modes; oversized
    titles always raise.
    """
    if not isinstance(title, str):
        raise InvalidConversationTitle("title must be a string")
    cleaned = title.strip()
    if not cleaned:
        if allow_blank:
            return DEFAULT_CONVERSATION_TITLE
        raise InvalidConversationTitle(
            "title must be non-empty after trimming"
        )
    if len(cleaned) > settings.conversation_title_max_length:
        raise InvalidConversationTitle(
            f"title length {len(cleaned)} exceeds the maximum of "
            f"{settings.conversation_title_max_length} characters."
        )
    return cleaned


def _validate_role(role: str) -> str:
    if role not in CONVERSATION_MESSAGE_ROLES:
        raise InvalidMessageRole(
            f"role must be one of {list(CONVERSATION_MESSAGE_ROLES)}; "
            f"got {role!r}"
        )
    return role


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise InvalidMessageContent("content must be a string")
    cleaned = content.strip()
    if not cleaned:
        raise InvalidMessageContent("content must be non-empty after trimming")
    return cleaned


def _validate_limit_offset(
    *,
    limit: int,
    offset: int,
    max_limit: int,
) -> Tuple[int, int]:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise InvalidPagination("limit must be an int")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise InvalidPagination("offset must be an int")
    if limit < 1 or limit > max_limit:
        raise InvalidPagination(
            f"limit must be in [1, {max_limit}]; got {limit}"
        )
    if offset < 0:
        raise InvalidPagination("offset must be >= 0")
    return limit, offset


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ConversationService:
    """Domain layer for conversations + AI message history."""

    def __init__(
        self,
        *,
        db: Session,
        settings: Optional[Settings] = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()

    # ----- ownership-scoped query helpers --------------------------------

    def _get_owned_conversation(
        self, *, user_id: int, conversation_id: int
    ) -> Conversation:
        """Return the conversation owned by ``user_id`` or raise.

        The WHERE clause includes both ``id`` and ``user_id`` so a
        caller cannot observe another user's row by ID guessing.
        """
        _validate_user_id(user_id)
        if not isinstance(conversation_id, int) or isinstance(conversation_id, bool):
            raise ConversationNotFound("conversation not found")
        stmt = select(Conversation).where(
            and_(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        conv = self._db.execute(stmt).scalar_one_or_none()
        if conv is None:
            logger.info(
                "conversation.access_denied user_id=%s conversation_id=%s",
                user_id, conversation_id,
            )
            raise ConversationNotFound("conversation not found")
        return conv

    # ----- CRUD ---------------------------------------------------------

    def create(self, *, user_id: int, title: Optional[str] = None) -> Conversation:
        """Create a new conversation for ``user_id``.

        Title validation lives here (not in the ORM) so empty
        values fall back to the default rather than failing the
        DB CHECK constraint.
        """
        uid = _validate_user_id(user_id)
        if title is None:
            clean_title = DEFAULT_CONVERSATION_TITLE
        else:
            clean_title = _validate_title(title, settings=self._settings)
        now = datetime.now(timezone.utc)
        conv = Conversation(
            user_id=uid,
            title=clean_title,
            is_archived=False,
            created_at=now,
            updated_at=now,
            last_message_at=None,
        )
        self._db.add(conv)
        self._db.commit()
        self._db.refresh(conv)
        logger.info(
            "conversation.create user_id=%s conversation_id=%s title_len=%s",
            uid, conv.id, len(clean_title),
        )
        return conv

    def list_for_user(
        self,
        *,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
        archived: Optional[bool] = None,
    ) -> Tuple[List[Conversation], int]:
        """Return a page of the user's conversations.

        Ordered by ``updated_at DESC`` (most recently active
        first).  When ``archived`` is provided the filter is
        applied; otherwise both archived and active are returned.

        ``limit`` is clamped to ``conversation_list_max_limit``.
        """
        uid = _validate_user_id(user_id)
        lim, off = _validate_limit_offset(
            limit=limit,
            offset=offset,
            max_limit=self._settings.conversation_list_max_limit,
        )

        base = select(Conversation).where(Conversation.user_id == uid)
        if archived is not None:
            base = base.where(Conversation.is_archived == bool(archived))
        page_stmt = (
            base.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(lim)
            .offset(off)
        )
        rows = list(self._db.execute(page_stmt).scalars())
        return rows, len(rows)

    def get(self, *, user_id: int, conversation_id: int) -> Conversation:
        return self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )

    def rename(
        self, *, user_id: int, conversation_id: int, title: str
    ) -> Conversation:
        """Rename a conversation.

        Empty title after stripping raises
        :class:`InvalidConversationTitle` (the API layer is also
        supposed to reject empty strings before they reach here,
        but this is the last line of defence).
        """
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        conv.title = _validate_title(
            title, settings=self._settings, allow_blank=False
        )
        conv.updated_at = datetime.now(timezone.utc)
        self._db.add(conv)
        self._db.commit()
        self._db.refresh(conv)
        logger.info(
            "conversation.rename user_id=%s conversation_id=%s title_len=%s",
            user_id, conversation_id, len(conv.title),
        )
        return conv

    def set_archived(
        self,
        *,
        user_id: int,
        conversation_id: int,
        archived: bool,
    ) -> Conversation:
        """Set the ``is_archived`` flag.  ``archived`` is normalised to bool."""
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        conv.is_archived = bool(archived)
        conv.updated_at = datetime.now(timezone.utc)
        self._db.add(conv)
        self._db.commit()
        self._db.refresh(conv)
        logger.info(
            "conversation.archive user_id=%s conversation_id=%s archived=%s",
            user_id, conversation_id, conv.is_archived,
        )
        return conv

    def delete(self, *, user_id: int, conversation_id: int) -> None:
        """Hard-delete the conversation (and its messages via CASCADE).

        Uses ``delete()`` against the same ownership-scoped query,
        so a non-owner cannot delete even by guessing the id.
        """
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        self._db.delete(conv)
        try:
            self._db.commit()
        except SQLAlchemyError as exc:
            self._db.rollback()
            logger.warning(
                "conversation.delete_failed user_id=%s conversation_id=%s "
                "error=%s",
                user_id, conversation_id, type(exc).__name__,
            )
            raise
        logger.info(
            "conversation.delete user_id=%s conversation_id=%s",
            user_id, conversation_id,
        )

    def count_messages(
        self, *, user_id: int, conversation_id: int
    ) -> int:
        """Return the message count for a conversation (ownership-scoped).

        Used by the detail endpoint to populate ``message_count``
        without a separate list call.
        """
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        stmt = (
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv.id)
        )
        return int(self._db.execute(stmt).scalar_one() or 0)

    # ----- message insertion -------------------------------------------

    def add_user_message(
        self,
        *,
        user_id: int,
        conversation_id: int,
        content: str,
    ) -> ConversationMessage:
        """Persist a USER message; bump conversation activity timestamp.

        Returns the persisted message.  Triggers ``updated_at`` and
        ``last_message_at`` on the conversation row.
        """
        return self._insert_message(
            user_id=user_id,
            conversation_id=conversation_id,
            role="USER",
            content=content,
            operation_type=None,
            model_alias=None,
            grounding_metadata=None,
            evidence_references=None,
            warnings=None,
            token_usage=None,
            error_code=None,
        )

    def add_assistant_message(
        self,
        *,
        user_id: int,
        conversation_id: int,
        content: str,
        operation_type: str,
        model_alias: Optional[str],
        grounding_metadata: Optional[Dict[str, Any]],
        evidence_references: Optional[List[Dict[str, Any]]],
        warnings: Optional[List[str]],
        token_usage: Optional[Dict[str, Any]],
    ) -> ConversationMessage:
        """Persist an ASSISTANT message with safe provenance.

        ``content`` MUST be the model's actual output (or, on a
        controlled failure path, an empty string — the caller
        should switch to :meth:`add_system_event` instead).
        ``model_alias`` is the model identifier; the service layer
        stores it as-is.  ``grounding_metadata``, ``evidence_references``,
        ``warnings``, ``token_usage`` must be JSON-serialisable
        dicts/lists; the service layer validates shape via
        ``_safe_json``.
        """
        _validate_role("ASSISTANT")
        return self._insert_message(
            user_id=user_id,
            conversation_id=conversation_id,
            role="ASSISTANT",
            content=content,
            operation_type=operation_type,
            model_alias=model_alias,
            grounding_metadata=_safe_json(grounding_metadata, name="grounding_metadata"),
            evidence_references=_safe_json(evidence_references, name="evidence_references"),
            warnings=_safe_json(warnings, name="warnings"),
            token_usage=_safe_json(token_usage, name="token_usage"),
            error_code=None,
        )

    def add_system_event(
        self,
        *,
        user_id: int,
        conversation_id: int,
        code: str,
    ) -> ConversationMessage:
        """Persist a SYSTEM_EVENT row with a sanitized error code.

        ``code`` is a short stable identifier (e.g. ``LITELLM_TIMEOUT``)
        — NEVER a raw provider message.  ``content`` is set to a
        short, sanitized prefix so the operator looking at the
        conversation can see *what* happened without dumping the
        provider payload.
        """
        if not isinstance(code, str) or not code.strip():
            raise InvalidMessageRole("system_event code must be a non-empty string")
        # Hard cap so a giant code cannot blow the column width.
        code = code.strip()[:64]
        return self._insert_message(
            user_id=user_id,
            conversation_id=conversation_id,
            role="SYSTEM_EVENT",
            content=f"system_event:{code}",
            operation_type="system_event",
            model_alias=None,
            grounding_metadata=None,
            evidence_references=None,
            warnings=None,
            token_usage=None,
            error_code=code,
        )

    def _insert_message(
        self,
        *,
        user_id: int,
        conversation_id: int,
        role: str,
        content: str,
        operation_type: Optional[str],
        model_alias: Optional[str],
        grounding_metadata: Optional[Dict[str, Any]],
        evidence_references: Optional[List[Dict[str, Any]]],
        warnings: Optional[List[str]],
        token_usage: Optional[Dict[str, Any]],
        error_code: Optional[str],
    ) -> ConversationMessage:
        """Insert a message row + bump the conversation timestamp.

        This is the ONLY write path into ``conversation_messages``.
        Ownership is enforced via :meth:`_get_owned_conversation`.
        """
        _validate_role(role)
        cleaned = _validate_content(content)
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        now = datetime.now(timezone.utc)
        msg = ConversationMessage(
            conversation_id=conv.id,
            role=role,
            content=cleaned,
            created_at=now,
            operation_type=operation_type,
            model_alias=model_alias,
            grounding_metadata=grounding_metadata,
            evidence_references=evidence_references,
            warnings=warnings,
            token_usage=token_usage,
            error_code=error_code,
        )
        self._db.add(msg)
        # Bump the conversation activity timestamp in the same
        # transaction so the message and the timestamp are atomic.
        conv.updated_at = now
        conv.last_message_at = now
        self._db.add(conv)
        self._db.commit()
        self._db.refresh(msg)
        logger.info(
            "conversation.message.add user_id=%s conversation_id=%s "
            "message_id=%s role=%s",
            user_id, conversation_id, msg.id, role,
        )
        return msg

    # ----- message listing ---------------------------------------------

    def list_messages(
        self,
        *,
        user_id: int,
        conversation_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[ConversationMessage], int, bool]:
        """Return a chronological page of the conversation's messages.

        Returns ``(rows, count, has_more)``.  Ordering is
        oldest-first (chronological) so the client can render a
        coherent timeline.  ``has_more`` lets the client detect
        whether further pages exist.
        """
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        lim, off = _validate_limit_offset(
            limit=limit,
            offset=offset,
            max_limit=self._settings.conversation_messages_max_limit,
        )
        page_stmt = (
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv.id)
            .order_by(
                ConversationMessage.created_at.asc(),
                ConversationMessage.id.asc(),
            )
            .limit(lim + 1)
            .offset(off)
        )
        rows = list(self._db.execute(page_stmt).scalars())
        has_more = len(rows) > lim
        if has_more:
            rows = rows[:lim]
        return rows, len(rows), has_more

    def get_recent_messages(
        self,
        *,
        user_id: int,
        conversation_id: int,
        max_messages: int,
        max_chars: int,
    ) -> List[HistoryTurn]:
        """Return up to ``max_messages`` recent USER+ASSISTANT turns.

        Bounded by:

        * ``max_messages`` (caller-supplied, validated against
          ``ai_max_history_messages`` by the route layer).
        * ``max_chars`` — total character budget across the
          resulting rows.  Rows are sliced from the most recent
          backwards until either the message budget or the
          character budget is exhausted.

        SYSTEM_EVENT rows are NEVER returned — they are operational
        metadata, not dialogue, and would only confuse the model.
        The returned :class:`HistoryTurn` objects carry the
        ``role`` / ``content`` / ``created_at`` triple; the API
        layer pipes them into :func:`render_history_block`.
        """
        conv = self._get_owned_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        if max_messages < 0 or max_chars < 0:
            raise InvalidPagination("history budgets must be non-negative")
        if max_messages == 0 or max_chars == 0:
            return []

        stmt = (
            select(ConversationMessage)
            .where(
                and_(
                    ConversationMessage.conversation_id == conv.id,
                    ConversationMessage.role.in_(("USER", "ASSISTANT")),
                )
            )
            .order_by(
                ConversationMessage.created_at.desc(),
                ConversationMessage.id.desc(),
            )
            .limit(max_messages)
        )
        rows_desc = list(self._db.execute(stmt).scalars())
        # Reverse to chronological order before applying the char
        # budget so the model sees the most recent turns in order.
        rows = list(reversed(rows_desc))

        # Char budget: trim from the front (oldest first) until we
        # fit.  This is deterministic for a given input.
        total = 0
        kept: List[ConversationMessage] = []
        for msg in rows:
            length = len(msg.content or "")
            if total + length > max_chars and kept:
                # Stop once we exceed the budget; we already kept
                # at least one message so the history is never empty.
                break
            kept.append(msg)
            total += length
        return [
            HistoryTurn(role=msg.role, content=msg.content, created_at=msg.created_at)
            for msg in kept
        ]


# ---------------------------------------------------------------------------
# Helpers for safe JSON metadata
# ---------------------------------------------------------------------------


def _safe_json(value: Any, *, name: str) -> Any:
    """Return ``value`` if it is a JSON-serialisable primitive container.

    Accepts ``None`` or a ``dict`` / ``list`` whose elements are
    primitives (str, int, float, bool, None).  Raises
    :class:`ValueError` on anything more complex so the service
    layer never silently drops arbitrary objects into JSONB.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{name} keys must be strings")
            _assert_primitive(v, name=f"{name}.{k}")
        return value
    if isinstance(value, list):
        for i, v in enumerate(value):
            _assert_primitive(v, name=f"{name}[{i}]")
        return value
    raise ValueError(f"{name} must be a dict, list, or None")


def _assert_primitive(value: Any, *, name: str) -> None:
    """Recursively validate that ``value`` is JSON-serialisable."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{name} keys must be strings")
            _assert_primitive(v, name=f"{name}.{k}")
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _assert_primitive(v, name=f"{name}[{i}]")
        return
    raise ValueError(
        f"{name} contains a non-JSON value of type {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Module-level dependency factory — mirrors :func:`get_auth_service`.
# ---------------------------------------------------------------------------


from typing import Generator

from app.db.session import SessionLocal  # noqa: E402  (late import)


def get_conversation_service(
    db=None,
    *,
    settings: Optional[Settings] = None,
) -> Generator["ConversationService", None, None]:
    """FastAPI dependency yielding a request-scoped ConversationService.

    When called as a FastAPI dependency, ``db`` is supplied by the
    framework.  Tests can pass a custom session directly.
    """
    if db is None:
        session = SessionLocal()
        own = True
    else:
        session = db
        own = False
    try:
        yield ConversationService(db=session, settings=settings)
    finally:
        if own:
            session.close()


__all__ = [
    "ConversationError",
    "ConversationNotFound",
    "ConversationService",
    "HistoryTurn",
    "InvalidConversationTitle",
    "InvalidMessageContent",
    "InvalidMessageRole",
    "InvalidPagination",
    "get_conversation_service",
    "render_history_block",
]
