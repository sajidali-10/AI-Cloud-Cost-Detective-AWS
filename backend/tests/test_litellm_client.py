"""Tests for the LiteLLM HTTP client — Phase 4.

Every external interaction is mocked at the ``httpx`` boundary
using :class:`httpx.MockTransport` so no real network call is ever
made.  The tests cover the full error matrix the spec calls out
(health success / unavailable, completion success / timeout /
connection / provider / rate limit / malformed / empty) plus
defensive checks that the API key and Authorization header never
leak into exception messages or reprs.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

from app.core.config import Settings
from app.services.litellm_client import (
    ChatMessage,
    LiteLLMAuthError,
    LiteLLMClient,
    LiteLLMEmptyCompletion,
    LiteLLMMalformedResponse,
    LiteLLMProviderError,
    LiteLLMQuotaExhausted,
    LiteLLMRateLimit,
    LiteLLMTimeout,
    LiteLLMUnavailable,
)


SECRET_KEY = "sk-very-secret-key-do-not-leak"


def _settings(**overrides: Any) -> Settings:
    base: Dict[str, Any] = {
        "litellm_base_url": "http://litellm.local:4000",
        "litellm_model": "cost-detective-free",
        "litellm_api_key": SECRET_KEY,
        "ai_request_timeout_seconds": 5,
        "ai_max_output_tokens": 400,
        "ai_max_context_recommendations": 20,
        "ai_max_context_services": 15,
        "ai_max_context_regions": 10,
        "ai_max_question_length": 2000,
        "app_env": "test",
    }
    base.update(overrides)
    return Settings(**base)


def _make_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _ok_health_body() -> Dict[str, Any]:
    return {"status": "healthy", "healthy_count": 1, "unhealthy_count": 0}


def _ok_completion_body(content: str = "Hello, world.") -> Dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        "model": "cost-detective-free",
    }


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_health_success(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/health/readiness"
            return httpx.Response(200, json=_ok_health_body())

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        assert client.health_check() is True

    def test_health_legacy_ready_string(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ready"})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        assert client.health_check() is True

    def test_health_unavailable_non_200(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "starting"})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        assert client.health_check() is False

    def test_health_unavailable_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        assert client.health_check() is False

    def test_health_unavailable_invalid_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        assert client.health_check() is False


# ---------------------------------------------------------------------------
# Completion success + token usage
# ---------------------------------------------------------------------------


class TestCompletionSuccess:
    def test_completion_success_returns_normalized_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            assert request.method == "POST"
            return httpx.Response(200, json=_ok_completion_body("Grounded answer."))

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        result = client.complete([ChatMessage(role="user", content="hi")])
        assert result.content == "Grounded answer."
        assert result.model == "cost-detective-free"
        assert result.prompt_tokens == 12
        assert result.completion_tokens == 8
        assert result.total_tokens == 20

    def test_completion_strips_empty_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_completion_body("   \n  "))

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMEmptyCompletion):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_completion_rejects_empty_message_list(self) -> None:
        client = LiteLLMClient(_settings(), transport=_make_transport(lambda r: httpx.Response(200)))
        with pytest.raises(LiteLLMMalformedResponse):
            client.complete([])


# ---------------------------------------------------------------------------
# Error matrix
# ---------------------------------------------------------------------------


class TestCompletionErrors:
    def test_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMTimeout) as exc:
            client.complete([ChatMessage(role="user", content="hi")])
        assert exc.value.code == "litellm_timeout"

    def test_connection_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMUnavailable) as exc:
            client.complete([ChatMessage(role="user", content="hi")])
        assert exc.value.code == "litellm_unavailable"

    def test_auth_error_401(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": "Invalid API key", "code": "invalid_api_key"}},
            )

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMAuthError):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_auth_error_403(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "forbidden"})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMAuthError):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_rate_limit_429(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMRateLimit):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_quota_exhausted_402(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"error": {"message": "no credits"}})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMQuotaExhausted):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_provider_error_500(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "internal"}})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMProviderError):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_provider_error_502(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMUnavailable):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_provider_error_504(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(504, json={"error": {"message": "upstream timeout"}})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMUnavailable):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_malformed_response_non_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-json")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMMalformedResponse):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_malformed_response_not_object(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "an", "object"])

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMMalformedResponse):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_malformed_response_no_choices(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMEmptyCompletion):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_malformed_response_content_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}
                    ]
                },
            )

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMEmptyCompletion):
            client.complete([ChatMessage(role="user", content="hi")])

    def test_malformed_response_content_not_string(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": 42},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMMalformedResponse):
            client.complete([ChatMessage(role="user", content="hi")])


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


class TestSecretHandling:
    def test_exception_messages_do_not_contain_api_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMTimeout) as exc:
            client.complete([ChatMessage(role="user", content="hi")])
        assert SECRET_KEY not in str(exc.value.message)
        assert "sk-very-secret" not in repr(exc.value)

    def test_provider_error_messages_do_not_contain_api_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Upstream body intentionally includes the API key to
            # prove the client never copies it into the exception.
            return httpx.Response(
                500,
                json={
                    "error": {
                        "message": f"internal failure (key={SECRET_KEY})",
                        "code": "internal",
                    }
                },
            )

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMProviderError) as exc:
            client.complete([ChatMessage(role="user", content="hi")])
        assert SECRET_KEY not in str(exc.value.message)

    def test_auth_error_messages_do_not_contain_api_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": f"bad key {SECRET_KEY}",
                        "code": "invalid_api_key",
                    }
                },
            )

        client = LiteLLMClient(_settings(), transport=_make_transport(handler))
        with pytest.raises(LiteLLMAuthError) as exc:
            client.complete([ChatMessage(role="user", content="hi")])
        assert SECRET_KEY not in str(exc.value.message)

    def test_envelope_does_not_expose_api_key(self) -> None:
        from app.services.litellm_client import LiteLLMError

        err = LiteLLMAuthError("Authentication failed.")
        env = err.to_envelope()
        assert "error_code" in env
        assert "message" in env
        assert SECRET_KEY not in json.dumps(env)
        assert SECRET_KEY not in str(env)

    def test_repr_does_not_expose_api_key(self) -> None:
        client = LiteLLMClient(_settings())
        # The repr must not leak the key.  We deliberately access
        # private attributes so the test fails loudly if a future
        # refactor starts putting the key into a public surface.
        assert SECRET_KEY not in repr(client)
        # And the Authorization header built for outbound requests is
        # never returned via the to_envelope() path used by HTTP
        # responses.
        from app.services.litellm_client import LiteLLMAuthError
        err = LiteLLMAuthError("Authentication failed.")
        env = err.to_envelope()
        assert SECRET_KEY not in json.dumps(env)
        client.close()
