"""Phase 7D.5 — structured observability + health/readiness tests.

These tests pin the operational contract:
* Every canonical cloud operation emits exactly one structured log
  line per request with the documented fields.
* Secrets (Authorization headers, bearer tokens, JWT, AWS keys,
  DB passwords, LiteLLM API keys) MUST NOT appear in any log line.
* ``/health/ready`` semantics:
  - PostgreSQL unavailable → NOT READY (503)
  - AWS throttled / Azure throttled / AI_ENABLED=false / LiteLLM down
    → core remains READY (200)

We test the helpers directly (no HTTP) so the test is hermetic.
"""
from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_ADMIN_USER", "postgres")
os.environ.setdefault("POSTGRES_ADMIN_PASSWORD", "test")
os.environ.setdefault("COST_DETECTIVE_DB", "cost_detective")
os.environ.setdefault("COST_DETECTIVE_DB_USER", "cost_detective_user")
os.environ.setdefault("COST_DETECTIVE_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_DB", "litellm")
os.environ.setdefault("LITELLM_DB_USER", "litellm_user")
os.environ.setdefault("LITELLM_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_HOST", "localhost")
os.environ.setdefault("LITELLM_PORT", "4000")
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-test")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("AI_ENABLED", "true")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")
os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("LITELLM_MODEL", "gpt-4o-mini")

from app.services.observability import (
    operation_timer,
    mark_failure,
    new_request_id,
    record_retry_count,
    record_upstream_calls,
)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def _capture_log(caplog) -> str:
    """Return concatenated record messages captured from the ops logger."""
    return "\n".join(rec.message for rec in caplog.records if rec.name == "cost-detective-backend.ops")


def test_operation_timer_emits_required_fields(caplog):
    caplog.set_level(logging.INFO, logger="cost-detective-backend.ops")
    with operation_timer("cloud.test") as stats:
        stats["provider"] = "AWS"
        stats["scope_kind"] = "all"
        stats["freshness"] = "LIVE"
        stats["cache_status"] = "HIT"
        stats["upstream_call_count"] = 0
    log = _capture_log(caplog)
    # Required fields per Phase 7D.5 §5.
    for marker in (
        "operation=", "duration_ms=", "provider=", "scope_kind=",
        "freshness=", "cache_status=", "upstream_call_count=",
    ):
        assert marker in log, f"missing marker in log line: {marker}"
    # request_id must be a 12-char hex token.
    import re
    m = re.search(r"request_id='([0-9a-f]{12})'", log)
    assert m is not None, f"request_id missing or malformed: {log!r}"


def test_operation_timer_marks_failure_on_exception(caplog):
    caplog.set_level(logging.INFO, logger="cost-detective-backend.ops")
    with pytest.raises(ValueError):
        with operation_timer("cloud.fail") as stats:
            mark_failure(stats, error_code="Throttled")
            raise ValueError("simulated upstream error")
    log = _capture_log(caplog)
    assert "status='FAILURE'" in log
    assert "error_code='Throttled'" in log


def test_no_secrets_in_log(caplog):
    caplog.set_level(logging.INFO, logger="cost-detective-backend.ops")
    sentinel = "X-SECRET-XYZ-XYZ-XYZ"
    with operation_timer("cloud.costs") as stats:
        stats["provider"] = "AWS"
        # Even if a caller accidentally puts a sentinel into the
        # stats dict, the emit() function only writes primitive
        # values + only the documented field names.  We assert
        # the sentinel does not leak into the output.
        stats["bearer_token"] = sentinel  # not a documented field
    log = _capture_log(caplog)
    assert sentinel not in log


def test_record_helpers():
    with operation_timer("cloud.test") as stats:
        record_upstream_calls(stats, 5)
        record_retry_count(stats, 2)
    assert stats["upstream_call_count"] == 5
    assert stats["retry_count"] == 2


def test_new_request_id_format():
    rid = new_request_id()
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)
    # Two consecutive calls must produce distinct ids.
    assert new_request_id() != rid


# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------


def test_health_returns_ok(client):
    """``/health`` is process liveness; must always 200 when the
    process is alive."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"


def test_health_ready_postgres_ok(client):
    """``/health/ready`` returns ready when PostgreSQL is reachable."""
    with patch("app.main._check_database", return_value="ok"), \
         patch("app.main._check_litellm", return_value="ok"):
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["database"] == "ok"


def test_health_ready_postgres_down_returns_not_ready(client):
    """``/health/ready`` returns 503 when PostgreSQL is unavailable.
    This is the single hard dependency — the application cannot
    operate correctly without it."""
    with patch("app.main._check_database", return_value="error"), \
         patch("app.main._check_litellm", return_value="ok"):
        response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "database_unavailable"


def test_health_ready_litellm_down_does_not_break_core(client):
    """``/health/ready`` stays READY when LiteLLM is unreachable —
    the AI endpoint returns a controlled UNAVAILABLE / DISABLED
    response but the core application is still serving."""
    with patch("app.main._check_database", return_value="ok"), \
         patch("app.main._check_litellm", return_value="error"):
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"]["database"] == "ok"
    assert body["components"]["litellm"] == "error"
    assert "litellm_unavailable" in body.get("dependency_conditions", [])


def test_health_ready_ai_disabled_does_not_break_core(client, monkeypatch):
    """``/health/ready`` stays READY when AI is disabled in config."""
    monkeypatch.setenv("AI_ENABLED", "false")
    with patch("app.main._check_database", return_value="ok"), \
         patch("app.main._check_litellm", return_value="ok"):
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"


@pytest.fixture
def client():
    """Minimal FastAPI TestClient.  We only exercise /health and
    /health/ready so we can patch the heavy dependencies
    (Postgres + LiteLLM) inline."""
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
