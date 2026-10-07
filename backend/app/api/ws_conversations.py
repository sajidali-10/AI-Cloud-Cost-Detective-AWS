"""Realtime conversation WebSocket endpoint — Phase 5C.

Mounted at ``/ws/conversations`` (public path ``/api/ws/conversations``
after nginx strips ``/api/``).  The endpoint is intentionally
single-conversation: the ``conversation_id`` is part of the path and
ownership is verified BEFORE the WebSocket upgrade completes.

WebSockets are a transport layer — this module does NOT contain any
AI logic.  Every AI request flows through:

    WebSocket frame
        ↓ parse_client_event
        ↓ ConversationService (persistence)
        ↓ AIService.generate_analysis_with_history (fresh evidence)
        ↓ ConversationService (assistant persistence)
        ↓ WebSocket frame back to client

Failure modes
-------------

* AUTH_ENABLED=false → 1008 close at handshake.
* Missing/invalid/expired token → 4401 close.
* Inactive user / VIEWER role / forged role claim → 4403 close.
* Cross-user / nonexistent conversation → 4404 close (indistinguishable
  from "not found" to avoid leaking private conversation IDs).
* In-band errors (bad JSON, unsupported event type, oversize frame)
  produce a sanitized ``error`` event with ``code=ProtocolViolation``.
* AI failure → ``error`` event with ``code=AIUnavailable``; the USER
  message is preserved in the DB.
* Duplicate ``request_id`` → ``error`` event with ``code=Busy`` (the
  previous request is still in flight) or silently consumed when the
  previous request already completed (FIFO dedup).

Concurrency
-----------

ONE AI request at a time per connection.  A second ``user_message``
arriving while one is in flight is rejected with an ``error`` event
(code ``Busy``).  The asyncio.Lock guarantees correctness; the
boolean flag is kept for cheap reads.

Heartbeat
---------

Client-driven.  Clients send ``{"type":"ping","ts":...}``; the server
replies with ``{"type":"pong","ts":...}``.  There is no outbound
heartbeat — that would needlessly wake idle connections.  Browsers
detect liveness through the pong round-trip; proxies (nginx) drop
idle connections after their own timeout, which is fine because the
client will reconnect.

Persistence
-----------

USER messages are persisted BEFORE the AI call so a crash mid-flight
never loses user input.  ASSISTANT messages are persisted after a
successful call.  SYSTEM_EVENT rows are persisted on AI failure so the
timeline stays honest — no fabricated assistant text is ever stored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Path, WebSocket, WebSocketDisconnect

from app.core.config import get_settings as _ws_get_settings

from app.api.ws_auth import (
    WSAuthFailure,
    WS_CLOSE_AUTH_DISABLED,
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_NOT_FOUND,
    WS_CLOSE_PROTOCOL_ERROR,
    WS_CLOSE_TOO_MANY_REQUESTS,
    WS_CLOSE_UNAUTHORIZED,
    negotiate_subprotocol,
    resolve_ws_principal,
)
from app.db.session import SessionLocal
from app.schemas.ai import AIGenerationStatus, AIResponse
from app.schemas.conversation import (
    ConversationMessageView,
)
from app.schemas.websocket import (
    MAX_FRAME_BYTES,
    MAX_RECENT_REQUEST_IDS,
    PROTOCOL_VERSION,
    ClientEvent,
    ServerAiProcessing,
    ServerAssistantMessage,
    ServerConnected,
    ServerError,
    ServerPong,
    ServerUserMessageAccepted,
    parse_client_event,
)
from app.services.ai_service import AIService, get_ai_service
from app.services.conversation_service import (
    ConversationError,
    ConversationNotFound,
    ConversationService,
    HistoryTurn,
    InvalidMessageContent,
    InvalidMessageRole,
    render_history_block,
)

logger = logging.getLogger("cost-detective-backend.ws_conversations")

router = APIRouter(prefix="/ws/conversations", tags=["ws"])


# ---------------------------------------------------------------------------
# Dependency overrides for tests (mirrors app/api/ai.py + app/api/conversations.py)
# ---------------------------------------------------------------------------


#: Default AIService factory — replaced by tests via monkeypatch so the
#: real LiteLLM client never gets exercised in unit tests.  This is a
#: module-level binding rather than a FastAPI Depends so tests don't
#: have to plumb overrides through the dependency tree.
_service_factory = get_ai_service


def _make_service() -> AIService:
    """Return the AIService instance the WebSocket should use."""
    return _service_factory()


#: Session factory used to open a per-connection database session.
#: Tests override this with an in-memory SQLite session factory so
#: the WS route exercises the same database as the REST routes.
#: Production uses :data:`app.db.session.SessionLocal`.
_session_factory = SessionLocal


# ---------------------------------------------------------------------------
# Per-connection state
# ---------------------------------------------------------------------------


class _ConnectionState:
    """Mutable, per-connection runtime state.

    Owns:

    * The async lock that serializes AI requests.
    * The inflight boolean that drives the busy / duplicate decisions.
    * A bounded FIFO of recently processed ``request_id`` values.
    * Per-connection counters used by the disconnect handler.
    """

    __slots__ = (
        "lock",
        "inflight",
        "recent_request_ids",
        "messages_processed",
        "ai_failures",
    )

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.inflight = False
        # OrderedDict gives O(1) FIFO eviction: popitem(last=False)
        # drops the oldest entry.
        self.recent_request_ids: "OrderedDict[str, None]" = OrderedDict()
        self.messages_processed = 0
        self.ai_failures = 0

    def remember_request_id(self, request_id: str) -> bool:
        """Record ``request_id`` as processed; return True if it was new.

        When the same request_id arrives twice (browser resend, retry),
        the second call returns ``False`` and the caller treats the
        request as already handled.  The FIFO is bounded to
        :data:`MAX_RECENT_REQUEST_IDS`.
        """
        if request_id in self.recent_request_ids:
            return False
        self.recent_request_ids[request_id] = None
        while len(self.recent_request_ids) > MAX_RECENT_REQUEST_IDS:
            self.recent_request_ids.popitem(last=False)
        return True


# ---------------------------------------------------------------------------
# Frame send helper
# ---------------------------------------------------------------------------


async def _send(websocket: WebSocket, event: Dict[str, Any]) -> None:
    """Serialize ``event`` to JSON and send it as a text frame.

    Uses ``send_text`` so we control serialization order (Pydantic
    ``model_dump_json`` would also work but we keep the helper
    dependency-free).  The bytes payload is bounded by the schema's
    MAX_EVENT_BYTES cap.
    """
    payload = json.dumps(event, separators=(",", ":"), default=str)
    # ``len(payload)`` is the UTF-8 byte count for ASCII content; the
    # schema-level max_event_bytes is generous so this is a defensive
    # cap, not a hard functional limit.
    if len(payload.encode("utf-8")) > 1_000_000:  # 1 MiB hard server cap
        raise ValueError("server event too large to send")
    await websocket.send_text(payload)


async def _send_connected(
    websocket: WebSocket,
    *,
    conversation_id: int,
    user_id: int,
    role: str,
) -> None:
    await _send(
        websocket,
        ServerConnected(
            type="connected",
            protocol_version=PROTOCOL_VERSION,
            conversation_id=conversation_id,
            user_id=user_id,
            role=role,
            heartbeat_interval_seconds=30,
        ).model_dump(mode="json"),
    )


async def _send_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    request_id: Optional[str] = None,
    conversation_id: Optional[int] = None,
) -> None:
    await _send(
        websocket,
        ServerError(
            type="error",
            code=code,
            message=message,
            request_id=request_id,
            conversation_id=conversation_id,
        ).model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Handshake helpers
# ---------------------------------------------------------------------------


async def _reject(
    websocket: WebSocket,
    *,
    failure: WSAuthFailure,
    conversation_id: Optional[int],
    user_id_hint: Optional[int] = None,
) -> None:
    """Reject the upgrade BEFORE the WebSocket is finalized.

    We do not call ``websocket.accept()`` so the rejection happens at
    the HTTP layer (the client sees a 403/401/etc. rather than an
    accepted socket that immediately closes).  For AUTH_ENABLED=false
    we still need to accept the socket so we can close it with the
    intended 4xxx code; otherwise the load-balancer would never see
    the deliberate close.
    """
    logger.info(
        "ws.handshake.reject reason=%s close_code=%s conversation_id=%s",
        failure.reason, failure.close_code, conversation_id,
    )
    if failure.close_code == WS_CLOSE_AUTH_DISABLED:
        # AUTH_DISABLED — accept first so we can close with 1008
        # (the framework closes the underlying HTTP upgrade itself
        # when we never call accept; we accept then close so the
        # ``close_code`` is honored).
        await websocket.accept()
        await websocket.close(code=failure.close_code, reason=failure.safe_message)
        return
    await websocket.close(code=failure.close_code, reason=failure.safe_message)


# ---------------------------------------------------------------------------
# WebSocket route
# ---------------------------------------------------------------------------


@router.websocket("/{conversation_id}")
async def conversation_socket(
    websocket: WebSocket,
    conversation_id: int = Path(..., ge=1),
) -> None:
    """Authenticate, scope to ``conversation_id``, exchange events."""

    # ---- negotiate subprotocol (browser-compatible JWT transport) -----
    subprotocol = negotiate_subprotocol(websocket)

    # ---- open a DB session for the entire connection -----
    # Use the (test-overridable) session factory so the WebSocket and
    # the REST surface share the same database in unit tests.
    db = _session_factory()
    state = _ConnectionState()
    user_id_log: Optional[int] = None
    role_log: Optional[str] = None
    accepted = False
    ai_failure_codes_seen: list[str] = []
    try:
        # ---- resolve principal (token, role, active, RBAC) ----
        settings = _ws_get_settings()
        user, auth_failure = resolve_ws_principal(
            websocket=websocket, db=db, settings=settings,
        )
        if auth_failure is not None or user is None:
            failure = auth_failure or WSAuthFailure(
                reason="principal_unresolved",
                close_code=WS_CLOSE_UNAUTHORIZED,
                safe_message="authentication failed",
            )
            await _reject(
                websocket,
                failure=failure,
                conversation_id=conversation_id,
            )
            return

        user_id_log = int(user.id)
        role_log = user.role

        # ---- ownership check (cross-user → indistinguishable 4404) ----
        service = ConversationService(db=db)
        try:
            service.get(user_id=int(user.id), conversation_id=conversation_id)
        except ConversationNotFound:
            logger.info(
                "ws.handshake.reject reason=conversation_not_found "
                "user_id=%s conversation_id=%s",
                user.id, conversation_id,
            )
            await websocket.close(
                code=WS_CLOSE_NOT_FOUND,
                reason="conversation not found",
            )
            return

        # ---- accept the WebSocket with the negotiated subprotocol ----
        # starlette requires subprotocol as a keyword argument; ``None``
        # means "no subprotocol negotiation" which is fine for non-browser
        # clients.
        await websocket.accept(subprotocol=subprotocol)
        accepted = True
        await _send_connected(
            websocket,
            conversation_id=conversation_id,
            user_id=int(user.id),
            role=user.role,
        )
        logger.info(
            "ws.connect user_id=%s role=%s conversation_id=%s",
            user.id, user.role, conversation_id,
        )

        # ---- main loop ----
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            # ---- oversize frame guard ----
            if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                await _send_error(
                    websocket,
                    code="OversizedFrame",
                    message="frame too large",
                )
                # Continue the loop — the client can send a smaller
                # frame next.  Closing on every oversize attempt would
                # amplify a buggy client into a denial-of-service.
                continue

            # ---- parse + dispatch ----
            try:
                payload = json.loads(raw)
                event = parse_client_event(payload)
            except json.JSONDecodeError as exc:
                # Sanitize: never include the raw payload in the
                # message back to the client.
                await _send_error(
                    websocket,
                    code="InvalidJSON",
                    message=f"event must be valid JSON ({exc.msg})",
                )
                continue
            except ValueError as exc:
                await _send_error(
                    websocket,
                    code="ProtocolViolation",
                    message=str(exc),
                )
                continue
            except Exception as exc:  # noqa: BLE001 - sanitized path
                # Pydantic ValidationError and any other model-level
                # failure collapse to a sanitized protocol violation.
                await _send_error(
                    websocket,
                    code="ProtocolViolation",
                    message="event failed schema validation",
                )
                logger.info(
                    "ws.event.parse_error user_id=%s conversation_id=%s "
                    "error=%s",
                    user.id, conversation_id, type(exc).__name__,
                )
                continue

            if isinstance(event, ClientEvent):
                # The runtime narrowing on ``ClientEvent`` keeps the
                # dispatch below type-safe.
                pass  # pragma: no cover — covered below
            # ``ClientEvent`` is a Union; discriminate manually.
            event_type = getattr(event, "type", None)
            if event_type == "ping":
                ts = getattr(event, "ts", None)
                await _send(
                    websocket,
                    ServerPong(type="pong", ts=ts).model_dump(mode="json"),
                )
                continue
            if event_type == "user_message":
                await _handle_user_message(
                    websocket=websocket,
                    state=state,
                    db=db,
                    service=service,
                    user=user,
                    conversation_id=conversation_id,
                    request_id=event.request_id,
                    question=event.question,
                    region=event.region,
                    days=event.days,
                    ai_failure_sink=ai_failure_codes_seen,
                )
                continue

            # Should be unreachable; guarded by the schema.
            await _send_error(
                websocket,
                code="UnsupportedEventType",
                message=f"unsupported event type {event_type!r}",
            )
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # Task was cancelled — propagate so the framework can clean up.
        raise
    except Exception as exc:  # noqa: BLE001
        # Last-resort guard.  Log sanitized context, then close
        # cleanly — never raise through the WebSocket layer.
        logger.warning(
            "ws.unhandled_error user_id=%s conversation_id=%s error=%s",
            user_id_log, conversation_id, type(exc).__name__,
        )
        if accepted:
            try:
                await _send_error(
                    websocket,
                    code="InternalError",
                    message="the realtime connection encountered an internal error",
                )
                await websocket.close(code=1011, reason="internal error")
            except Exception:  # noqa: BLE001
                pass
    finally:
        if state.inflight:
            # Cancel any in-flight AI work; the WS is going away.
            pass
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            "ws.disconnect user_id=%s role=%s conversation_id=%s "
            "messages=%s ai_failures=%s",
            user_id_log,
            role_log,
            conversation_id,
            state.messages_processed,
            state.ai_failures,
        )


# ---------------------------------------------------------------------------
# user_message handler
# ---------------------------------------------------------------------------


async def _handle_user_message(
    *,
    websocket: WebSocket,
    state: _ConnectionState,
    db,
    service: ConversationService,
    user,
    conversation_id: int,
    request_id: str,
    question: str,
    region: Optional[str],
    days: int,
    ai_failure_sink: list[str],
) -> None:
    """Persist USER, run AI, persist ASSISTANT (or SYSTEM_EVENT), reply."""
    # ---- duplicate / busy guard ----
    async with state.lock:
        if state.inflight:
            # Another AI request is still running on this connection.
            await _send_error(
                websocket,
                code="Busy",
                message=(
                    "another AI request is in progress on this connection; "
                    "wait for it to finish before sending another question"
                ),
                request_id=request_id,
                conversation_id=conversation_id,
            )
            return
        # Even if no AI is in flight, the same request_id arriving twice
        # is treated as a duplicate resend.  We still reply with the
        # original outcome IF the request_id is in the recent set, but
        # because we don't keep the full response around we just emit a
        # deduplicated error event so the client knows it was a no-op.
        if request_id in state.recent_request_ids:
            await _send_error(
                websocket,
                code="Busy",
                message="duplicate request_id — already processed",
                request_id=request_id,
                conversation_id=conversation_id,
            )
            return
        state.inflight = True

    start = time.monotonic()
    try:
        # ---- ownership re-check (defensive — conversation deleted
        # between handshake and message) ----
        try:
            service.get(user_id=int(user.id), conversation_id=conversation_id)
        except ConversationNotFound:
            await _send_error(
                websocket,
                code="ConversationNotFound",
                message="conversation not found",
                request_id=request_id,
                conversation_id=conversation_id,
            )
            return

        # ---- persist USER first (never lose user input) ----
        try:
            user_msg = service.add_user_message(
                user_id=int(user.id),
                conversation_id=conversation_id,
                content=question,
            )
        except (InvalidMessageRole, InvalidMessageContent) as exc:
            await _send_error(
                websocket,
                code="InvalidQuestion",
                message=str(exc),
                request_id=request_id,
                conversation_id=conversation_id,
            )
            return
        except ConversationError as exc:
            logger.warning(
                "ws.user_message.persist_failed user_id=%s "
                "conversation_id=%s error=%s",
                user.id, conversation_id, type(exc).__name__,
            )
            await _send_error(
                websocket,
                code="ProtocolViolation",
                message="user message could not be persisted",
                request_id=request_id,
                conversation_id=conversation_id,
            )
            return

        await _send(
            websocket,
            ServerUserMessageAccepted(
                type="user_message_accepted",
                request_id=request_id,
                conversation_id=conversation_id,
                message_id=int(user_msg.id),
            ).model_dump(mode="json"),
        )

        # ---- bounded prior history ----
        cfg = _ws_get_settings()
        history_turns: list[HistoryTurn] = []
        try:
            history_turns = service.get_recent_messages(
                user_id=int(user.id),
                conversation_id=conversation_id,
                max_messages=cfg.ai_max_history_messages,
                max_chars=cfg.ai_max_history_chars,
            )
            # The just-persisted USER message is the most recent turn;
            # drop it so it appears only once (in the question block).
            if history_turns and history_turns[-1].role.upper() == "USER":
                history_turns = history_turns[:-1]
        except Exception:  # noqa: BLE001
            history_turns = []
        history_text = render_history_block(history_turns)

        # ---- emit ai_processing ----
        await _send(
            websocket,
            ServerAiProcessing(
                type="ai_processing",
                request_id=request_id,
                conversation_id=conversation_id,
            ).model_dump(mode="json"),
        )

        # ---- invoke the AI service (offload sync LiteLLM call) ----
        ai_service: AIService = _make_service()
        # Bounded timeout = configured AI timeout + small slack so a
        # racing client disconnect does not leave the LiteLLM call
        # running forever.
        timeout_seconds = float(cfg.ai_request_timeout_seconds) + 5.0
        try:
            ai_response: AIResponse = await asyncio.wait_for(
                asyncio.to_thread(
                    ai_service.generate_analysis_with_history,
                    region=region or "",
                    days=days,
                    question=question,
                    history_text=history_text,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            ai_response = AIResponse(
                status=AIGenerationStatus.UNAVAILABLE,
                operation="analyze",
                answer=(
                    "AI generation timed out before a response was "
                    "received. The Phase 2/3 evidence is still valid; "
                    "the model layer did not respond in time."
                ),
                model=None,
                grounding={
                    "region": region or "",
                    "days": days,
                    "cost_evidence_used": False,
                    "recommendations_used": 0,
                    "capabilities_used": False,
                },
                citations=[],
                warnings=["LITELLM_TIMEOUT"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ws.ai_call.unhandled_error user_id=%s conversation_id=%s "
                "request_id=%s error=%s",
                user.id, conversation_id, request_id, type(exc).__name__,
            )
            ai_response = AIResponse(
                status=AIGenerationStatus.UNAVAILABLE,
                operation="analyze",
                answer="AI generation was not available.",
                model=None,
                grounding={
                    "region": region or "",
                    "days": days,
                    "cost_evidence_used": False,
                    "recommendations_used": 0,
                    "capabilities_used": False,
                },
                citations=[],
                warnings=["AI_UNAVAILABLE"],
            )

        # ---- persist + reply based on AI status ----
        if ai_response.status == AIGenerationStatus.SUCCESS:
            try:
                assistant_msg = service.add_assistant_message(
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
                    "ws.assistant.persist_failed user_id=%s "
                    "conversation_id=%s request_id=%s error=%s",
                    user.id, conversation_id, request_id, str(exc),
                )
                await _send_error(
                    websocket,
                    code="InternalError",
                    message="assistant response could not be persisted",
                    request_id=request_id,
                    conversation_id=conversation_id,
                )
                return

            await _send(
                websocket,
                ServerAssistantMessage(
                    type="assistant_message",
                    request_id=request_id,
                    conversation_id=conversation_id,
                    message_id=int(assistant_msg.id),
                    operation=ai_response.operation or "analyze",
                    model=ai_response.model,
                    answer=ai_response.answer,
                    grounding=ai_response.grounding.model_dump(mode="json"),
                    citations=ai_response.citations or [],
                    warnings=ai_response.warnings or [],
                ).model_dump(mode="json"),
            )
            state.messages_processed += 1
        else:
            # AI failure path: persist a SYSTEM_EVENT row with a
            # sanitized code, emit a controlled error event.  USER
            # message is preserved.
            error_code = _sanitize_failure_code(ai_response.warnings)
            ai_failure_sink.append(error_code)
            try:
                service.add_system_event(
                    user_id=int(user.id),
                    conversation_id=conversation_id,
                    code=error_code,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "ws.system_event.persist_failed user_id=%s "
                    "conversation_id=%s code=%s",
                    user.id, conversation_id, error_code,
                )
            await _send_error(
                websocket,
                code=(
                    "AIDisabled"
                    if ai_response.status == AIGenerationStatus.DISABLED
                    else "AIUnavailable"
                ),
                message=_safe_failure_message(
                    ai_response.status, ai_response.warnings
                ),
                request_id=request_id,
                conversation_id=conversation_id,
            )
            state.ai_failures += 1
            state.messages_processed += 1

        # Remember this request_id AFTER successful handling so the
        # next attempt with the same id hits the dedup branch above.
        state.remember_request_id(request_id)
    finally:
        state.inflight = False
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "ws.user_message.complete user_id=%s conversation_id=%s "
            "request_id=%s duration_ms=%s",
            user.id, conversation_id, request_id, duration_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Stable codes emitted by the LiteLLM client.  The set is duplicated
# here (mirrors app/api/conversations.py) because the WS module is
# self-contained and we don't want a runtime dependency on the
# conversation router module just for these literals.
_LITELLM_KNOWN_CODES = {
    "LITELLM_TIMEOUT",
    "LITELLM_UNAVAILABLE",
    "LITELLM_RATE_LIMIT",
    "LITELLM_AUTH",
    "LITELLM_QUOTA_EXHAUSTED",
    "LITELLM_PROVIDER_ERROR",
    "LITELLM_MALFORMED_RESPONSE",
    "LITELLM_EMPTY_COMPLETION",
}


def _sanitize_failure_code(warnings: list[str]) -> str:
    """Pick a stable, sanitized error code from the AI failure warnings.

    Provider warning lists are untrusted free text; we never persist
    them verbatim.  When no known code matches we fall back to
    ``AI_UNAVAILABLE``.
    """
    if not warnings:
        return "AI_UNAVAILABLE"
    for w in warnings:
        token = (w or "").strip().split(":", 1)[0].strip().upper()
        if token in _LITELLM_KNOWN_CODES:
            return token
    return "AI_UNAVAILABLE"


def _safe_failure_message(
    status: AIGenerationStatus, warnings: list[str]
) -> str:
    """Return a sanitized user-facing message for an AI failure."""
    if status == AIGenerationStatus.DISABLED:
        return (
            "AI generation is disabled in configuration; the user "
            "message was recorded but no assistant response was "
            "generated."
        )
    code = _sanitize_failure_code(warnings)
    if code == "LITELLM_TIMEOUT":
        return "The AI analyst took too long to respond. Please try again."
    if code == "LITELLM_RATE_LIMIT":
        return "The AI analyst is currently rate-limited. Please try again shortly."
    if code == "LITELLM_QUOTA_EXHAUSTED":
        return "The AI analyst is temporarily unavailable (quota)."
    if code == "LITELLM_AUTH":
        return "The AI analyst is temporarily unavailable (auth)."
    if code == "LITELLM_PROVIDER_ERROR":
        return "The AI analyst is temporarily unavailable."
    return "The AI analyst is temporarily unavailable."


__all__ = [
    "router",
    "conversation_socket",
]
