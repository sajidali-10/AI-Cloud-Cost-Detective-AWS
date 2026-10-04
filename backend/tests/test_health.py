"""Health endpoint + config + safe error behavior tests.

No AWS, no LLM calls, no real DB. Dependency failures are simulated via mocks.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "ai-cloud-cost-detective-backend"


def test_health_ready_degraded_when_db_unreachable(monkeypatch, client):
    """When the DB is unreachable, /health/ready must return 503 with sanitized status."""
    import app.main as main

    def fake_db_down():
        return "error"

    async def fake_litellm_ok():
        return "ok"

    monkeypatch.setattr(main, "_check_database", fake_db_down)
    monkeypatch.setattr(main, "_check_litellm", fake_litellm_ok)

    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["components"]["backend"] == "ok"
    assert body["components"]["database"] == "error"
    # Must not leak credentials / connection strings / stack traces.
    text = r.text
    assert "test" not in text  # no test password leaks
    assert "OperationalError" not in text
    assert "postgresql" not in text.lower()


def test_health_ready_degraded_when_litellm_unreachable(monkeypatch, client):
    import app.main as main

    def fake_db_ok():
        return "ok"

    async def fake_litellm_down():
        return "error"

    monkeypatch.setattr(main, "_check_database", fake_db_ok)
    monkeypatch.setattr(main, "_check_litellm", fake_litellm_down)

    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["components"]["litellm"] == "error"


def test_root_returns_service_metadata(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "ai-cloud-cost-detective-backend"
    assert body["phase"] == 0


def test_settings_load_from_env():
    s = get_settings()
    assert s.app_env in ("development", "test")
    assert s.postgres_host  # populated from env
    assert s.litellm_base_url.startswith("http://")


def test_settings_database_url_format():
    s = get_settings()
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.cost_detective_db in s.database_url


def test_cors_origins_parsed_from_comma_string(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a, http://b ,http://c")
    get_settings.cache_clear()
    s = get_settings()
    assert s.cors_allowed_origins_list == ["http://a", "http://b", "http://c"]
    # Cleanup so other tests are not affected.
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    get_settings.cache_clear()


def test_no_aws_credentials_required():
    """Phase 0 must not require any AWS key in env."""
    s = get_settings()
    # No AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY fields exist.
    assert not hasattr(s, "aws_access_key_id")
    assert not hasattr(s, "aws_secret_access_key")


def test_litellm_master_key_optional_in_phase0():
    """Master key may be present but is not validated by app code."""
    s = get_settings()
    # Whatever value is in env, the app never logs or returns it.
    assert isinstance(s.litellm_master_key, str)
