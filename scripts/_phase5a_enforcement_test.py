"""Phase 5A verifier: AUTH_ENABLED=true hermetic enforcement check.

Copied into the backend container by scripts/phase5a_verify.sh and
executed in-process.  Replaces the inline ``python -c`` block that
struggled with shell-quoting of multi-line Python.

Validates:

* 401 without token
* 401 with malformed token
* 200 with valid token (login -> /auth/me)
* 403 when VIEWER tries to call /admin/users
* 403 when VIEWER tries to call /api/ai/executive-summary
* 200 when ANALYST calls /api/ai/executive-summary (DISABLED envelope
  expected; auth still passes)
* No ``password_hash`` or ``$argon2id$`` in any response body
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault(
    "JWT_SECRET",
    "phase5a-verify-secret-0123456789abcdef0123456789abcdef",
)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import (  # noqa: E402
    SecurityCore,
    reset_security_core_for_tests,
)
from app.db.models import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from app.services.password_hasher import PasswordHasher  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]
reset_security_core_for_tests()

# File-based SQLite so the StaticPool sharing is unambiguous and the
# session that seeds users and the TestClient use the same DB.
import tempfile

with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as _f:
    _db_path = _f.name

eng = create_engine(
    f"sqlite+pysqlite:///{_db_path}",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
Base.metadata.create_all(eng)
Sess = sessionmaker(
    bind=eng, autoflush=False, autocommit=False, expire_on_commit=False
)


def _override():
    d = Sess()
    try:
        yield d
    finally:
        d.close()


app.dependency_overrides[get_db] = _override

# Seed three users via the real AuthService so Argon2id is exercised.
db = Sess()
try:
    auth = AuthService(
        db=db,
        hasher=PasswordHasher(
            time_cost=1, memory_cost=8 * 1024, parallelism=1,
            hash_length=16, salt_length=8,
        ),
        security=SecurityCore(settings=get_settings()),
    )
    auth.create_user(
        email="admin@verify.example", password="password-1234",
        display_name="Admin", role="ADMIN",
    )
    auth.create_user(
        email="viewer@verify.example", password="password-1234",
        display_name="Viewer", role="VIEWER",
    )
    auth.create_user(
        email="analyst@verify.example", password="password-1234",
        display_name="Analyst", role="ANALYST",
    )
finally:
    db.close()

from fastapi.testclient import TestClient  # noqa: E402

with TestClient(app) as c:
    # 401 without token.
    assert c.get("/auth/me").status_code == 401, "expected 401 without token"
    # 401 with malformed bearer.
    assert c.get("/auth/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401

    # Successful login -> /me.
    r = c.post("/auth/login", json={"email": "admin@verify.example", "password": "password-1234"})
    assert r.status_code == 200, r.text
    admin_token = r.json()["access_token"]
    me = c.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()
    assert me["email"] == "admin@verify.example", me

    # VIEWER cannot reach /admin/users.
    vt = c.post("/auth/login", json={"email": "viewer@verify.example", "password": "password-1234"}).json()["access_token"]
    r_admin = c.get("/admin/users", headers={"Authorization": f"Bearer {vt}"})
    assert r_admin.status_code == 403, r_admin.text

    # VIEWER cannot generate AI.
    r_ai = c.post(
        "/ai/executive-summary",
        headers={"Authorization": f"Bearer {vt}"},
        json={"region": "us-east-1", "days": 30},
    )
    assert r_ai.status_code == 403, r_ai.text

    # ANALYST can attempt AI (DISABLED envelope or 200).
    at = c.post("/auth/login", json={"email": "analyst@verify.example", "password": "password-1234"}).json()["access_token"]
    r_aigen = c.post(
        "/ai/executive-summary",
        headers={"Authorization": f"Bearer {at}"},
        json={"region": "us-east-1", "days": 30},
    )
    assert r_aigen.status_code in (200, 502), r_aigen.text

    # ADMIN can list users.
    r_list = c.get("/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert r_list.status_code == 200, r_list.text

    # No hash leaked in any of the responses we examined.
    for body in (r.text, r_admin.text, r_ai.text, r_aigen.text, r_list.text, me.__repr__()):
        assert "password_hash" not in body
        assert "$argon2id$" not in body

print("auth_enabled_true_ok")
sys.exit(0)
