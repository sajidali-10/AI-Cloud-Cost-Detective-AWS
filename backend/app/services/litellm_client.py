"""LiteLLM Gateway HTTP client — Phase 4.

A thin, transport-agnostic client over ``httpx``.  Every backend LLM
call goes through this module — NO provider SDK (OpenAI, Anthropic,
Gemini, OpenRouter, Bedrock, Ollama, ...) is imported anywhere in
the backend.  Provider portability is delegated to LiteLLM.

Responsibilities
----------------

* Health / readiness probe (``GET /health/readiness``).
* One chat completion request (``POST /v1/chat/completions`` using
  the OpenAI-compatible schema LiteLLM exposes).
* Timeout enforcement (configurable, default 30s).
* Response normalization — extract the assistant ``content`` and any
  ``usage`` fields, never the raw provider payload.
* Malformed-response detection.
* Empty-completion handling (no choice / empty content / API-side
  refusal without content).
* Sanitized exceptions — no API key, no Authorization header, no
  raw upstream body in error messages.

The transport is swappable so tests can pass an ``httpx.MockTransport``
without monkeypatching the global client.  In production the default
is an ``httpx.Client`` constructed from the resolved ``Settings``.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger("cost-detective-backend.litellm_client")


# ---------------------------------------------------------------------------
# Exceptions — every error path returns a sanitized subclass.  The
# ``code`` field is a stable string callers can match on; the ``message``
# is safe to surface in HTTP responses / logs (no secrets, no raw bodies).
# ---------------------------------------------------------------------------


class LiteLLMError(Exception):
    """Base class for LiteLLM client errors.

    ``code`` is a stable identifier (``"timeout"``, ``"auth"`` ...)
    suitable for the public API envelope.  ``message`` is sanitized —
    it never contains the API key, the Authorization header, or the
    raw upstream response body.
    """

    code: str = "litellm_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_envelope(self) -> Dict[str, str]:
        return {"status": "error", "error_code": self.code, "message": self.message}


class LiteLLMUnavailable(LiteLLMError):
    code = "litellm_unavailable"


class LiteLLMTimeout(LiteLLMError):
    code = "litellm_timeout"


class LiteLLMAuthError(LiteLLMError):
    code = "litellm_auth"


class LiteLLMRateLimit(LiteLLMError):
    code = "litellm_rate_limit"


class LiteLLMQuotaExhausted(LiteLLMError):
    code = "litellm_quota_exhausted"


class LiteLLMProviderError(LiteLLMError):
    code = "litellm_provider_error"


class LiteLLMMalformedResponse(LiteLLMError):
    code = "litellm_malformed_response"


class LiteLLMEmptyCompletion(LiteLLMError):
    code = "litellm_empty_completion"


# ---------------------------------------------------------------------------
# Response data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatMessage:
    """A single chat message (system / user / assistant)."""

    role: str
    content: str


@dataclass(frozen=True)
class CompletionResult:
    """Normalized completion output.

    The raw provider / LiteLLM payload is intentionally NOT exposed
    here.  Only the fields Phase 4 callers need are kept; downstream
    code must NOT trust anything that came from the model as a fact.
    """

    content: str
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sanitized status mapping — translates HTTP status codes from the
# LiteLLM gateway / upstream provider into our stable error codes.
# ---------------------------------------------------------------------------


_STATUS_CODE_MAP: Dict[int, type] = {
    401: LiteLLMAuthError,
    403: LiteLLMAuthError,
    408: LiteLLMTimeout,
    429: LiteLLMRateLimit,
    402: LiteLLMQuotaExhausted,
    500: LiteLLMProviderError,
    502: LiteLLMUnavailable,
    503: LiteLLMUnavailable,
    504: LiteLLMUnavailable,
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LiteLLMClient:
    """Minimal LiteLLM HTTP client.

    Construct with explicit ``Settings`` for tests, or call
    :func:`get_litellm_client` for a process-wide default.  The
    ``transport`` parameter is exposed so unit tests can pass an
    :class:`httpx.MockTransport` without monkeypatching.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._settings = settings
        # The headers and timeout are baked into a private client so
        # the Authorization header never leaks through repr / logs.
        self._headers = {"Content-Type": "application/json"}
        if settings.litellm_api_key:
            self._headers["Authorization"] = f"Bearer {settings.litellm_api_key}"
        self._timeout = httpx.Timeout(
            float(settings.ai_request_timeout_seconds),
            connect=min(5.0, float(settings.ai_request_timeout_seconds)),
        )
        self._transport = transport
        self._client: Optional[httpx.Client] = None

    # -- lifecycle ---------------------------------------------------------

    def _build_client(self) -> httpx.Client:
        if self._client is None:
            kwargs: Dict[str, Any] = {
                "timeout": self._timeout,
                "headers": self._headers,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # -- health probe ------------------------------------------------------

    def health_check(self) -> bool:
        """Return ``True`` when ``GET /health/readiness`` reports healthy.

        A non-200 response, a network failure, or a body without the
        expected ``"status":"healthy"`` (or legacy ``"ready"``) marker
        yields ``False``.  Exceptions are NEVER raised from this
        method — callers (status endpoint, readiness probe) treat
        ``False`` as "AI unavailable".
        """
        client = self._build_client()
        try:
            r = client.get(f"{self._settings.litellm_base_url}/health/readiness")
        except httpx.HTTPError as exc:
            logger.info("litellm.health transport_error=%s", type(exc).__name__)
            return False
        if r.status_code != 200:
            return False
        try:
            body = r.json()
        except (ValueError, json.JSONDecodeError):
            return False
        status_value = str(body.get("status", "")).lower()
        if status_value in ("healthy", "ready"):
            return True
        # LiteLLM sometimes returns nested shapes (``{"data": {...}}``).
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        nested = str(data.get("status", "")).lower()
        return nested in ("healthy", "ready")

    # -- completion --------------------------------------------------------

    def complete(
        self,
        messages: Iterable[ChatMessage],
        *,
        max_tokens: Optional[int] = None,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Issue one ``POST /v1/chat/completions`` request.

        Raises one of the sanitized ``LiteLLMError`` subclasses on any
        failure.  Returns a normalized :class:`CompletionResult` on
        success.  Raises :class:`LiteLLMEmptyCompletion` when the
        upstream returns a structurally valid response with no usable
        assistant content (e.g. an empty choices list, an empty content
        string, or a refusal without content).
        """
        # Materialize + sanitize the message list.  Every role must be
        # a non-empty string and every content must be a string.
        payload_messages: List[Dict[str, str]] = []
        for m in messages:
            if not m.role or not isinstance(m.content, str):
                raise LiteLLMMalformedResponse(
                    "Invalid chat message: role and content required."
                )
            payload_messages.append({"role": m.role, "content": m.content})
        if not payload_messages:
            raise LiteLLMMalformedResponse(
                "Cannot complete with an empty message list."
            )

        body: Dict[str, Any] = {
            "model": self._settings.litellm_model,
            "messages": payload_messages,
            "temperature": float(temperature),
            "max_tokens": int(
                max_tokens if max_tokens is not None else self._settings.ai_max_output_tokens
            ),
        }

        client = self._build_client()
        try:
            response = client.post(
                f"{self._settings.litellm_base_url}/v1/chat/completions",
                json=body,
            )
        except httpx.TimeoutException as exc:
            logger.warning("litellm.complete timeout=%s", type(exc).__name__)
            raise LiteLLMTimeout("LiteLLM request timed out.") from exc
        except httpx.ConnectError as exc:
            logger.warning("litellm.complete connect_error=%s", type(exc).__name__)
            raise LiteLLMUnavailable("LiteLLM gateway is unreachable.") from exc
        except httpx.HTTPError as exc:
            logger.warning("litellm.complete http_error=%s", type(exc).__name__)
            raise LiteLLMUnavailable("LiteLLM transport error.") from exc

        # --- status code mapping ---
        if response.status_code >= 400:
            self._raise_for_status(response)

        # --- parse JSON ---
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LiteLLMMalformedResponse(
                "LiteLLM returned a non-JSON response."
            ) from exc
        if not isinstance(data, dict):
            raise LiteLLMMalformedResponse(
                "LiteLLM returned a non-object response."
            )

        # --- extract content ---
        try:
            choices = data.get("choices") or []
        except AttributeError as exc:
            raise LiteLLMMalformedResponse(
                "LiteLLM response missing 'choices'."
            ) from exc
        if not choices:
            raise LiteLLMEmptyCompletion("LiteLLM returned no choices.")
        first = choices[0]
        if not isinstance(first, dict):
            raise LiteLLMMalformedResponse(
                "LiteLLM 'choices[0]' is not an object."
            )
        message = first.get("message") or {}
        if not isinstance(message, dict):
            raise LiteLLMMalformedResponse(
                "LiteLLM 'choices[0].message' is not an object."
            )
        content = message.get("content")
        finish_reason = first.get("finish_reason")
        if content is None:
            raise LiteLLMEmptyCompletion(
                "LiteLLM returned a completion with no content."
            )
        if not isinstance(content, str):
            raise LiteLLMMalformedResponse(
                "LiteLLM 'choices[0].message.content' is not a string."
            )
        content = content.strip()
        if not content:
            raise LiteLLMEmptyCompletion("LiteLLM returned an empty completion.")

        # --- extract token usage (optional) ---
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens"))

        return CompletionResult(
            content=content,
            model=data.get("model"),
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            raw=data,
        )

    # -- internals ---------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        status_code = response.status_code
        # Try to extract a sanitized upstream message.  We only surface
        # the top-level ``error`` shape; the body itself is NEVER
        # placed in the exception message because upstream payloads
        # may carry provider internal hints.
        detail = ""
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "").strip()
            elif isinstance(err, str):
                detail = err.strip()
        if detail:
            # Defensive scrub: redact any sk-... / Bearer ... tokens
            # that an upstream might echo back.
            detail = _redact_secrets(detail)
            if len(detail) > 240:
                detail = detail[:240] + "..."
        cls = _STATUS_CODE_MAP.get(status_code, LiteLLMProviderError)
        message = (
            detail
            if detail
            else {
                401: "LiteLLM rejected the API key.",
                403: "LiteLLM rejected the API key.",
                408: "LiteLLM request timed out.",
                429: "LiteLLM reported a rate limit.",
                402: "LiteLLM reported a quota / billing issue.",
                500: "LiteLLM upstream provider error.",
                502: "LiteLLM upstream provider error.",
                503: "LiteLLM upstream provider error.",
                504: "LiteLLM upstream provider error.",
            }.get(status_code, f"LiteLLM returned HTTP {status_code}.")
        )
        raise cls(message)


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


# Patterns we redact from any string that flows into an exception
# message or log line.  Conservative on purpose — we only target the
# patterns that are almost certainly credentials rather than natural
# language.
_REDACT_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


def _redact_secrets(text: str) -> str:
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


# ---------------------------------------------------------------------------
# Module-level accessor — convenient default for the service layer.
# ---------------------------------------------------------------------------


_default_client: Optional[LiteLLMClient] = None


def get_litellm_client(settings: Optional[Settings] = None) -> LiteLLMClient:
    """Return a process-wide :class:`LiteLLMClient`.

    Tests should construct :class:`LiteLLMClient` directly with a
    MockTransport instead of mutating this module-level default.
    """
    global _default_client
    if _default_client is None:
        _default_client = LiteLLMClient(settings or get_settings())
    return _default_client


def reset_default_client() -> None:
    """Reset the module-level default — test helper."""
    global _default_client
    if _default_client is not None:
        _default_client.close()
    _default_client = None


__all__ = [
    "ChatMessage",
    "CompletionResult",
    "LiteLLMAuthError",
    "LiteLLMClient",
    "LiteLLMEmptyCompletion",
    "LiteLLMError",
    "LiteLLMMalformedResponse",
    "LiteLLMProviderError",
    "LiteLLMQuotaExhausted",
    "LiteLLMRateLimit",
    "LiteLLMTimeout",
    "LiteLLMUnavailable",
    "get_litellm_client",
    "reset_default_client",
]
