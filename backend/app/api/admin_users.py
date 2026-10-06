"""Admin user-management API — Phase 5A.

Three endpoints under ``/api/admin/users``:

* ``POST  /api/admin/users``              — create user.
* ``GET   /api/admin/users``              — list users.
* ``PATCH /api/admin/users/{user_id}``    — partial update (role /
                                           is_active / display_name /
                                           password).

All endpoints require ADMIN role.  When ``AUTH_ENABLED=false`` the
:class:`require_admin` dependency still issues a synthetic ADMIN
user (Phase 0-4 backward-compat), so the routes remain reachable.

The wire shape NEVER includes the password hash.  ``AdminUserView``
is hash-less by construction.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from app.api.deps import require_admin
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.auth import (
    AdminUserCreateRequest,
    AdminUserListResponse,
    AdminUserPatchRequest,
    AdminUserView,
)
from app.services.auth_service import (
    AuthService,
    InvalidRole,
    UserAlreadyExists,
    UserNotFound,
)

logger = logging.getLogger("cost-detective-backend.admin_users_api")

router = APIRouter(prefix="/admin/users", tags=["admin"])


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new application user (ADMIN only)",
)
def create_user(
    payload: AdminUserCreateRequest,
    _admin: AppUser = Depends(require_admin),
    db = Depends(get_db),
) -> Any:
    """Create a new user.

    Returns 201 with an :class:`AdminUserView` (no password hash)
    on success, 409 ``UserAlreadyExists`` on a duplicate email,
    400 ``InvalidRole`` on a role outside the allowed set.
    """
    auth = AuthService(db=db)
    try:
        user = auth.create_user(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            role=payload.role,
        )
    except UserAlreadyExists as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "UserAlreadyExists",
                "message": "a user with this email already exists",
            },
        )
    except InvalidRole as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "InvalidRole",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "InvalidInput",
                "message": str(exc),
            },
        )
    return AdminUserView.model_validate(user).model_dump(mode="json")


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List all application users (ADMIN only)",
)
def list_users(
    _admin: AppUser = Depends(require_admin),
    db = Depends(get_db),
) -> Any:
    """Return every application user.

    The list contains only safe fields (id, email, display_name,
    role, is_active, timestamps).  Password hashes are not exposed.
    """
    auth = AuthService(db=db)
    users = auth.list_users()
    views = [AdminUserView.model_validate(u) for u in users]
    return AdminUserListResponse(
        users=views, count=len(views)
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{user_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/{user_id}",
    summary="Partially update a user (ADMIN only)",
)
def patch_user(
    user_id: int,
    payload: AdminUserPatchRequest,
    admin: AppUser = Depends(require_admin),
    db = Depends(get_db),
) -> Any:
    """Apply a partial update to a user.

    Supports:

    * changing the role (``ADMIN | ANALYST | VIEWER``)
    * activating / deactivating
    * renaming the display name
    * admin-set new password (no email-reset flow yet)

    Admin users cannot demote themselves in a way that would leave
    the system without an ADMIN — see ``_prevent_last_admin_lockout``.
    """
    auth = AuthService(db=db)
    if user_id == admin.id and payload.is_active is False:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "SelfDeactivation",
                "message": "an admin cannot deactivate their own account",
            },
        )
    if user_id == admin.id and payload.role is not None and payload.role != "ADMIN":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "SelfDemotion",
                "message": "an admin cannot change their own role",
            },
        )

    try:
        user = auth.update_user(
            user_id,
            display_name=payload.display_name,
            role=payload.role,
            is_active=payload.is_active,
            password=payload.password,
        )
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "status": "error",
                "error_code": "UserNotFound",
                "message": f"no user with id={user_id}",
            },
        )
    except InvalidRole as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "InvalidRole",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "InvalidInput",
                "message": str(exc),
            },
        )
    return AdminUserView.model_validate(user).model_dump(mode="json")


__all__ = ["router", "create_user", "list_users", "patch_user"]
