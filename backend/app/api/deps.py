"""FastAPI dependencies for authentication + RBAC.

These are the only place where JWT parsing and role enforcement
happen.  Business routes import :func:`require_role`,
:func:`require_admin`, etc. and never inspect the token directly.

Two states are supported via ``settings.auth_enabled``:

* **AUTH_ENABLED=false** (Phase 0-4 default): the dependencies
  short-circuit and never inspect the Authorization header.  A
  synthetic ``AppUser`` with ``role='ADMIN'`` is returned so that
  the existing Phase 0-4 verification suite keeps working
  unchanged.  A warning is logged once at startup so operators
  notice the disabled state.

* **AUTH_ENABLED=true** (Phase 5A target): every business request
  must carry a valid bearer token.  Inactive users are rejected,
  the user row is re-loaded from the DB on every request, and the
  role used for authorization is the DB row's role — the token
  claim is treated as a hint only.

Errors are surfaced via the existing
``{"status":"error","error_code":...,"message":...}`` envelope.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Iterable

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.services.auth_service import (
    AuthService,
    InvalidCredentials,
    TokenExpired,
    TokenInvalid,
    TokenSignatureInvalid,
    UserInactive,
    UserNotFound,
)

logger = logging.getLogger("cost-detective-backend.auth_deps")


# ---------------------------------------------------------------------------
# Synthetic admin used when AUTH_ENABLED=false
# ---------------------------------------------------------------------------


# A module-level sentinel so we can return a stable id without ever
# hitting the DB.  This user NEVER persists to ``app_users``.
_SYNTHETIC_ADMIN_USER_ID = -1
_SYNTHETIC_ADMIN_EMAIL = "anonymous+disabled-auth@local.invalid"


def _synthetic_admin_user() -> AppUser:
    now = datetime.now(timezone.utc)
    user = AppUser(
        id=_SYNTHETIC_ADMIN_USER_ID,
        email=_SYNTHETIC_ADMIN_EMAIL,
        password_hash="",  # never serialised; never compared
        display_name="Anonymous (auth disabled)",
        role="ADMIN",
        is_active=True,
        created_at=now,
        updated_at=now,
        last_login_at=None,
    )
    return user


# Track whether we've emitted the "auth disabled" warning so we
# don't spam the log on every request.
_disabled_warning_emitted = False


def _maybe_warn_auth_disabled() -> None:
    global _disabled_warning_emitted
    if _disabled_warning_emitted:
        return
    _disabled_warning_emitted = True
    logger.warning(
        "AUTH_ENABLED=false: business APIs accept anonymous requests. "
        "Set AUTH_ENABLED=true in production."
    )


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "status": "error",
            "error_code": "Unauthenticated",
            "message": message,
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "status": "error",
            "error_code": "Forbidden",
            "message": message,
        },
    )


def _service_unavailable(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "status": "error",
            "error_code": "AuthDisabled",
            "message": message,
        },
    )


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _unauthorized("Authorization header must be 'Bearer <token>'")
    return parts[1].strip()


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    db = Depends(get_db),
) -> AppUser:
    auth = AuthService(db=db)
    """Return the authenticated :class:`AppUser` for this request.

    * AUTH_ENABLED=false -> synthetic ADMIN (backward-compat).
    * AUTH_ENABLED=true  -> verify bearer, look up the user, ensure
      they are active.
    """
    if not settings.auth_enabled:
        _maybe_warn_auth_disabled()
        return _synthetic_admin_user()

    token = _extract_bearer(authorization)
    try:
        claims = auth.resolve_token(token)
    except TokenExpired:
        raise _unauthorized("token expired")
    except TokenSignatureInvalid:
        raise _unauthorized("token signature invalid")
    except TokenInvalid:
        raise _unauthorized("token invalid")

    try:
        user_id = int(claims.sub)
    except (TypeError, ValueError):
        raise _unauthorized("token subject is not a valid user id")

    try:
        user = auth.get_active_user(user_id)
    except UserNotFound:
        raise _unauthorized("user no longer exists")
    except UserInactive:
        raise _forbidden("user is inactive")

    # Stash the verified token claims on the request for any
    # downstream consumer (audit logging in Phase 5B/5C, for
    # example).  We never log the token itself.
    request.state.token_claims = claims
    request.state.user_id = int(user.id)
    request.state.role = user.role
    return user


def require_role(*allowed: str) -> Callable[[AppUser], AppUser]:
    """Build a dependency that asserts the caller's role is in ``allowed``.

    Usage::

        @router.get("/x", dependencies=[Depends(require_role("ADMIN", "ANALYST"))])

    or as the parameter annotation directly::

        def handler(user: AppUser = Depends(require_role("ADMIN"))): ...
    """
    allowed_set = frozenset(allowed)

    def _dep(user: AppUser = Depends(get_current_user)) -> AppUser:
        if user.role not in allowed_set:
            logger.info(
                "rbac.denied user_id=%s role=%s required=%s",
                user.id, user.role, sorted(allowed_set),
            )
            raise _forbidden(
                f"role {user.role!r} is not permitted on this endpoint"
            )
        return user

    return _dep


def require_admin(user: AppUser = Depends(get_current_user)) -> AppUser:
    """Convenience dependency: require ADMIN role."""
    if user.role != "ADMIN":
        logger.info(
            "rbac.denied user_id=%s role=%s required=ADMIN",
            user.id, user.role,
        )
        raise _forbidden("ADMIN role required")
    return user


# ---------------------------------------------------------------------------
# Helpers for the auth API itself
# ---------------------------------------------------------------------------


def login_error_response(exc: Exception) -> JSONResponse:
    """Translate a :class:`InvalidCredentials` into a sanitized 401.

    All login failures share the same envelope so the response
    cannot be used to enumerate users.
    """
    if isinstance(exc, InvalidCredentials):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "status": "error",
                "error_code": "InvalidCredentials",
                "message": "invalid credentials",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise exc


__all__ = [
    "get_current_user",
    "require_role",
    "require_admin",
    "login_error_response",
]
