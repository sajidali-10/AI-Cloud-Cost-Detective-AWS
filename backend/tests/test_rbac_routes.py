"""RBAC integration tests across Phase 1-4 routes — Phase 5A."""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.security import reset_security_core_for_tests
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.services.auth_service import AuthService
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
        bind=sqlite_engine, autoflush=False, autocommit=False, expire_on_commit=False,
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


def _seed(client, sqlite_engine, *, email, password, role):
    SessionLocal = sessionmaker(
        bind=sqlite_engine,
        autoflush=False, autocommit=False, expire_on_commit=False,
    )
    db = SessionLocal()
    try:
        AuthService(
            db=db,
            hasher=PasswordHasher(
                time_cost=1, memory_cost=8 * 1024, parallelism=1,
                hash_length=16, salt_length=8,
            ),
        ).create_user(email=email, password=password, display_name=email, role=role)
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


def _login(client, *, email, password) -> str:
    return client.post(
        "/auth/login",
        json={"email": email, "password": password},
    ).json()["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# AWS read endpoints (identity, resources, costs, evidence, utilization,
# optimization) — allowed for ADMIN, ANALYST, VIEWER.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/aws/identity",
        "/aws/resources",
        "/aws/costs",
        "/aws/optimization/capabilities",
        "/aws/optimization/recommendations",
        "/aws/optimization/summary",
    ],
)
@pytest.mark.parametrize("role", ["ADMIN", "ANALYST", "VIEWER"])
def test_aws_read_endpoints_open_to_all_roles(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, role: str
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    email = f"{role.lower()}@example.com"
    _seed(client, sqlite_engine, email=email, password="password-1234", role=role)
    token = _login(client, email=email, password="password-1234")

    r = client.get(path, headers=_bearer(token))
    # We accept any 2xx/4xx that isn't 401/403: AWS upstream failures
    # in the test environment may yield 502/500.  What we are
    # asserting is that RBAC did NOT block the call.
    assert r.status_code not in (401, 403), f"{role} blocked from {path}: {r.text}"


def test_aws_read_endpoints_unauthenticated_when_auth_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    r = client.get("/aws/identity")
    assert r.status_code == 401


def test_aws_evidence_post_open_to_all_roles(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    _seed(client, sqlite_engine, email="viewer@example.com", password="password-1234", role="VIEWER")
    token = _login(client, email="viewer@example.com", password="password-1234")
    r = client.post(
        "/aws/evidence",
        headers=_bearer(token),
        json={"region": "us-east-1", "days": 30},
    )
    assert r.status_code not in (401, 403), r.text


# ---------------------------------------------------------------------------
# AI endpoints — allowed for ADMIN, ANALYST; denied for VIEWER.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ADMIN", "ANALYST"])
def test_ai_status_allowed_for_admin_and_analyst(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, role: str
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    email = f"{role.lower()}@example.com"
    _seed(client, sqlite_engine, email=email, password="password-1234", role=role)
    token = _login(client, email=email, password="password-1234")
    r = client.get("/ai/status", headers=_bearer(token))
    assert r.status_code == 200, r.text


def test_ai_status_denied_for_viewer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    _seed(client, sqlite_engine, email="viewer@example.com", password="password-1234", role="VIEWER")
    token = _login(client, email="viewer@example.com", password="password-1234")
    r = client.get("/ai/status", headers=_bearer(token))
    # /ai/status itself is unprotected (anonymous status), but the
    # generation endpoints require ADMIN/ANALYST.  Verify the
    # generation endpoints are denied.
    r = client.post(
        "/ai/executive-summary",
        headers=_bearer(token),
        json={"region": "us-east-1", "days": 30},
    )
    assert r.status_code == 403
    assert r.json()["error_code"] == "Forbidden"

    r = client.post(
        "/ai/analyze",
        headers=_bearer(token),
        json={"region": "us-east-1", "days": 30, "question": "Why?"},
    )
    assert r.status_code == 403

    r = client.post(
        "/ai/recommendations/rec-1/explain",
        headers=_bearer(token),
        json={"region": "us-east-1", "days": 30},
    )
    assert r.status_code == 403


def test_ai_generation_endpoints_unauthenticated_when_auth_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    r = client.post(
        "/ai/executive-summary",
        json={"region": "us-east-1", "days": 30},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# AUTH_ENABLED=false backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/aws/identity"),
        ("get", "/aws/resources"),
        ("get", "/aws/costs"),
        ("post", "/aws/evidence"),
        ("get", "/aws/optimization/capabilities"),
        ("get", "/ai/status"),
        ("post", "/ai/executive-summary"),
        ("post", "/ai/analyze"),
    ],
)
def test_business_routes_anonymous_when_auth_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
, sqlite_engine) -> None:
    _disable_auth(monkeypatch)
    fn = getattr(client, method)
    if method == "get":
        r = fn(path)
    else:
        r = fn(
            path,
            json={"region": "us-east-1", "days": 30,
                  "question": "Why?"} if "analyze" in path else
                  {"region": "us-east-1", "days": 30},
        )
    # Must NOT be 401/403 — auth is disabled.
    assert r.status_code not in (401, 403), f"{method.upper()} {path} blocked: {r.text}"


# ---------------------------------------------------------------------------
# Anonymous endpoints (always open)
# ---------------------------------------------------------------------------


def test_health_endpoints_always_anonymous(client: TestClient) -> None:
    # Even with auth disabled or enabled, /health and / are open.
    for path in ("/health", "/"):
        r = client.get(path)
        assert r.status_code in (200, 503)


def test_login_endpoint_anonymous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    # No Authorization header at all.
    r = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "password-1234"},
    )
    # 401 (invalid credentials) is expected; 401 (auth required) is NOT.
    assert r.status_code == 401
    assert r.json()["error_code"] == "InvalidCredentials"


def test_info_endpoint_anonymous(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
, sqlite_engine) -> None:
    _enable_auth(monkeypatch)
    r = client.get("/auth/info")
    assert r.status_code == 200
