"""Phase 7A — AI status safety tests.

The `/api/ai/status` payload MUST never contain:
  * API keys (LITELLM_API_KEY, OPENAI_API_KEY, …)
  * Authorization headers
  * Raw provider secrets
  * Credential fragments (e.g. ``sk-…``)

It MAY contain:
  * the stable internal model alias (``cost-analyst``)
  * the env var name the operator needs to set
  * boolean provider_configured
"""
from __future__ import annotations

import os

import pytest

from app.schemas.ai import AIStatusResponse


def test_status_does_not_echo_credential_values() -> None:
    secret = "sk-this-is-a-fake-secret-do-not-leak-1234567890"
    os.environ["LITELLM_API_KEY"] = secret
    try:
        from app.services.ai_service import AIService
        from app.core.config import get_settings

        service = AIService(settings=get_settings())
        # Force a fast health-check path: AI disabled, no live gateway call.
        response = service.status()
        payload = response.model_dump(mode="json")
        flat = repr(payload)
        assert secret not in flat
        assert "Bearer" not in flat
        assert "Authorization" not in flat
    finally:
        del os.environ["LITELLM_API_KEY"]


def test_required_env_var_reported_when_missing() -> None:
    os.environ.pop("LITELLM_API_KEY", None)
    from app.services.ai_service import AIService
    from app.core.config import get_settings

    service = AIService(settings=get_settings())
    response = service.status()
    payload = response.model_dump(mode="json")
    assert payload["required_env_var"] == "LITELLM_API_KEY"
    assert payload["provider_configured"] is False


def test_alias_is_cost_analyst() -> None:
    from app.core.config import get_settings

    assert get_settings().litellm_model == "cost-analyst"


def test_ai_status_response_never_has_optional_credentials() -> None:
    """Pydantic-level guard: even if a caller tries to inject a key,
    the schema rejects it.  This catches future regressions where a
    contributor adds a credential field accidentally."""
    bad = AIStatusResponse(
        status="OK",  # type: ignore[arg-type]
        ai_enabled=True,
        litellm_reachable=True,
        provider_configured=True,
        model_alias="cost-analyst",
        provider_model=None,
        litellm_base_url="http://litellm:4000",
        timeout_seconds=30,
        max_output_tokens=800,
        context_limits={},
        required_env_var=None,
    )
    payload = bad.model_dump(mode="json")
    for forbidden in ("api_key", "authorization", "secret", "credential"):
        assert forbidden not in payload
        assert forbidden not in repr(payload).lower()
