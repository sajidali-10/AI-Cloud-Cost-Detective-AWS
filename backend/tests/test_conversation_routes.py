"""End-to-end tests for the conversation API — Phase 5B.

Pattern mirrors ``tests/test_auth_api.py``:
* in-memory SQLite via :class:`StaticPool`
* TestClient with ``app.dependency_overrides[get_db]``
* each test starts with auth disabled (synthetic admin) so we
  can exercise the AuthDisabled path explicitly

The durable-state endpoints (POST/GET/PATCH/DELETE/POST analyze)
short-circuit with 503 ``AuthDisabled`` when auth is disabled; the
tests below flip ``auth_enabled=true`` to exercise the real flow,
then verify the auth-disabled path returns the controlled response.
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _seed_user(
    sqlite_engine,
    *,
    email: str,
    password: str,
    role: str = "ADMIN",
) -> None:
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
            email=email, password=password, display_name=email.split("@")[0], role=role
        )
    finally:
        db.close()


def _login_token(client: TestClient, *, email: str, password: str) -> str:
    r = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# AuthDisabled path (AUTH_ENABLED=false) — controlled 503 on every endpoint
# ---------------------------------------------------------------------------


def test_auth_disabled_returns_503_on_create(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.post("/conversations", json={"title": "t"})
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["error_code"] == "AuthDisabled"


def test_auth_disabled_returns_503_on_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.get("/conversations")
    assert r.status_code == 503
    assert r.json()["error_code"] == "AuthDisabled"


def test_auth_disabled_returns_503_on_get(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.get("/conversations/1")
    assert r.status_code == 503
    assert r.json()["error_code"] == "AuthDisabled"


def test_auth_disabled_returns_503_on_patch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.patch("/conversations/1", json={"title": "x"})
    assert r.status_code == 503


def test_auth_disabled_returns_503_on_delete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.delete("/conversations/1")
    assert r.status_code == 503


def test_auth_disabled_returns_503_on_analyze(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.post(
        "/conversations/1/analyze", json={"days": 30, "question": "x"}
    )
    assert r.status_code == 503


def test_auth_disabled_returns_503_on_messages(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_auth(monkeypatch)
    r = client.get("/conversations/1/messages")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# RBAC: VIEWER cannot use conversation endpoints at all
# ---------------------------------------------------------------------------


def test_viewer_denied_on_create(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", password="pw-12345678", role="ADMIN")
    _seed_user(sqlite_engine, email="viewer@example.com", password="pw-12345678", role="VIEWER")
    admin_tok = _login_token(client, email="admin@example.com", password="pw-12345678")
    viewer_tok = _login_token(client, email="viewer@example.com", password="pw-12345678")

    # Admin can create.
    r = client.post(
        "/conversations",
        json={"title": "ok"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert r.status_code == 201

    # Viewer is denied.
    r = client.post(
        "/conversations",
        json={"title": "no"},
        headers={"Authorization": f"Bearer {viewer_tok}"},
    )
    assert r.status_code == 403
    assert r.json()["error_code"] == "Forbidden"


def test_viewer_denied_on_analyze(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", password="pw-12345678", role="ADMIN")
    _seed_user(sqlite_engine, email="viewer@example.com", password="pw-12345678", role="VIEWER")
    admin_tok = _login_token(client, email="admin@example.com", password="pw-12345678")
    viewer_tok = _login_token(client, email="viewer@example.com", password="pw-12345678")

    # Create a conversation as admin.
    r = client.post(
        "/conversations",
        json={"title": "ok"},
        headers={"Authorization": f"Bearer {admin_tok}"},
    )
    cid = r.json()["id"]

    r = client.post(
        f"/conversations/{cid}/analyze",
        json={"days": 30, "question": "why?"},
        headers={"Authorization": f"Bearer {viewer_tok}"},
    )
    assert r.status_code == 403
    assert r.json()["error_code"] == "Forbidden"


# ---------------------------------------------------------------------------
# CRUD happy path
# ---------------------------------------------------------------------------


def _admin_token(client: TestClient, sqlite_engine) -> str:
    _seed_user(sqlite_engine, email="admin@example.com", password="pw-12345678", role="ADMIN")
    return _login_token(client, email="admin@example.com", password="pw-12345678")


def test_create_with_title_returns_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    r = client.post(
        "/conversations",
        json={"title": "Q4 review"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "Q4 review"
    assert body["is_archived"] is False
    assert body["id"] > 0


def test_create_with_empty_title_falls_back_to_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    r = client.post(
        "/conversations",
        json={"title": ""},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201
    assert r.json()["title"] == "New Cost Analysis"


def test_create_without_title_uses_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    r = client.post(
        "/conversations",
        json={},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201
    assert r.json()["title"] == "New Cost Analysis"


def test_create_oversized_title_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    r = client.post(
        "/conversations",
        json={"title": "x" * 500},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    assert r.json()["error_code"] == "InvalidTitle"


def test_list_returns_only_owners_conversations(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="alice@example.com", password="pw-12345678", role="ANALYST")
    _seed_user(sqlite_engine, email="bob@example.com", password="pw-12345678", role="ANALYST")
    a_tok = _login_token(client, email="alice@example.com", password="pw-12345678")
    b_tok = _login_token(client, email="bob@example.com", password="pw-12345678")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    # Each user creates a conversation.
    client.post("/conversations", json={"title": "alice-1"}, headers=H(a_tok))
    client.post("/conversations", json={"title": "alice-2"}, headers=H(a_tok))
    client.post("/conversations", json={"title": "bob-1"}, headers=H(b_tok))

    # Alice sees only hers.
    r = client.get("/conversations", headers=H(a_tok))
    assert r.status_code == 200
    titles = sorted(c["title"] for c in r.json()["conversations"])
    assert titles == ["alice-1", "alice-2"]

    # Bob sees only his.
    r = client.get("/conversations", headers=H(b_tok))
    titles = sorted(c["title"] for c in r.json()["conversations"])
    assert titles == ["bob-1"]


def test_get_returns_owned_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    cid = client.post("/conversations", json={"title": "x"}, headers=H(tok)).json()["id"]
    r = client.get(f"/conversations/{cid}", headers=H(tok))
    assert r.status_code == 200
    body = r.json()
    assert body["conversation"]["id"] == cid
    assert body["message_count"] == 0


def test_patch_rename_and_archive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    cid = client.post("/conversations", json={"title": "old"}, headers=H(tok)).json()["id"]

    r = client.patch(
        f"/conversations/{cid}", json={"title": "new"}, headers=H(tok)
    )
    assert r.status_code == 200
    assert r.json()["title"] == "new"

    r = client.patch(
        f"/conversations/{cid}", json={"is_archived": True}, headers=H(tok)
    )
    assert r.status_code == 200
    assert r.json()["is_archived"] is True

    r = client.patch(
        f"/conversations/{cid}", json={"is_archived": False}, headers=H(tok)
    )
    assert r.json()["is_archived"] is False


def test_patch_empty_body_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    cid = client.post("/conversations", json={"title": "x"}, headers=H(tok)).json()["id"]
    r = client.patch(f"/conversations/{cid}", json={}, headers=H(tok))
    assert r.status_code == 400
    assert r.json()["error_code"] == "EmptyPatch"


def test_delete_removes_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    cid = client.post("/conversations", json={"title": "x"}, headers=H(tok)).json()["id"]
    r = client.delete(f"/conversations/{cid}", headers=H(tok))
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    # Subsequent get returns 404.
    r = client.get(f"/conversations/{cid}", headers=H(tok))
    assert r.status_code == 404
    assert r.json()["error_code"] == "ConversationNotFound"


def test_delete_other_users_conversation_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="alice@example.com", password="pw-12345678", role="ANALYST")
    _seed_user(sqlite_engine, email="bob@example.com", password="pw-12345678", role="ANALYST")
    a_tok = _login_token(client, email="alice@example.com", password="pw-12345678")
    b_tok = _login_token(client, email="bob@example.com", password="pw-12345678")
    H = lambda t: {"Authorization": f"Bearer {t}"}

    cid = client.post("/conversations", json={"title": "x"}, headers=H(a_tok)).json()["id"]
    r = client.delete(f"/conversations/{cid}", headers=H(b_tok))
    assert r.status_code == 404
    assert r.json()["error_code"] == "ConversationNotFound"

    # Still owned by alice.
    r = client.get(f"/conversations/{cid}", headers=H(a_tok))
    assert r.status_code == 200


def test_get_other_users_conversation_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="alice@example.com", password="pw-12345678", role="ANALYST")
    _seed_user(sqlite_engine, email="bob@example.com", password="pw-12345678", role="ANALYST")
    a_tok = _login_token(client, email="alice@example.com", password="pw-12345678")
    b_tok = _login_token(client, email="bob@example.com", password="pw-12345678")
    H = lambda t: {"Authorization": f"Bearer {t}"}
    cid = client.post("/conversations", json={"title": "x"}, headers=H(a_tok)).json()["id"]
    r = client.get(f"/conversations/{cid}", headers=H(b_tok))
    assert r.status_code == 404


def test_messages_endpoint_returns_404_for_unknown_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    r = client.get("/conversations/9999/messages", headers=H(tok))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_list_pagination(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    tok = _admin_token(client, sqlite_engine)
    H = lambda t: {"Authorization": f"Bearer {t}"}
    for i in range(5):
        client.post("/conversations", json={"title": f"c{i}"}, headers=H(tok))

    r = client.get("/conversations?limit=2&offset=0", headers=H(tok))
    body = r.json()
    assert body["count"] == 2
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["conversations"]) == 2

    r = client.get("/conversations?limit=2&offset=2", headers=H(tok))
    assert len(r.json()["conversations"]) == 2
    r = client.get("/conversations?limit=2&offset=4", headers=H(tok))
    assert len(r.json()["conversations"]) == 1
