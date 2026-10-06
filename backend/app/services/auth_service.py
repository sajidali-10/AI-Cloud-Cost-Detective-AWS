"""AuthService — Phase 5A.

The single domain layer that:

* authenticates a user against the database;
* issues and verifies access tokens;
* exposes a thin CRUD surface for the admin user-management API.

The HTTP layer (``app.api.auth``, ``app.api.admin_users``) is
intentionally thin: it parses requests, calls one of these
methods, and serialises results.  No password, hash, or token ever
appears in HTTP response bodies.

Operational rules:

* Failed logins return :class:`InvalidCredentials` regardless of
  whether the email was unknown, the password was wrong, or the
  user is inactive.  This is the user-enumeration defence.
* The Argon2id verifier is the only place a password is compared
  to a stored hash.  ``verify`` always returns ``bool``; it never
  raises on bad input.
* The role used for authorization comes from the DATABASE row,
  never from the token claim.  ``require_role`` re-loads the user
  via :meth:`get_active_user`.  The token claim is treated as a
  hint.
* ``create_user`` and ``update_user`` update ``updated_at`` on the
  row.  ``record_successful_login`` updates ``last_login_at``.
* No method logs the password, the hash, or the token.  Logging
  happens at the HTTP boundary where the request id is known.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    SecurityCore,
    TokenClaims,
    TokenExpired,
    TokenInvalid,
    TokenSignatureInvalid,
)
from app.db.models import APP_USER_ROLES, AppUser
from app.services.password_hasher import PasswordHasher

logger = logging.getLogger("cost-detective-backend.auth")


# ----- typed exceptions (sanitized; never include credentials) -----


class AuthError(Exception):
    """Base class for auth service errors.  Never carries credentials."""


class InvalidCredentials(AuthError):
    """Generic login failure (unknown user, wrong password, inactive)."""


class UserAlreadyExists(AuthError):
    """Email is already registered."""


class UserNotFound(AuthError):
    """No user with the requested id."""


class UserInactive(AuthError):
    """User exists but is not active."""


class InvalidRole(AuthError):
    """A role value was not in the allowed set."""


# ----- helpers -----


def normalise_email(email: str) -> str:
    """Lowercase + trim an email so uniqueness is case-insensitive.

    The DB has a UNIQUE constraint on the column; normalising in the
    application layer avoids relying on Postgres-specific collation.
    """
    if not isinstance(email, str):
        raise ValueError("email must be a string")
    return email.strip().lower()


def validate_role(role: str) -> str:
    """Return ``role`` unchanged if it is in :data:`APP_USER_ROLES`."""
    if role not in APP_USER_ROLES:
        raise InvalidRole(
            f"role must be one of {sorted(APP_USER_ROLES)}; got {role!r}"
        )
    return role


# ----- AuthService -----


class AuthService:
    """Domain layer for application-local authentication + RBAC."""

    def __init__(
        self,
        *,
        db: Session,
        hasher: Optional[PasswordHasher] = None,
        security: Optional[SecurityCore] = None,
    ) -> None:
        self._db = db
        self._hasher = hasher or PasswordHasher()
        self._security = security

    # ----- properties (delayed resolution keeps tests simple) -----

    @property
    def hasher(self) -> PasswordHasher:
        return self._hasher

    @property
    def security(self) -> SecurityCore:
        if self._security is None:
            from app.core.security import get_security_core

            self._security = get_security_core()
        return self._security

    # ----- authentication -----

    def authenticate(self, *, email: str, password: str) -> AppUser:
        """Return the active user matching ``email`` + ``password``.

        Every failure mode (unknown user, wrong password, inactive
        user) raises :class:`InvalidCredentials` with a sanitized
        message.  This is the user-enumeration defence.
        """
        norm = normalise_email(email)
        user = self._db.execute(
            select(AppUser).where(AppUser.email == norm)
        ).scalar_one_or_none()
        if user is None:
            # Perform a dummy verify so a timing oracle cannot
            # distinguish "no such user" from "wrong password".
            self._hasher.verify(password, "$argon2id$v=19$m=8,t=1,p=1$"
                                "$AAAAAAAAAAAAAAAAAAAAAA$"
                                "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
            logger.info("auth.login.failed reason=unknown_user")
            raise InvalidCredentials("invalid credentials")
        if not user.is_active:
            logger.info("auth.login.failed user_id=%s reason=inactive", user.id)
            raise InvalidCredentials("invalid credentials")
        if not self._hasher.verify(password, user.password_hash):
            logger.info("auth.login.failed user_id=%s reason=bad_password", user.id)
            raise InvalidCredentials("invalid credentials")
        return user

    def record_successful_login(self, user: AppUser) -> None:
        """Stamp ``last_login_at`` and ``updated_at`` for the user."""
        now = datetime.now(timezone.utc)
        user.last_login_at = now
        user.updated_at = now
        self._db.add(user)
        self._db.commit()

    def issue_token(self, user: AppUser) -> tuple[str, int]:
        """Issue an access token for ``user`` and return ``(token, ttl_seconds)``."""
        token = self.security.issue_access_token(
            user_id=int(user.id),
            role=user.role,
        )
        return token, int(self.security.settings.jwt_access_token_minutes * 60)

    def resolve_token(self, token: str) -> TokenClaims:
        """Decode + verify ``token``.  Typed errors propagate to the API layer."""
        return self.security.decode_token(token)

    def get_user_by_id(self, user_id: int) -> AppUser:
        """Return the user with ``user_id`` or raise :class:`UserNotFound`."""
        user = self._db.get(AppUser, user_id)
        if user is None:
            raise UserNotFound(f"no user with id={user_id}")
        return user

    def get_active_user(self, user_id: int) -> AppUser:
        """Return the active user with ``user_id`` or raise an :class:`AuthError`.

        Used by the FastAPI dependencies to enforce authorization:
        the role used for ``require_role`` comes from this DB row,
        never from the token claim.
        """
        user = self.get_user_by_id(user_id)
        if not user.is_active:
            raise UserInactive(f"user id={user_id} is inactive")
        return user

    # ----- admin user management -----

    def create_user(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        role: str,
    ) -> AppUser:
        """Create a new user.

        Raises :class:`UserAlreadyExists` on a duplicate email.
        """
        norm = normalise_email(email)
        role_v = validate_role(role)
        display_clean = (display_name or "").strip()
        if not display_clean:
            raise ValueError("display_name must be non-empty")
        user = AppUser(
            email=norm,
            password_hash=self._hasher.hash(password),
            display_name=display_clean,
            role=role_v,
            is_active=True,
        )
        self._db.add(user)
        try:
            self._db.commit()
        except IntegrityError as exc:
            self._db.rollback()
            logger.info("admin.users.create.conflict email=%s", norm)
            raise UserAlreadyExists(
                f"user with email={norm!r} already exists"
            ) from exc
        self._db.refresh(user)
        logger.info("admin.users.create user_id=%s role=%s", user.id, user.role)
        return user

    def list_users(self) -> List[AppUser]:
        return list(
            self._db.execute(
                select(AppUser).order_by(AppUser.id.asc())
            ).scalars()
        )

    def update_user(
        self,
        user_id: int,
        *,
        display_name: Optional[str] = None,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        password: Optional[str] = None,
    ) -> AppUser:
        """Apply a partial update to the user identified by ``user_id``."""
        user = self.get_user_by_id(user_id)
        if display_name is not None:
            cleaned = display_name.strip()
            if not cleaned:
                raise ValueError("display_name must be non-empty after trimming")
            user.display_name = cleaned
        if role is not None:
            user.role = validate_role(role)
        if is_active is not None:
            user.is_active = bool(is_active)
        if password is not None:
            if len(password) < 8:
                raise ValueError("password must be at least 8 characters")
            user.password_hash = self._hasher.hash(password)
        user.updated_at = datetime.now(timezone.utc)
        self._db.add(user)
        self._db.commit()
        self._db.refresh(user)
        logger.info(
            "admin.users.update user_id=%s role=%s is_active=%s",
            user.id, user.role, user.is_active,
        )
        return user


# ----- module-level singletons -----

from typing import Generator

from app.db.session import SessionLocal  # noqa: E402  (late import)


def get_auth_service(db=None) -> Generator[AuthService, None, None]:
    """FastAPI dependency: yield an :class:`AuthService` for the request.

    When invoked as a FastAPI dependency (``Depends(get_auth_service)``)
    the framework supplies the request-scoped DB session through the
    ``db`` parameter (FastAPI inspects the dependency callable's
    signature).  Tests that need a custom session can call
    ``get_auth_service(my_db)`` directly.
    """
    if db is None:
        db = SessionLocal()
        own = True
    else:
        own = False
    try:
        yield AuthService(db=db)
    finally:
        if own:
            db.close()


__all__ = [
    "APP_USER_ROLES",
    "AuthError",
    "AuthService",
    "InvalidCredentials",
    "InvalidRole",
    "UserAlreadyExists",
    "UserInactive",
    "UserNotFound",
    "TokenExpired",
    "TokenInvalid",
    "TokenSignatureInvalid",
    "get_auth_service",
    "normalise_email",
    "validate_role",
]
