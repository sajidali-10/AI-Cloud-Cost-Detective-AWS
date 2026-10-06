"""Logging safety tests — Phase 5A.

Asserts that the auth layer does NOT log passwords, JWTs, password
hashes, or the JWT secret.  These tests use a captured log handler
so they're hermetic and don't require the application to be
running.
"""
from __future__ import annotations

import logging
import re

import pytest

from app.core.config import Settings


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=True,
        jwt_secret="phase5a-test-jwt-secret-0123456789abcdef",
        jwt_access_token_minutes=5,
    )


def _make_settings() -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=True,
        jwt_secret="phase5a-test-jwt-secret-0123456789abcdef",
        jwt_access_token_minutes=5,
    )
from app.core.security import SecurityCore
from app.services.auth_service import AuthService, InvalidCredentials
from app.services.password_hasher import PasswordHasher


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
HASH_RE = re.compile(r"\$argon2[^\s]{8,}")


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def captured_logs():
    handler = _CapturingHandler()
    root = logging.getLogger("cost-detective-backend")
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)


def _format(record: logging.LogRecord) -> str:
    return record.getMessage()


def test_jwt_issuance_does_not_log_token(
    captured_logs: _CapturingHandler, settings: Settings
) -> None:
    core = SecurityCore(settings=settings)
    token = core.issue_access_token(user_id=1, role="ADMIN")
    # Trigger decode to add some log noise.
    core.decode_token(token)
    blob = "\n".join(_format(r) for r in captured_logs.records)
    assert JWT_RE.search(blob) is None
    # The JWT secret must not appear either.
    assert settings.jwt_secret not in blob


def test_failed_login_does_not_log_password(
    captured_logs: _CapturingHandler, settings: Settings
) -> None:
    SessionLocal = _FakeSession()
    auth = AuthService(
        db=SessionLocal,  # type: ignore[arg-type]
        hasher=PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1,
                              hash_length=16, salt_length=8),
        security=SecurityCore(settings=settings),
    )
    with pytest.raises(InvalidCredentials):
        auth.authenticate(email="ghost@example.com", password="supersecretpassword")

    blob = "\n".join(_format(r) for r in captured_logs.records)
    assert "supersecretpassword" not in blob
    assert HASH_RE.search(blob) is None


def test_successful_login_does_not_log_password_or_hash(
    captured_logs: _CapturingHandler, settings: Settings
) -> None:
    # Seed an in-memory user.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db.models import Base

    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        auth = AuthService(
            db=db,
            hasher=PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1,
                                  hash_length=16, salt_length=8),
            security=SecurityCore(settings=settings),
        )
        user = auth.create_user(
            email="alice@example.com",
            password="correct-password-1234",
            display_name="Alice",
            role="ADMIN",
        )
        # Clear the captured records from the create_user call.
        captured_logs.records.clear()
        result = auth.authenticate(email="alice@example.com", password="correct-password-1234")
        assert result.id == user.id

        blob = "\n".join(_format(r) for r in captured_logs.records)
        assert "correct-password-1234" not in blob
        assert HASH_RE.search(blob) is None
        assert user.password_hash not in blob
    finally:
        db.close()


class _FakeSession:
    """Minimal stub returning no rows — enough for the
    ``authenticate`` unknown-email path.
    """

    def execute(self, _statement):
        class _R:
            def scalar_one_or_none(self_inner):
                return None
        return _R()
