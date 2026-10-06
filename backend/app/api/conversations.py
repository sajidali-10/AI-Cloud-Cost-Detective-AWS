"""FastAPI routes for Phase 5B conversations + AI history.

Routes (all under ``/conversations``; nginx strips ``/api/``):

* ``POST   /conversations``                          \u2014 create conversation.
* ``GET    /conversations``                          \u2014 list (paginated).
* ``GET    /conversations/{conversation_id}``        \u2014 detail.
* ``PATCH  /conversations/{conversation_id}``        \u2014 rename / archive.
* ``DELETE /conversations/{conversation_id}``        \u2014 hard delete.
* ``GET    /conversations/{conversation_id}/messages``\u2014 paginated messages.
* ``POST   /conversations/{conversation_id}/analyze``\u2014 grounded AI Q&A.

RBAC
----
``require_role(\"ADMIN\", \"ANALYST\")`` is the gate for every
endpoint \u2014 VIEWERs are denied (consistent with Phase 4's
``/api/ai/*`` policy).  Centralizing the role check in one
dependency prevents accidentally exposing a new endpoint without
it.

Auth-disabled behaviour
-----------------------
The Phase 5A dependency ``require_role`` injects a synthetic
ADMIN when ``AUTH_ENABLED=false`` so backward-compatibility is
preserved.  However, durable state requires an identity, so the
conversation routes additionally short-circuit when ``auth_enabled``
is false and return a controlled 503 ``AuthDisabled`` response
\u2014 we never persist conversation history without an identity.
The same behaviour applies to the analyze endpoint.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.core.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.ai import (
    AIGenerationStatus,
    AIGrounding,
    AIResponse,
)
from app.schemas.conversation import (
    ConversationAnalyzeRequest,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMessageView,
    ConversationMessagesResponse,
    ConversationPatchRequest,
    ConversationView,
)
from app.services.ai_service import AIService, get_ai_service
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

logger = logging.getLogger("cost-detective-backend.conversations_api")

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# Error helpers \u2014 mirror the existing project envelope.
# ---------------------------------------------------------------------------


def _error(
    *,
    code: str,
    message: str,
    http_status: int = 400,
    **extra: Any,
) -> JSONResponse:
    payload: dict = {
        "status": "error",
        "error_code": code,
        "message": message,
    }
    payload.update(extra)
    return JSONResponse(status_code=http_status, content=payload)


def _auth_disabled_response() -> JSONResponse:
    """Return the controlled 503 response for AUTH_ENABLED=false.

    Durable conversation endpoints never create anonymous persistent
    ownership \u2014 the user must turn on authentication and supply
    a real identity before persisting state.  Phase 0\u20134 dev
    workflows continue to use the anonymous /api/aws/* and
    /api/ai/* endpoints.
    """
    return _error(
        code="AuthDisabled",
        message=(
            "Conversation persistence requires authentication. "
            "Set AUTH_ENABLED=true and authenticate the request, or use "
            "the stateless /api/ai/* endpoints for anonymous analysis."
        ),
        http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


# ---------------------------------------------------------------------------
# Dependency overrides for tests
# ---------------------------------------------------------------------------


# Mirrors ``app.api.ai._service_factory`` \u2014 tests monkey-patch
# this to inject a fake :class:`AIService`.
_service_factory = get_ai_service


# ---------------------------------------------------------------------------
# Route guard helper
# ---------------------------------------------------------------------------


def _require_auth_enabled(settings: Settings) -> Optional[JSONResponse]:
    """Return an AuthDisabled response when ``auth_enabled`` is false.

    Centralized so every conversation route enforces the same
    durability requirement.
    """
    if settings.auth_enabled:
        return None
    return _auth_disabled_response()


# ---------------------------------------------------------------------------
# POST /conversations
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new AI conversation (ADMIN | ANALYST)",
)
def create_conversation(
    payload: Optional[ConversationCreateRequest] = None,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Create a conversation for the authenticated user."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    service = ConversationService(db=db)
    try:
        conv = service.create(
            user_id=int(user.id),
            title=(payload.title if payload else None),
        )
    except InvalidConversationTitle as exc:
        return _error(
            code="InvalidTitle",
            message=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    return ConversationView.model_validate(conv).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /conversations
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List the authenticated user's conversations",
)
def list_conversations(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    archived: Optional[bool] = Query(default=None),
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Return a page of the user's conversations."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    service = ConversationService(db=db)
    try:
        rows, count = service.list_for_user(
            user_id=int(user.id),
            limit=limit,
            offset=offset,
            archived=archived,
        )
    except InvalidPagination as exc:
        return _error(
            code="InvalidPagination",
            message=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    payload = ConversationListResponse(
        conversations=[ConversationView.model_validate(c) for c in rows],
        count=count,
        limit=limit,
        offset=offset,
    )
    return payload.model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}",
    summary="Get a single conversation by id",
)
def get_conversation(
    conversation_id: int,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Return the conversation and the message count."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    service = ConversationService(db=db)
    try:
        conv = service.get(user_id=int(user.id), conversation_id=conversation_id)
        count = service.count_messages(
            user_id=int(user.id), conversation_id=conversation_id
        )
    except ConversationNotFound:
        return _error(
            code="ConversationNotFound",
            message="conversation not found",
            http_status=status.HTTP_404_NOT_FOUND,
            conversation_id=conversation_id,
        )
    payload = ConversationDetailResponse(
        conversation=ConversationView.model_validate(conv),
        message_count=count,
    )
    return payload.model_dump(mode="json")


# ---------------------------------------------------------------------------
# PATCH /conversations/{conversation_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/{conversation_id}",
    summary="Rename and/or archive a conversation",
)
def patch_conversation(
    conversation_id: int,
    payload: ConversationPatchRequest,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Apply a partial update to the conversation."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    if payload.title is None and payload.is_archived is None:
        return _error(
            code="EmptyPatch",
            message="at least one of 'title' or 'is_archived' must be provided",
            http_status=status.HTTP_400_BAD_REQUEST,
        )

    service = ConversationService(db=db)
    try:
        conv = service.get(user_id=int(user.id), conversation_id=conversation_id)
        if payload.title is not None:
            conv = service.rename(
                user_id=int(user.id),
                conversation_id=conversation_id,
                title=payload.title,
            )
        if payload.is_archived is not None:
            conv = service.set_archived(
                user_id=int(user.id),
                conversation_id=conversation_id,
                archived=payload.is_archived,
            )
    except ConversationNotFound:
        return _error(
            code="ConversationNotFound",
            message="conversation not found",
            http_status=status.HTTP_404_NOT_FOUND,
            conversation_id=conversation_id,
        )
    except InvalidConversationTitle as exc:
        return _error(
            code="InvalidTitle",
            message=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    return ConversationView.model_validate(conv).model_dump(mode="json")


# ---------------------------------------------------------------------------
# DELETE /conversations/{conversation_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{conversation_id}",
    summary="Hard-delete a conversation and its messages",
)
def delete_conversation(
    conversation_id: int,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Delete the conversation; messages are removed by ON DELETE CASCADE."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    service = ConversationService(db=db)
    try:
        service.delete(user_id=int(user.id), conversation_id=conversation_id)
    except ConversationNotFound:
        return _error(
            code="ConversationNotFound",
            message="conversation not found",
            http_status=status.HTTP_404_NOT_FOUND,
            conversation_id=conversation_id,
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok",
            "deleted": True,
            "conversation_id": conversation_id,
        },
    )


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}/messages
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}/messages",
    summary="List a conversation's messages (oldest first)",
)
def list_conversation_messages(
    conversation_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Return a chronological page of messages."""
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp
    service = ConversationService(db=db)
    try:
        rows, count, has_more = service.list_messages(
            user_id=int(user.id),
            conversation_id=conversation_id,
            limit=limit,
            offset=offset,
        )
    except ConversationNotFound:
        return _error(
            code="ConversationNotFound",
            message="conversation not found",
            http_status=status.HTTP_404_NOT_FOUND,
            conversation_id=conversation_id,
        )
    except InvalidPagination as exc:
        return _error(
            code="InvalidPagination",
            message=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )
    payload = ConversationMessagesResponse(
        messages=[ConversationMessageView.model_validate(m) for m in rows],
        count=count,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )
    return payload.model_dump(mode="json")


# ---------------------------------------------------------------------------
# POST /conversations/{conversation_id}/analyze
# ---------------------------------------------------------------------------


@router.post(
    "/{conversation_id}/analyze",
    summary="Ask a grounded question in an existing conversation",
)
def analyze_conversation(
    conversation_id: int,
    payload: ConversationAnalyzeRequest,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Persist the user question, generate an AI response, persist it.

    Persistence semantics (matches the spec):

    1. ``USER`` message is persisted BEFORE the AI call.  Even on
       failure the user message survives so the conversation is
       never lost.
    2. Fresh authoritative Phase 2/3 evidence is fetched.
    3. Bounded prior-turn history is attached to the user-message
       body (NEVER to the system channel).
    4. On success an ``ASSISTANT`` message is persisted with safe
       provenance.
    5. On failure a ``SYSTEM_EVENT`` row is persisted with the
       sanitized error code; the response carries the controlled
       ``AIResponse`` envelope with no fabricated assistant text.
    """
    if (resp := _require_auth_enabled(settings)) is not None:
        return resp

    service = ConversationService(db=db)

    # --- validate question length against the same ceiling the AI uses
    question = (payload.question or "").strip()
    if not question:
        return _error(
            code="InvalidQuestion",
            message="question must be a non-empty string",
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(question) > settings.ai_max_question_length:
        return _error(
            code="InvalidQuestion",
            message=(
                f"question length {len(question)} exceeds the maximum of "
                f"{settings.ai_max_question_length}."
            ),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            question_length=len(question),
        )
    if payload.days not in (7, 30, 60, 90):
        return _error(
            code="InvalidLookbackDays",
            message=(
                f"days={payload.days!r} is not allowed; "
                "allowed values are [7, 30, 60, 90]."
            ),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            days=payload.days,
        )

    try:
        service.get(user_id=int(user.id), conversation_id=conversation_id)
    except ConversationNotFound:
        return _error(
            code="ConversationNotFound",
            message="conversation not found",
            http_status=status.HTTP_404_NOT_FOUND,
            conversation_id=conversation_id,
        )

    # --- persist the USER message first (never lose user input)
    try:
        service.add_user_message(
            user_id=int(user.id),
            conversation_id=conversation_id,
            content=question,
        )
    except (InvalidMessageRole, InvalidMessageContent) as exc:
        return _error(
            code="InvalidMessage",
            message=str(exc),
            http_status=status.HTTP_400_BAD_REQUEST,
        )

    # --- load bounded history (user-message already persisted above)
    history_turns: list[HistoryTurn] = []
    try:
        history_turns = service.get_recent_messages(
            user_id=int(user.id),
            conversation_id=conversation_id,
            max_messages=settings.ai_max_history_messages,
            max_chars=settings.ai_max_history_chars,
        )
        # The just-persisted USER message is the most recent turn;
        # we drop it from the history rendering because it appears
        # again as the explicit question below.
        history_turns = _strip_last_user_turn(history_turns)
    except Exception:  # noqa: BLE001
        # History loading is best-effort; failing to load it should
        # never block the AI call.  Logged below.
        history_turns = []

    history_text = render_history_block(history_turns)

    # --- invoke the AI service (single LiteLLM call)
    ai: AIService = _service_factory()
    ai_response: AIResponse = ai.generate_analysis_with_history(
        region=payload.region or "",
        days=payload.days,
        question=question,
        history_text=history_text,
    )

    # --- persist ASSISTANT on success or SYSTEM_EVENT on failure
    if ai_response.status == AIGenerationStatus.SUCCESS:
        try:
            service.add_assistant_message(
                user_id=int(user.id),
                conversation_id=conversation_id,
                content=ai_response.answer,
                operation_type=ai_response.operation or "analyze",
                model_alias=ai_response.model,
                grounding_metadata=ai_response.grounding.model_dump(mode="json"),
                evidence_references=ai_response.citations,
                warnings=ai_response.warnings,
                token_usage=None,
            )
        except (InvalidMessageRole, InvalidMessageContent) as exc:
            logger.warning(
                "conversations.analyze.persist_failed conversation_id=%s "
                "error=%s", conversation_id, str(exc),
            )
        return ai_response.model_dump(mode="json")

    if ai_response.status == AIGenerationStatus.DISABLED:
        # AI is off \u2014 do not fabricate anything.  Persist a
        # SYSTEM_EVENT so the conversation timeline is honest.
        try:
            service.add_system_event(
                user_id=int(user.id),
                conversation_id=conversation_id,
                code="AI_DISABLED",
            )
        except Exception:  # noqa: BLE001
            logger.warning("conversations.analyze.system_event_failed")
        return _error(
            code="AIDisabled",
            message=(
                "AI generation is disabled in configuration; the user "
                "message was recorded but no assistant response was "
                "generated."
            ),
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            ai_status=ai_response.status.value,
        )

    # PARTIAL_SUCCESS / UNAVAILABLE / FAILED \u2014 persist sanitized
    # SYSTEM_EVENT and return the AI envelope to the caller.  No
    # fabricated assistant content is stored.
    error_code = _sanitize_failure_code(ai_response.warnings)
    try:
        service.add_system_event(
            user_id=int(user.id),
            conversation_id=conversation_id,
            code=error_code,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "conversations.analyze.system_event_failed code=%s", error_code,
        )
    # Surface the AI envelope as-is.  HTTP 200 because the AI layer
    # explicitly returns controlled statuses rather than HTTP codes.
    return ai_response.model_dump(mode="json")


def _strip_last_user_turn(turns: list[HistoryTurn]) -> list[HistoryTurn]:
    """Drop the most recent USER turn (it is already in the question).

    ``get_recent_messages`` returns the just-persisted USER message
    as the last entry; we exclude it from the history block so the
    model sees the user's question exactly once (in the explicit
    ``<user_question>`` block).
    """
    if not turns:
        return turns
    if turns[-1].role.upper() == "USER":
        return turns[:-1]
    return turns


def _sanitize_failure_code(warnings: list[str]) -> str:
    """Pick a stable, sanitized error code from the AI failure warnings.

    The provider warning list is untrusted free text; we never store
    it verbatim.  We pattern-match against the known stable codes
    the LiteLLM client emits; when nothing matches we fall back to
    ``AI_UNAVAILABLE``.
    """
    if not warnings:
        return "AI_UNAVAILABLE"
    candidates = {
        "LITELLM_TIMEOUT",
        "LITELLM_UNAVAILABLE",
        "LITELLM_RATE_LIMIT",
        "LITELLM_AUTH",
        "LITELLM_QUOTA_EXHAUSTED",
        "LITELLM_PROVIDER_ERROR",
        "LITELLM_MALFORMED_RESPONSE",
        "LITELLM_EMPTY_COMPLETION",
    }
    for w in warnings:
        token = (w or "").strip().split(":", 1)[0].strip().upper()
        if token in candidates:
            return token
    return "AI_UNAVAILABLE"


__all__ = [
    "analyze_conversation",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_conversation_messages",
    "list_conversations",
    "patch_conversation",
    "router",
]
