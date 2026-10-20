"""Shared pytest fixtures: isolate tests from real env vars / secrets."""
import os
import socket

# Ensure required settings exist BEFORE app imports.
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
# Phase 7D.5 (release hardening) — respect an explicit
# ``POSTGRES_HOST`` from the environment (the canonical test
# wrapper ``scripts/test_backend_release.sh`` sets this to the
# docker-compose service name ``postgres`` when running inside the
# backend container).  When no env var is set, fall back to
# ``localhost`` if it resolves via DNS, otherwise to ``postgres``
# (for in-container use).  We deliberately do NOT hardcode
# ``postgres`` as the unconditional default so direct host
# pytest invocations on the EC2 host do not silently redirect
# to a docker-network-only service name.
def _resolve_default_host() -> str:
    for candidate in ("localhost", "postgres"):
        try:
            socket.getaddrinfo(candidate, 5432, type=socket.SOCK_STREAM)
            return candidate
        except (socket.gaierror, OSError):
            continue
    return "localhost"
if "POSTGRES_HOST" not in os.environ:
    os.environ["POSTGRES_HOST"] = _resolve_default_host()
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_ADMIN_USER", "postgres")
os.environ.setdefault("POSTGRES_ADMIN_PASSWORD", "test")
os.environ.setdefault("COST_DETECTIVE_DB", "cost_detective")
os.environ.setdefault("COST_DETECTIVE_DB_USER", "cost_detective_user")
os.environ.setdefault("COST_DETECTIVE_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_DB", "litellm")
os.environ.setdefault("LITELLM_DB_USER", "litellm_user")
os.environ.setdefault("LITELLM_DB_PASSWORD", "test")
if "LITELLM_HOST" not in os.environ:
    os.environ["LITELLM_HOST"] = _resolve_default_host()
os.environ.setdefault("LITELLM_PORT", "4000")
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-test")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# Tests run regardless of the container's APP_ENV (an operator may
# leave production env vars on the host for the live deployment).
# Conftest forces the dev defaults so unit/integration tests do
# not accidentally trigger production-only behaviour (strict CORS
# allowlist, mandatory WS Origin header, refusal to start with a
# placeholder JWT_SECRET).  Production behaviour is exercised by
# the Phase 8.2 live validator against the real deployment.
os.environ["APP_ENV"] = "development"
os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost"
os.environ["AUTH_ENABLED"] = "false"

# Phase 5A: tests that exercise ``AUTH_ENABLED=true`` need a long,
# non-placeholder JWT secret.  We default ``AUTH_ENABLED`` to False
# so existing Phase 0-4 tests are untouched; tests that opt in set
# ``AUTH_ENABLED=true`` and rely on this secret.
#
# We deliberately OVERWRITE (not ``setdefault``) the operator's
# container env (``AUTH_ENABLED=true`` in production) so Phase 0-4
# tests that pre-date Phase 5A continue to run with anonymous
# access.  Phase 5A opt-in tests use ``monkeypatch.setenv`` which
# scopes the override to a single test.
_TEST_JWT_SECRET = (
    "test-jwt-secret-do-not-use-in-production-"
    "0123456789abcdef0123456789abcdef"
)
os.environ["AUTH_ENABLED"] = "false"
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
from app.core.auth_rate_limiter import reset_auth_rate_limiter_for_tests


@pytest.fixture(autouse=True)
def _reset_phase5a_settings():
    # Phase 8.2 closure: when the backend container is started in
    # production mode (``APP_ENV=production``) the test session
    # inherits those env vars.  Tests must run against the
    # development defaults; production-only behaviour is exercised
    # by the Phase 8.2 live validator against the real deployment.
    os.environ["APP_ENV"] = "development"
    os.environ["CORS_ALLOWED_ORIGINS"] = "http://localhost"
    os.environ["AUTH_ENABLED"] = "false"
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    # Restore the default env vars that Phase 5A tests may have
    # monkey-patched.
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["JWT_SECRET"] = _TEST_JWT_SECRET
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()
    # Phase 8.2 — drop the auth rate-limiter singleton so a test
    # that exercises /api/auth/login does not leak its bucket into
    # the next test.  Starlette TestClient uses "testclient" as the
    # peer IP for every connection, so without this reset the
    # limiter would refuse logins after a handful of tests.
    reset_auth_rate_limiter_for_tests()
