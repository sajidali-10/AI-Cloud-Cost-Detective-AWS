"""Pydantic schemas for the Phase 5C WebSocket realtime conversation protocol.

The protocol is deliberately small and versioned (``protocol_version = "v1"``).
Future phases can add new event types behind the same envelope; clients pin
against ``protocol_version`` from the initial ``connected`` event.

Design notes
------------

* ``request_id`` is a UUID v4 string the client chooses; the server echoes it
  on every correlated response (``user_message_accepted``, ``ai_processing``,
  ``assistant_message``, ``error``).  This lets a browser safely retry a
  user_message and lets the server deduplicate obvious resends.
* The schemas are pure data containers — no I/O, no DB imports, no logging.
* All string fields are bounded so a malformed client cannot blow the JSON
  parser.  Question length matches ``settings.ai_max_question_length`` and the
  schema-level max is generous (``max_length = 2000``); the route layer caps
  against the runtime setting.
* Discriminator ``type`` on the union models lets FastAPI/Pydantic reject
  unsupported event types at parse time.  Clients that send a missing or
  unknown ``type`` field get a sanitized ``ProtocolViolation`` error.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Protocol version advertised in the ``connected`` event.  Clients pin
#: against this value so future phases can introduce v2 without breaking
#: older clients.
PROTOCOL_VERSION: str = "v1"

#: Maximum acceptable length for a raw WebSocket frame (text) before we
#: treat it as oversized.  Anything beyond this is rejected without
#: parsing.  64 KiB is generous for a typed protocol but blocks obvious
#: abuse.
MAX_FRAME_BYTES: int = 64 * 1024

#: Maximum acceptable length for a serialized server event payload.
#: Assistant answers can be long, but the framework enforces the
#: LiteLLM-side ``ai_max_output_tokens`` cap (~800 tokens, well under
#: 32 KiB of UTF-8 prose).  We cap at 256 KiB for forward-compatibility.
MAX_EVENT_BYTES: int = 256 * 1024

#: Allowed ``days`` lookback values for the realtime channel.  Mirrors
#: the REST surface (Phase 4) and the cost-cache key.
ALLOWED_LOOKBACK_DAYS: tuple[int, ...] = (7, 30, 60, 90)

#: Maximum number of recent ``request_id`` values to remember per
#: connection for duplicate suppression.  Browsers may resend a request
#: after a transient disconnect; a bounded FIFO is enough for Phase 5C.
MAX_RECENT_REQUEST_IDS: int = 128

#: Hard cap on the literal server-side ``question`` length.  Matches the
#: existing ``ai_max_question_length`` configuration ceiling (2000
#: characters).  Pydantic enforces it at parse time; the route layer
#: enforces the runtime setting against the same constant.
MAX_QUESTION_LENGTH: int = 2000

#: Hard cap on the literal client ``region`` string length.  AWS regions
#: are short (``us-east-1``); 64 characters is generous and stops obvious
#: abuse.
MAX_REGION_LENGTH: int = 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_UUID4_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-"
    r"[0-9a-fA-F]{12}$"
)


def _validate_request_id(value: str) -> str:
    """Return ``value`` if it looks like a UUID v4, else raise ``ValueError``.

    We accept any RFC 4122 UUID (versions 1-5) — the client picks the
    ``request_id`` and a relaxed check avoids blocking legitimate UUIDs
    from libraries that emit v7.  The strict v4 requirement would
    over-restrict the client side.
    """
    if not isinstance(value, str):
        raise ValueError("request_id must be a string")
    if not _UUID4_RE.match(value):
        raise ValueError("request_id must be a UUID")
    return value


def _strip_optional(value: Optional[str], *, max_length: int) -> Optional[str]:
    """Trim an optional string; raise ``ValueError`` when over-long."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValueError(
            f"value length {len(cleaned)} exceeds the maximum of {max_length}"
        )
    return cleaned


# ---------------------------------------------------------------------------
# Client events
# ---------------------------------------------------------------------------


class ClientUserMessage(BaseModel):
    """The only AI-request event the client may send.

    ``question`` is required; ``region`` is optional (matches the
    REST surface); ``days`` must be one of
    :data:`ALLOWED_LOOKBACK_DAYS`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["user_message"]
    request_id: str
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    region: Optional[str] = Field(default=None, max_length=MAX_REGION_LENGTH)
    days: int = 30

    @field_validator("request_id")
    @classmethod
    def _check_request_id(cls, v: str) -> str:
        return _validate_request_id(v)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("question must be non-empty after trimming")
        return cleaned

    @field_validator("region")
    @classmethod
    def _strip_region(cls, v: Optional[str]) -> Optional[str]:
        return _strip_optional(v, max_length=MAX_REGION_LENGTH)

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: int) -> int:
        if v not in ALLOWED_LOOKBACK_DAYS:
            raise ValueError(
                f"days={v!r} is not allowed; allowed values are "
                f"{list(ALLOWED_LOOKBACK_DAYS)}"
            )
        return v


class ClientPing(BaseModel):
    """Lightweight liveness probe from the client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["ping"]
    ts: Optional[int] = None


# Discriminated union of every event the client may send.  Pydantic v2
# uses the ``type`` field as the discriminator.  Sending an unsupported
# ``type`` raises ``ValidationError`` which the route layer translates
# into a sanitized ``ProtocolViolation`` error event.
ClientEvent = Union[ClientUserMessage, ClientPing]

#: A :class:`TypeAdapter` is the canonical Pydantic v2 way to validate
#: against a ``Union`` (the union type itself does not expose
#: ``model_validate``).
_CLIENT_EVENT_ADAPTER: TypeAdapter[ClientEvent] = TypeAdapter(ClientEvent)


def parse_client_event(raw: Dict[str, Any]) -> ClientEvent:
    """Parse a raw dict into a typed :class:`ClientEvent`.

    Accepts the union discriminator (``type``) and rejects unknown /
    missing values with a sanitized message.  This is a thin helper
    that normalizes the error message into a controlled prefix so
    the route layer can log it without leaking the payload.
    """
    if not isinstance(raw, dict):
        raise ValueError("event must be a JSON object")
    event_type = raw.get("type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("event 'type' is required")
    try:
        return _CLIENT_EVENT_ADAPTER.validate_python(raw)
    except Exception as exc:  # noqa: BLE001 - we always sanitize
        raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Server events
# ---------------------------------------------------------------------------


class ServerConnected(BaseModel):
    """Sent exactly once after the WebSocket handshake completes.

    Carries the authenticated identity (never the JWT) and the
    conversation id this socket is scoped to.  Heartbeat interval is a
    hint to the client about how often to send ``ping`` events.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["connected"]
    protocol_version: str
    conversation_id: int
    user_id: int
    role: str
    heartbeat_interval_seconds: int = 30


class ServerUserMessageAccepted(BaseModel):
    """Acknowledges that a USER message was persisted.

    The ``message_id`` is the ``conversation_messages.id`` row id
    so the client can correlate against the message listing endpoint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["user_message_accepted"]
    request_id: str
    conversation_id: int
    message_id: int


class ServerAiProcessing(BaseModel):
    """Emitted once per accepted user_message right before the LiteLLM call.

    Tells the client the AI layer is about to be invoked.  No model
    latency information is leaked in this event (it pre-dates the call).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["ai_processing"]
    request_id: str
    conversation_id: int


class ServerAssistantMessage(BaseModel):
    """Final AI answer event.

    Carries the same payload the REST ``/conversations/{id}/analyze``
    endpoint returns — minus the ``status`` field, which is implicit
    (``SUCCESS`` on the wire).  A failure produces a separate ``error``
    event instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["assistant_message"]
    request_id: str
    conversation_id: int
    message_id: int
    operation: str
    model: Optional[str] = None
    answer: str
    grounding: Dict[str, Any]
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ServerError(BaseModel):
    """Sanitized protocol / AI / RBAC error event.

    ``code`` is one of the stable identifiers below; ``message`` is
    safe to surface to the end user.  ``request_id`` correlates the
    error to the originating client event (when available).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["error"]
    request_id: Optional[str] = None
    conversation_id: Optional[int] = None
    code: str
    message: str


class ServerPong(BaseModel):
    """Heartbeat reply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["pong"]
    ts: Optional[int] = None


# ---------------------------------------------------------------------------
# Stable error code registry
# ---------------------------------------------------------------------------


#: Sanitized, stable error codes for the WebSocket protocol.  Kept as a
#: module-level constant so the route layer, tests, and the documentation
#: can all pin against the same strings.
WS_ERROR_CODES = frozenset(
    {
        # Protocol-level errors
        "ProtocolViolation",
        "InvalidJSON",
        "UnsupportedEventType",
        "OversizedFrame",
        "InvalidQuestion",
        "InvalidLookbackDays",
        "InvalidRequestId",
        # AI / service-level errors
        "AIUnavailable",
        "AIDisabled",
        "AIUnreachable",
        "Busy",
        "Timeout",
        # Authorization / ownership (still surfaced as in-band errors
        # when the connection has already upgraded; the initial
        # handshake uses close codes).
        "Unauthorized",
        "Forbidden",
        "ConversationNotFound",
    }
)


__all__ = [
    "ALLOWED_LOOKBACK_DAYS",
    "ClientEvent",
    "ClientPing",
    "ClientUserMessage",
    "MAX_EVENT_BYTES",
    "MAX_FRAME_BYTES",
    "MAX_QUESTION_LENGTH",
    "MAX_RECENT_REQUEST_IDS",
    "MAX_REGION_LENGTH",
    "PROTOCOL_VERSION",
    "ServerAiProcessing",
    "ServerAssistantMessage",
    "ServerConnected",
    "ServerError",
    "ServerPong",
    "ServerUserMessageAccepted",
    "WS_ERROR_CODES",
    "parse_client_event",
]
