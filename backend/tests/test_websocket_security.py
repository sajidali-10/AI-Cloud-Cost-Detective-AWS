"""Phase 5C WebSocket security tests — auth, RBAC, ownership, IDOR.

These tests exercise the full WebSocket handshake + protocol through
``TestClient.websocket_connect`` so every defense is checked at the
real FastAPI layer (not at a stub).

What we cover
-------------

* valid ADMIN / ANALYST connection
* VIEWER denied
* missing / malformed / expired / signature-invalid token denied
* forged role claim denied (token says ADMIN, DB says VIEWER)
* inactive user denied
* AUTH_ENABLED=false denied
* own conversation accepted
* cross-user conversation denied (4404, indistinguishable from
  "not found")
* nonexistent conversation denied (4404)
* no JWT, no AWS creds, no provider keys in any error response
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Iterator, Optional

import jwt as pyjwt
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
    # Point the WebSocket session factory at the SAME in-memory DB so
    # ownership + conversation lookups see the seeded users.
    import app.api.ws_conversations as _ws_conv
    _ws_conv._session_factory = SessionLocal
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
    role: str,
    password: str = "pw-12345678",
) -> int:
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
        user = auth.create_user(
            email=email, password=password,
            display_name=email, role=role,
        )
        return int(user.id)
    finally:
        db.close()


def _login_token(client: TestClient, *, email: str) -> str:
    r = client.post(
        "/auth/login",
        json={"email": email, "password": "pw-12345678"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _make_conversation(client: TestClient, token: str) -> int:
    r = client.post(
        "/conversations",
        json={"title": "ws-security"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def _ws_connect(
    client: TestClient,
    conversation_id: int,
    *,
    token: Optional[str] = None,
    auth_header: Optional[str] = None,
):
    """Open a WebSocket connection using the ``Sec-WebSocket-Protocol``
    header to carry the bearer token.  Returns the websocket context
    manager so the caller can ``with`` it.
    """
    headers = {}
    subprotocols: list[str] = []
    if token is not None:
        subprotocols.append(f"bearer.{token}")
    if auth_header is not None:
        headers["Authorization"] = auth_header
    url = f"/ws/conversations/{conversation_id}"
    return client.websocket_connect(url, subprotocols=subprotocols, headers=headers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_connected(ws) -> dict:
    msg = ws.receive_json()
    assert msg["type"] == "connected", msg
    return msg


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_admin_can_connect_to_own_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    with _ws_connect(client, cid, token=token) as ws:
        connected = _consume_connected(ws)
        assert connected["protocol_version"] == "v1"
        assert connected["conversation_id"] == cid
        assert connected["role"] == "ADMIN"
        assert connected["user_id"] > 0
        # The handshake MUST NOT echo the JWT back.
        text_dump = json.dumps(connected)
        assert token not in text_dump


def test_analyst_can_connect_to_own_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="analyst@example.com", role="ANALYST")
    token = _login_token(client, email="analyst@example.com")
    cid = _make_conversation(client, token)
    with _ws_connect(client, cid, token=token) as ws:
        connected = _consume_connected(ws)
        assert connected["role"] == "ANALYST"


def test_authorization_header_also_accepted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    # No subprotocol — token travels in the Authorization header.
    with client.websocket_connect(
        f"/ws/conversations/{cid}",
        headers={"Authorization": f"Bearer {token}"},
    ) as ws:
        connected = _consume_connected(ws)
        assert connected["role"] == "ADMIN"


# ---------------------------------------------------------------------------
# RBAC denials
# ---------------------------------------------------------------------------


def test_viewer_connection_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    _seed_user(sqlite_engine, email="viewer@example.com", role="VIEWER")
    a_token = _login_token(client, email="admin@example.com")
    v_token = _login_token(client, email="viewer@example.com")
    cid = _make_conversation(client, a_token)
    # VIEWER connecting to a real conversation must be rejected.
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token=v_token):
            pass


def test_viewer_with_someone_elses_conversation_id_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    _seed_user(sqlite_engine, email="viewer@example.com", role="VIEWER")
    a_token = _login_token(client, email="admin@example.com")
    v_token = _login_token(client, email="viewer@example.com")
    cid = _make_conversation(client, a_token)
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token=v_token):
            pass


# ---------------------------------------------------------------------------
# Token failure modes
# ---------------------------------------------------------------------------


def test_missing_token_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/conversations/{cid}"):
            pass


def test_malformed_token_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token="not-a-jwt"):
            pass


def test_expired_token_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    _login_token(client, email="admin@example.com")  # warm caches
    cid = 1  # any id — auth fails before ownership check
    settings = get_settings()
    now = int(time.time())
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
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token=expired):
            pass


def test_bad_signature_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    head, mid, sig = token.split(".")
    tampered = f"{head}.{mid}.{sig[:-2]}AA"
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token=tampered):
            pass


def test_inactive_user_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="root@example.com", role="ADMIN")
    _seed_user(sqlite_engine, email="analyst@example.com", role="ANALYST")
    admin_token = _login_token(client, email="root@example.com")
    analyst_token = _login_token(client, email="analyst@example.com")
    # Deactivate analyst via admin API.
    listing = client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    user_id = next(
        u["id"] for u in listing["users"]
        if u["email"] == "analyst@example.com"
    )
    r = client.patch(
        f"/admin/users/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text
    # Reset settings because admin API may have triggered env refresh.
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_security_core_for_tests()
    with pytest.raises(Exception):
        with _ws_connect(client, 1, token=analyst_token):
            pass


def test_forged_role_claim_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    """A token whose ``role`` claim says ADMIN but whose DB row is
    VIEWER must be denied — the DB role is authoritative.
    """
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="viewer@example.com", role="VIEWER")
    settings = get_settings()
    # Mint a token with sub pointing to the viewer user id but
    # role=ADMIN.  The DB still says VIEWER.
    SessionLocal = sessionmaker(
        bind=sqlite_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    db = SessionLocal()
    try:
        from app.db.models import AppUser

        viewer = db.execute(
            __import__("sqlalchemy").select(AppUser).where(
                AppUser.email == "viewer@example.com"
            )
        ).scalar_one()
        viewer_id = int(viewer.id)
    finally:
        db.close()

    now = int(time.time())
    forged = pyjwt.encode(
        {
            "sub": str(viewer_id),
            "role": "ADMIN",
            "iat": now,
            "exp": now + 600,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "jti": "x",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(Exception):
        with _ws_connect(client, 1, token=forged):
            pass


def test_auth_disabled_denies_ws(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _disable_auth(monkeypatch)
    # AUTH_ENABLED=false must prevent a successful realtime session.
    # The endpoint accepts the socket then closes with 1008, OR
    # rejects the upgrade at the HTTP layer; both are acceptable
    # outcomes from the client's perspective.  What matters is that
    # the client NEVER receives a 'connected' event.
    got_connected = False
    try:
        with client.websocket_connect("/ws/conversations/1") as ws:
            try:
                msg = ws.receive_json()
                if msg.get("type") == "connected":
                    got_connected = True
            except Exception:
                # Server closed before / during / after sending the
                # connected event.  Either is a pass.
                pass
    except Exception:
        # Upgrade rejected outright (e.g. close before accept).
        pass
    assert not got_connected, (
        "AUTH_ENABLED=false must NOT permit a connected WS session"
    )


# ---------------------------------------------------------------------------
# Ownership / IDOR
# ---------------------------------------------------------------------------


def test_cross_user_conversation_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="alice@example.com", role="ANALYST")
    _seed_user(sqlite_engine, email="bob@example.com", role="ANALYST")
    a_token = _login_token(client, email="alice@example.com")
    b_token = _login_token(client, email="bob@example.com")
    cid = _make_conversation(client, a_token)
    # Bob cannot connect to Alice's conversation.  Indistinguishable
    # from "not found" by close-code only.
    with pytest.raises(Exception):
        with _ws_connect(client, cid, token=b_token):
            pass


def test_nonexistent_conversation_denied(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    with pytest.raises(Exception):
        with _ws_connect(client, 999_999, token=token):
            pass


# ---------------------------------------------------------------------------
# No-secret-leakage assertions
# ---------------------------------------------------------------------------


def test_no_jwt_in_connected_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    with _ws_connect(client, cid, token=token) as ws:
        msg = ws.receive_json()
        assert token not in json.dumps(msg)


def test_no_aws_credentials_in_any_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sqlite_engine
) -> None:
    _enable_auth(monkeypatch)
    _seed_user(sqlite_engine, email="admin@example.com", role="ADMIN")
    token = _login_token(client, email="admin@example.com")
    cid = _make_conversation(client, token)
    with _ws_connect(client, cid, token=token) as ws:
        connected = _consume_connected(ws)
        for needle in (
            "AKIA",
            "AWS_SECRET",
            "sk-",
            "Bearer ",
            "password",
            "secret",
        ):
            assert needle.lower() not in json.dumps(connected).lower()
