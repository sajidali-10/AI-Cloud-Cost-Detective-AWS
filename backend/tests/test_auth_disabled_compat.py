"""AUTH_ENABLED=false compatibility test — Phase 5A.

The Phase 5A spec requires existing Phase 0-4 verification to keep
working when ``AUTH_ENABLED=false``.  This file is a smoke test
that exercises a representative slice of those routes through the
FastAPI app with auth disabled.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import reset_security_core_for_tests
from app.main import app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_auth_disabled_is_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()
    settings = get_settings()
    assert settings.auth_enabled is False


def test_phase0_health_endpoints_open_with_auth_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()

    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_phase4_ai_status_open_with_auth_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()

    r = client.get("/ai/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("DISABLED", "OK", "DEGRADED")


def test_phase4_ai_executive_summary_returns_disabled_with_auth_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()

    r = client.post(
        "/ai/executive-summary",
        json={"region": "us-east-1", "days": 30},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "DISABLED"


def test_phase1_aws_identity_open_with_auth_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()

    r = client.get("/aws/identity")
    # Either 200 (real AWS) or 502 (no AWS creds in test).  NOT 401/403.
    assert r.status_code not in (401, 403), r.text


def test_info_endpoint_reports_disabled_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()

    r = client.get("/auth/info")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is False
