"""Phase 5A verifier: forged role claim cannot escalate.

Copied into the backend container by scripts/phase5a_verify.sh.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault(
    "JWT_SECRET",
    "phase5a-verify-secret-0123456789abcdef0123456789abcdef",
)

import jwt as pyjwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
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
    viewer = auth.create_user(
        email="viewer2@verify.example", password="password-1234",
        display_name="V", role="VIEWER",
    )
finally:
    db.close()

settings = get_settings()
forged = pyjwt.encode(
    {
        "sub": str(viewer.id),
        "role": "ADMIN",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": "x",
    },
    settings.jwt_secret,
    algorithm=settings.jwt_algorithm,
)

with TestClient(app) as c:
    r = c.get("/admin/users", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code in (401, 403), r.text
    if r.status_code == 403:
        assert r.json()["error_code"] == "Forbidden"

print("escalation_blocked_ok")
sys.exit(0)
