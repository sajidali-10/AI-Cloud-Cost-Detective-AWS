"""Cross-user security tests for the Phase 5B conversation API.

These tests assert the IDOR / horizontal privilege-escalation
defences in the route layer \u2014 the same protections
:mod:`tests.test_conversation_service` covers at the service
layer, but exercised through the real FastAPI routes with
distinct authenticated sessions.
"""
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
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def client(sqlite_engine) -> Iterator[TestClient]:
    SessionLocal = sessionmaker(
        bind=sqlite_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
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


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "JWT_SECRET",
        "test-jwt-secret-0123456789abcdef0123456789abcdef",
    )
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()


def _seed_user(sqlite_engine, email: str, role: str) -> None:
    SessionLocal = sessionmaker(
        bind=sqlite_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    db = SessionLocal()
    try:
        auth = AuthService(
            db=db,
            hasher=PasswordHasher(
                time_cost=1, memory_cost=8 * 1024, parallelism=1,
                hash_length=16, salt_length=8,
            ),
        )
        auth.create_user(
            email=email, password="pw-12345678", display_name=email, role=role
        )
    finally:
        db.close()


def _login(client: TestClient, email: str) -> str:
    r = client.post(
        "/auth/login", json={"email": email, "password": "pw-12345678"}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


def test_cross_user_read_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "x"}, headers=H(a_tok)
    ).json()["id"]

    # Bob cannot read Alice's conversation.
    r = client.get(f"/conversations/{cid}", headers=H(b_tok))
    assert r.status_code == 404
    # The response is sanitized: same error code as a truly missing
    # conversation.
    assert r.json()["error_code"] == "ConversationNotFound"


def test_cross_user_messages_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "x"}, headers=H(a_tok)
    ).json()["id"]
    client.post(
        f"/conversations/{cid}/analyze",
        json={"days": 30, "question": "hi"},
        headers=H(a_tok),
    )

    r = client.get(f"/conversations/{cid}/messages", headers=H(b_tok))
    assert r.status_code == 404


def test_cross_user_update_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "original"}, headers=H(a_tok)
    ).json()["id"]

    # Bob cannot rename Alice's conversation.
    r = client.patch(
        f"/conversations/{cid}", json={"title": "PWNED"}, headers=H(b_tok)
    )
    assert r.status_code == 404

    # Title unchanged.
    r = client.get(f"/conversations/{cid}", headers=H(a_tok))
    assert r.json()["conversation"]["title"] == "original"

    # Bob cannot archive it either.
    r = client.patch(
        f"/conversations/{cid}", json={"is_archived": True}, headers=H(b_tok)
    )
    assert r.status_code == 404
    r = client.get(f"/conversations/{cid}", headers=H(a_tok))
    assert r.json()["conversation"]["is_archived"] is False


def test_cross_user_delete_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "x"}, headers=H(a_tok)
    ).json()["id"]
    r = client.delete(f"/conversations/{cid}", headers=H(b_tok))
    assert r.status_code == 404
    # Still there.
    r = client.get(f"/conversations/{cid}", headers=H(a_tok))
    assert r.status_code == 200


def test_cross_user_analyze_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "x"}, headers=H(a_tok)
    ).json()["id"]
    r = client.post(
        f"/conversations/{cid}/analyze",
        json={"days": 30, "question": "x"},
        headers=H(b_tok),
    )
    assert r.status_code == 404
    # Alice's conversation has zero messages after Bob's failed
    # analyze attempt.
    r = client.get(f"/conversations/{cid}/messages", headers=H(a_tok))
    assert r.json()["messages"] == []


def test_unauthenticated_request_denied_when_auth_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    r = client.post("/conversations", json={"title": "x"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "Unauthenticated"


# ---------------------------------------------------------------------------
# VIEWER cannot bypass via /analyze
# ---------------------------------------------------------------------------


def test_viewer_cannot_use_analyze_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "admin@example.com", "ADMIN")
    _seed_user(sqlite_engine, "viewer@example.com", "VIEWER")
    a_tok = _login(client, "admin@example.com")
    v_tok = _login(client, "viewer@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post(
        "/conversations", json={"title": "x"}, headers=H(a_tok)
    ).json()["id"]
    r = client.post(
        f"/conversations/{cid}/analyze",
        json={"days": 30, "question": "x"},
        headers=H(v_tok),
    )
    assert r.status_code == 403
    # No message was persisted by the failed attempt.
    r = client.get(f"/conversations/{cid}/messages", headers=H(a_tok))
    assert r.json()["messages"] == []


# ---------------------------------------------------------------------------
# List isolation: a user only sees their own conversations
# ---------------------------------------------------------------------------


def test_list_scoped_per_user_even_with_pagination(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable(monkeypatch)
    _seed_user(sqlite_engine, "alice@example.com", "ANALYST")
    _seed_user(sqlite_engine, "bob@example.com", "ANALYST")
    a_tok = _login(client, "alice@example.com")
    b_tok = _login(client, "bob@example.com")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    for i in range(7):
        client.post(
            "/conversations", json={"title": f"a{i}"}, headers=H(a_tok)
        )
    for i in range(3):
        client.post(
            "/conversations", json={"title": f"b{i}"}, headers=H(b_tok)
        )

    # Page through alice's conversations; bob's must never leak.
    seen: list[str] = []
    offset = 0
    while True:
        r = client.get(
            f"/conversations?limit=3&offset={offset}", headers=H(a_tok)
        )
        rows = r.json()["conversations"]
        if not rows:
            break
        seen.extend(c["title"] for c in rows)
        offset += 3
    assert sorted(seen) == [f"a{i}" for i in range(7)]
    assert all(not t.startswith("b") for t in seen)
