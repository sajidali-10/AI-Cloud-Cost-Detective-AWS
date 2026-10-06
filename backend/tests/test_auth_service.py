"""Tests for the AuthService domain layer — Phase 5A.

Uses an in-memory SQLite database via SQLAlchemy so the tests do
not require Postgres.  The UserNotFound / InvalidCredentials paths
do not need a DB but the CRUD paths do.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.security import SecurityCore
from app.db.models import APP_USER_ROLES, Base
from app.services.auth_service import (
    AuthService,
    InvalidCredentials,
    InvalidRole,
    UserAlreadyExists,
    UserInactive,
    UserNotFound,
    normalise_email,
    validate_role,
)
from app.services.password_hasher import PasswordHasher


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=True,
        jwt_secret="test-jwt-secret-0123456789abcdef0123456789",
        jwt_access_token_minutes=5,
    )


@pytest.fixture()
def hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=1,
        memory_cost=8 * 1024,
        parallelism=1,
        hash_length=16,
        salt_length=8,
    )


@pytest.fixture()
def security(settings: Settings) -> SecurityCore:
    return SecurityCore(settings=settings)


@pytest.fixture()
def auth(session, hasher, security) -> AuthService:
    return AuthService(db=session, hasher=hasher, security=security)


# ---- normalise_email ----


def test_normalise_email_lowercases_and_trims() -> None:
    assert normalise_email("  Admin@Example.COM  ") == "admin@example.com"


def test_normalise_email_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        normalise_email(None)  # type: ignore[arg-type]


# ---- validate_role ----


def test_validate_role_accepts_allowed_values() -> None:
    for r in APP_USER_ROLES:
        assert validate_role(r) == r


def test_validate_role_rejects_unknown() -> None:
    with pytest.raises(InvalidRole):
        validate_role("SUPERUSER")


# ---- create_user ----


def test_create_user_persists_with_hashed_password(
    auth: AuthService, hasher: PasswordHasher, session
) -> None:
    user = auth.create_user(
        email="alice@example.com",
        password="correct horse battery staple",
        display_name="Alice",
        role="ADMIN",
    )
    assert user.id is not None
    assert user.email == "alice@example.com"
    assert user.role == "ADMIN"
    assert user.is_active is True
    # Verify the stored hash matches the plaintext password.
    assert hasher.verify("correct horse battery staple", user.password_hash)
    # Plaintext is NOT stored anywhere.
    assert "correct horse battery staple" != user.password_hash
    # Refresh from DB to ensure commit worked.
    session.expire_all()
    again = auth.get_user_by_id(user.id)
    assert again.email == "alice@example.com"


def test_create_user_rejects_duplicate_email(auth: AuthService) -> None:
    auth.create_user(
        email="bob@example.com",
        password="password-1234",
        display_name="Bob",
        role="VIEWER",
    )
    with pytest.raises(UserAlreadyExists):
        auth.create_user(
            email="Bob@Example.com",  # case-insensitive duplicate
            password="password-1234",
            display_name="Bob 2",
            role="VIEWER",
        )


def test_create_user_rejects_invalid_role(auth: AuthService) -> None:
    with pytest.raises(InvalidRole):
        auth.create_user(
            email="x@example.com",
            password="password-1234",
            display_name="X",
            role="SUPERUSER",
        )


def test_create_user_rejects_empty_display_name(auth: AuthService) -> None:
    with pytest.raises(ValueError):
        auth.create_user(
            email="x@example.com",
            password="password-1234",
            display_name="   ",
            role="VIEWER",
        )


# ---- authenticate ----


def test_authenticate_success(auth: AuthService) -> None:
    user = auth.create_user(
        email="carol@example.com",
        password="password-1234",
        display_name="Carol",
        role="ANALYST",
    )
    found = auth.authenticate(email="carol@example.com", password="password-1234")
    assert found.id == user.id


def test_authenticate_unknown_email_is_invalid_credentials(auth: AuthService) -> None:
    with pytest.raises(InvalidCredentials):
        auth.authenticate(email="nobody@example.com", password="password-1234")


def test_authenticate_wrong_password_is_invalid_credentials(auth: AuthService) -> None:
    auth.create_user(
        email="dan@example.com",
        password="password-1234",
        display_name="Dan",
        role="VIEWER",
    )
    with pytest.raises(InvalidCredentials):
        auth.authenticate(email="dan@example.com", password="wrong-password")


def test_authenticate_inactive_user_is_invalid_credentials(auth: AuthService) -> None:
    user = auth.create_user(
        email="erin@example.com",
        password="password-1234",
        display_name="Erin",
        role="VIEWER",
    )
    auth.update_user(user.id, is_active=False)
    with pytest.raises(InvalidCredentials):
        auth.authenticate(email="erin@example.com", password="password-1234")


def test_authenticate_normalises_email_case(auth: AuthService) -> None:
    auth.create_user(
        email="frank@example.com",
        password="password-1234",
        display_name="Frank",
        role="VIEWER",
    )
    found = auth.authenticate(email="FRANK@example.com", password="password-1234")
    assert found.email == "frank@example.com"


# ---- tokens ----


def test_issue_token_returns_bearer_string(auth: AuthService) -> None:
    user = auth.create_user(
        email="gail@example.com",
        password="password-1234",
        display_name="Gail",
        role="VIEWER",
    )
    token, ttl = auth.issue_token(user)
    assert isinstance(token, str)
    assert token.count(".") == 2  # three JWT segments
    assert ttl == auth.security.settings.jwt_access_token_minutes * 60


def test_resolve_token_round_trip(auth: AuthService) -> None:
    user = auth.create_user(
        email="hank@example.com",
        password="password-1234",
        display_name="Hank",
        role="ADMIN",
    )
    token, _ = auth.issue_token(user)
    claims = auth.resolve_token(token)
    assert claims.sub == str(user.id)
    assert claims.role == "ADMIN"


def test_get_active_user_inactive_raises_user_inactive(auth: AuthService) -> None:
    user = auth.create_user(
        email="ivy@example.com",
        password="password-1234",
        display_name="Ivy",
        role="VIEWER",
    )
    auth.update_user(user.id, is_active=False)
    with pytest.raises(UserInactive):
        auth.get_active_user(user.id)


def test_get_user_by_id_missing_raises_user_not_found(auth: AuthService) -> None:
    with pytest.raises(UserNotFound):
        auth.get_user_by_id(999999)


# ---- update_user ----


def test_update_user_role_and_active(auth: AuthService) -> None:
    user = auth.create_user(
        email="jay@example.com",
        password="password-1234",
        display_name="Jay",
        role="VIEWER",
    )
    updated = auth.update_user(user.id, role="ANALYST", is_active=False)
    assert updated.role == "ANALYST"
    assert updated.is_active is False


def test_update_user_password_rehashes(auth: AuthService, hasher: PasswordHasher) -> None:
    user = auth.create_user(
        email="kim@example.com",
        password="old-password-1234",
        display_name="Kim",
        role="VIEWER",
    )
    old_hash = user.password_hash
    updated = auth.update_user(user.id, password="new-password-5678")
    assert updated.password_hash != old_hash
    assert hasher.verify("new-password-5678", updated.password_hash)
    assert not hasher.verify("old-password-1234", updated.password_hash)


def test_update_user_rejects_short_password(auth: AuthService) -> None:
    user = auth.create_user(
        email="leo@example.com",
        password="password-1234",
        display_name="Leo",
        role="VIEWER",
    )
    with pytest.raises(ValueError):
        auth.update_user(user.id, password="short")


def test_update_user_rejects_empty_display_name(auth: AuthService) -> None:
    user = auth.create_user(
        email="mia@example.com",
        password="password-1234",
        display_name="Mia",
        role="VIEWER",
    )
    with pytest.raises(ValueError):
        auth.update_user(user.id, display_name="   ")


# ---- list_users ----


def test_list_users_returns_all_in_id_order(auth: AuthService) -> None:
    for n in ("a", "b", "c"):
        auth.create_user(
            email=f"{n}@example.com",
            password="password-1234",
            display_name=n,
            role="VIEWER",
        )
    users = auth.list_users()
    assert [u.email for u in users] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
    ]
