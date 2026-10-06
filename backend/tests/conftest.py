"""Shared pytest fixtures: isolate tests from real env vars / secrets."""
import os

# Ensure required settings exist BEFORE app imports.
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

# Phase 5A: tests that exercise ``AUTH_ENABLED=true`` need a long,
# non-placeholder JWT secret.  We default ``AUTH_ENABLED`` to False
# so existing Phase 0-4 tests are untouched; tests that opt in set
# ``AUTH_ENABLED=true`` and rely on this secret.
_TEST_JWT_SECRET = (
    "test-jwt-secret-do-not-use-in-production-"
    "0123456789abcdef0123456789abcdef"
)
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("JWT_SECRET", _TEST_JWT_SECRET)


# ---------------------------------------------------------------------------
# Test isolation: reset cached settings + monkyepatched env vars after
# every test.  Phase 5A tests ``monkeypatch.setenv("AUTH_ENABLED",
# "true")``; if the change leaked into subsequent Phase 0-4 tests
# (which use the default ``false``), the latter would see a stale
# cached Settings and fail.  Clearing the lru_cache and restoring
# the env var between tests keeps each test hermetic.
# ---------------------------------------------------------------------------


import pytest

from app.core.config import get_settings
from app.core.security import reset_security_core_for_tests


@pytest.fixture(autouse=True)
def _reset_phase5a_settings():
    yield
    # Restore the default env vars that Phase 5A tests may have
    # monkey-patched.
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["JWT_SECRET"] = _TEST_JWT_SECRET
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()
