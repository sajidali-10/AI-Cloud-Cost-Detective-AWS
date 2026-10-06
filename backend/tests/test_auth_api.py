"""Tests for the FastAPI auth API — Phase 5A.

Uses an in-memory SQLite DB and FastAPI's TestClient.  Covers
``/api/auth/login``, ``/api/auth/me``, and ``/api/auth/info`` in
both ``AUTH_ENABLED=false`` (backward-compat) and
``AUTH_ENABLED=true`` (enforced) modes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.security import reset_security_core_for_tests
from app.db.models import APP_USER_ROLES, Base
from app.db.session import get_db
from app.main import app
from app.services.password_hasher import PasswordHasher


@pytest.fixture()
def sqlite_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(sqlite_engine) -> Iterator[TestClient]:
    SessionLocal = sessionmaker(
        bind=sqlite_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_admin(
    client: TestClient,
    sqlite_engine,
    *,
    email: str,
    password: str,
    role: str = "ADMIN",
) -> None:
    """Insert a user via the same service the API uses.

    The helper takes the test's :class:`Engine` directly so the API
    and the seed see the same database (the FastAPI app's DB
    dependency is overridden to use this engine by the ``client``
    fixture).
    """
    SessionLocal = sessionmaker(
        bind=sqlite_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    from app.services.auth_service import AuthService

    db = SessionLocal()
    try:
        auth = AuthService(
            db=db,
            hasher=PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1,
                                  hash_length=16, salt_length=8),
        )
        auth.create_user(email=email, password=password, display_name="Admin", role=role)
    finally:
        db.close()


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "JWT_SECRET",
        "test-jwt-secret-0123456789abcdef0123456789abcdef",
    )
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()


def _disable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()


# ---------------------------------------------------------------------------
# /api/auth/info (anonymous, always available)
# ---------------------------------------------------------------------------


def test_info_endpoint_anonymous(client: TestClient) -> None:
    r = client.get("/auth/info")
    assert r.status_code == 200
    body = r.json()
    for k in ("auth_enabled", "issuer", "audience", "algorithm", "access_token_minutes"):
        assert k in body
    # Sanitized: no secrets.
    assert "secret" not in r.text.lower()
    assert "password" not in r.text.lower()


def test_info_endpoint_reports_auth_enabled_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    r = client.get("/auth/info")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is True
    _disable_auth(monkeypatch)
    r = client.get("/auth/info")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is False


# ---------------------------------------------------------------------------
# /api/auth/login
# ---------------------------------------------------------------------------


def test_login_success_returns_bearer_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_admin(client, sqlite_engine, email="alice@example.com", password="password-1234")

    r = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "password-1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"].count(".") == 2
    assert body["expires_in"] > 0
    assert body["user"]["email"] == "alice@example.com"
    assert body["user"]["role"] == "ADMIN"
    # Hash must NEVER appear in the response.
    assert "password_hash" not in r.text
    assert "$argon2id$" not in r.text


def test_login_wrong_password_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_admin(client, sqlite_engine, email="bob@example.com", password="password-1234")

    r = client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "wrong-password"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["status"] == "error"
    assert body["error_code"] == "InvalidCredentials"
    # No enumeration: same message regardless of which check failed.
    assert body["message"] == "invalid credentials"
    assert "no such user" not in body["message"].lower()


def test_login_unknown_email_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    r = client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "password-1234"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == "InvalidCredentials"


def test_login_inactive_user_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_admin(client, sqlite_engine, email="admin@example.com", password="password-1234", role="ADMIN")
    _seed_admin(client, sqlite_engine, email="dave@example.com", password="password-1234", role="VIEWER")
    # Deactivate dave via the admin API.
    admin_token = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "password-1234"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}"}
    listing = client.get("/admin/users", headers=headers).json()
    user_id = next(u["id"] for u in listing["users"] if u["email"] == "dave@example.com")
    patch_resp = client.patch(
        f"/admin/users/{user_id}",
        headers=headers,
        json={"is_active": False},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    r = client.post(
        "/auth/login",
        json={"email": "dave@example.com", "password": "password-1234"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "InvalidCredentials"


def test_login_invalid_email_returns_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    r = client.post(
        "/auth/login",
        json={"email": "not-an-email", "password": "password-1234"},
    )
    # Pydantic validation rejects the email.
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /api/auth/me
# ---------------------------------------------------------------------------


def test_me_returns_current_user_with_valid_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_admin(client, sqlite_engine, email="eve@example.com", password="password-1234")
    token = client.post(
        "/auth/login",
        json={"email": "eve@example.com", "password": "password-1234"},
    ).json()["access_token"]

    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "eve@example.com"
    assert "password_hash" not in r.text


def test_me_rejects_missing_authorization_when_auth_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.json()["error_code"] == "Unauthenticated"


def test_me_rejects_malformed_authorization_when_auth_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    r = client.get("/auth/me", headers={"Authorization": "Basic xyz"})
    assert r.status_code == 401
    r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_me_rejects_expired_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    # Issue a token that has already expired.
    settings = get_settings()
    from app.core.security import SecurityCore

    core = SecurityCore(settings=settings)
    import time

    now = int(time.time())
    import jwt as pyjwt

    expired = pyjwt.encode(
        {
            "sub": "1",
            "role": "ADMIN",
            "iat": now - 3600,
            "exp": now - 60,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "jti": "x",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_me_rejects_tampered_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_admin(client, sqlite_engine, email="fay@example.com", password="password-1234")
    token = client.post(
        "/auth/login",
        json={"email": "fay@example.com", "password": "password-1234"},
    ).json()["access_token"]
    head, mid, sig = token.split(".")
    tampered = f"{head}.{mid}.{sig[:-2]}AA"
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# AUTH_ENABLED=false backward compatibility
# ---------------------------------------------------------------------------


def test_me_works_without_auth_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
    sqlite_engine
) -> None:
    _disable_auth(monkeypatch)
    r = client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["role"] == "ADMIN"
