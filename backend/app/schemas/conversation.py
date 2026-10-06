"""Pydantic schemas for Phase 5B conversations + AI message history.

Wire conventions match the existing project:

* Response shape never includes the raw LiteLLM payload, JWTs, AWS
  credentials, Authorization headers, or chain-of-thought.
* Errors are surfaced via the standard envelope
  ``{"status":"error","error_code":"...","message":"..."}`` — see
  :mod:`app.api.deps` and ``app.main`` for the global handler.
* The schema layer is purely a serializer/validator.  No DB
  imports.
* All user-supplied strings are bounded (length) and stripped
  before they reach the service layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Controlled role set; mirrors :data:`CONVERSATION_MESSAGE_ROLES` in
# :mod:`app.db.models`.  The Pydantic Literal keeps the API surface
# honest — a client cannot submit an arbitrary role string.
MessageRole = Literal["USER", "ASSISTANT", "SYSTEM_EVENT"]


# ---------------------------------------------------------------------------
# Conversation request shapes
# ---------------------------------------------------------------------------


class ConversationCreateRequest(BaseModel):
    """Body for ``POST /api/conversations``.

    ``title`` is optional; the service layer falls back to the
    configured default (``New Cost Analysis``).  Title length is
    bounded here to the schema upper bound (500 chars) so the
    service layer can return a controlled 400 ``InvalidTitle``
    response for values that exceed the DB column width (200) —
    the route never lets Pydantic truncate silently.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Optional[str] = Field(default=None, max_length=500)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        return cleaned


class ConversationPatchRequest(BaseModel):
    """Body for ``PATCH /api/conversations/{id}``.

    At least one of ``title`` or ``is_archived`` must be supplied.
    Empty/blank titles are rejected by the service layer; the
    schema upper bound matches the configuration so the service
    can return a controlled 400 ``InvalidTitle``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Optional[str] = Field(default=None, max_length=500)
    is_archived: Optional[bool] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("title must be non-empty after trimming")
        return cleaned


# ---------------------------------------------------------------------------
# Conversation response shapes
# ---------------------------------------------------------------------------


class ConversationView(BaseModel):
    """Single-conversation summary.

    Same shape for list and detail endpoints to keep the client
    contract simple.  Never exposes internal IDs other than the
    conversation ``id`` and the owner ``user_id`` (the latter is
    already known to the authenticated caller via ``/me``).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    title: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    last_message_at: Optional[datetime] = None


class ConversationListResponse(BaseModel):
    """Paginated conversation list payload.

    ``count`` is the count of conversations returned in *this*
    page (not the total in the database); ``limit`` / ``offset``
    echo the request values so the client can paginate deterministically.
    """

    conversations: List[ConversationView]
    count: int
    limit: int
    offset: int


class ConversationDetailResponse(BaseModel):
    """Single-conversation detail (with pagination metadata for messages).

    ``messages`` is intentionally absent from this shape — messages
    have their own paginated endpoint.  The detail endpoint returns
    metadata only.
    """

    conversation: ConversationView
    message_count: int


# ---------------------------------------------------------------------------
# Message request / response shapes
# ---------------------------------------------------------------------------


class ConversationMessageView(BaseModel):
    """Public view of a single conversation message.

    ``role`` is constrained to ``USER | ASSISTANT | SYSTEM_EVENT``
    (see :data:`MessageRole`).  Metadata fields are optional and
    only populated for ASSISTANT / SYSTEM_EVENT rows.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: MessageRole
    content: str
    created_at: datetime
    operation_type: Optional[str] = None
    model_alias: Optional[str] = None
    grounding_metadata: Optional[Dict[str, Any]] = None
    evidence_references: Optional[List[Dict[str, Any]]] = None
    warnings: Optional[List[str]] = None
    token_usage: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None


class ConversationMessagesResponse(BaseModel):
    """Paginated message list.

    Ordering is always oldest-first (chronological) — never
    newest-first — so the client can render the conversation as a
    coherent timeline.
    """

    messages: List[ConversationMessageView]
    count: int
    limit: int
    offset: int
    has_more: bool


# ---------------------------------------------------------------------------
# Conversation analyze request
# ---------------------------------------------------------------------------


class ConversationAnalyzeRequest(BaseModel):
    """Body for ``POST /api/conversations/{id}/analyze``.

    ``question`` is required and bounded to
    ``ai_max_question_length``.  ``region`` and ``days`` mirror
    :class:`app.schemas.ai.AnalyzeRequest`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    region: Optional[str] = Field(default=None, max_length=64)
    days: int = Field(default=30)
    question: str = Field(min_length=1, max_length=2000)


__all__ = [
    "ConversationAnalyzeRequest",
    "ConversationCreateRequest",
    "ConversationDetailResponse",
    "ConversationListResponse",
    "ConversationMessageView",
    "ConversationMessagesResponse",
    "ConversationPatchRequest",
    "ConversationView",
    "MessageRole",
]
