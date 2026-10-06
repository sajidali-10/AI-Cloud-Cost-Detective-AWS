"""Authentication API — Phase 5A.

Three endpoints, all under ``/api/auth``:

* ``POST /api/auth/login`` — anonymous, returns access token.
* ``GET  /api/auth/me``    — authenticated, returns the current user.
* ``GET  /api/auth/info``  — anonymous, returns auth metadata.

Routes are thin: they parse requests, delegate to
:class:`AuthService`, and serialise the result.  No password,
hash, or token is ever logged at this layer.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user, login_error_response
from app.core.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.auth import (
    AuthInfoResponse,
    LoginRequest,
    LoginResponse,
    PublicUserView,
)
from app.services.auth_service import (
    AuthService,
    InvalidCredentials,
)

logger = logging.getLogger("cost-detective-backend.auth_api")

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


@router.post(
    "/login",
    summary="Authenticate with email + password, receive access token",
)
def login(
    payload: LoginRequest,
    db = Depends(get_db),
) -> Any:
    auth = AuthService(db=db)
    """Authenticate the user and return a bearer access token.

    The endpoint is intentionally anonymous (it must accept
    unauthenticated requests).  Failures are reported as a generic
    ``InvalidCredentials`` 401 — no information about whether the
    email exists is leaked.
    """
    try:
        user = auth.authenticate(email=payload.email, password=payload.password)
    except InvalidCredentials as exc:
        # Sanitized response — never reveals which check failed.
        logger.info("auth.login.denied email=%s", (payload.email or "").strip().lower())
        return login_error_response(exc)

    # Stamp last_login_at and issue a token.  Both are best-effort:
    # if the DB write fails the login still succeeds (the user can
    # log in again), and if token issuance fails we surface a 500.
    try:
        auth.record_successful_login(user)
    except Exception:  # pragma: no cover — defensive only
        logger.exception("auth.login.stamp_failed user_id=%s", user.id)
        auth._db.rollback()  # noqa: SLF001 — best-effort

    token, ttl = auth.issue_token(user)
    logger.info("auth.login.ok user_id=%s role=%s", user.id, user.role)
    return LoginResponse(
        access_token=token,
        expires_in=ttl,
        user=PublicUserView.model_validate(user),
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    summary="Return the authenticated user (no password hash)",
)
def me(user: AppUser = Depends(get_current_user)) -> Any:
    """Return the current authenticated user.

    Never includes ``password_hash`` — the response shape is
    :class:`PublicUserView` which has no such field.
    """
    return PublicUserView.model_validate(user).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /api/auth/info
# ---------------------------------------------------------------------------


@router.get(
    "/info",
    summary="Anonymous authentication metadata",
)
def info(settings: Settings = Depends(get_settings)) -> Any:
    """Return anonymous auth metadata for the login UI.

    Never includes the JWT secret or any key material.
    """
    return AuthInfoResponse(
        auth_enabled=settings.auth_enabled,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithm=settings.jwt_algorithm,
        access_token_minutes=settings.jwt_access_token_minutes,
    ).model_dump(mode="json")


__all__ = ["router", "login", "me", "info"]
