"""Pydantic schemas for authentication & admin user management.

The wire shape returned by the API NEVER contains a password hash.
The API never projects an ``AppUser`` ORM row directly into a
response; the auth service builds clean ``PublicUserView`` /
``AdminUserView`` records which deliberately do not have a
``password_hash`` field.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

RoleName = Literal["ADMIN", "ANALYST", "VIEWER"]


# Minimal, intentionally-permissive email regex.  RFC 5321 allows
# a huge number of exotic addresses; for Phase 5A we just need
# ``local@domain.tld`` and to reject obvious garbage.  We do NOT
# depend on the optional ``email-validator`` package — the Phase 5A
# spec keeps the dependency footprint narrow.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}$"
)


def _validate_email(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("email must be a string")
    cleaned = value.strip()
    if not _EMAIL_RE.match(cleaned):
        raise ValueError("email is not a valid address")
    return cleaned.lower()


class LoginRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    email: str
    password: str = Field(min_length=1, max_length=4096)

    @field_validator("email")
    @classmethod
    def _validate_email_field(cls, v: str) -> str:
        return _validate_email(v)


class LoginResponse(BaseModel):
    """Successful login payload.

    ``user`` is intentionally a flat, hash-less projection.  The
    client never sees the password hash.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: "PublicUserView"


class AuthInfoResponse(BaseModel):
    """Anonymous authentication metadata.

    Reveals only what the client needs to render a login UI.  Never
    includes the JWT secret, signing algorithm key material, or any
    credential material.
    """

    auth_enabled: bool
    issuer: str
    audience: str
    algorithm: str
    access_token_minutes: int


class PublicUserView(BaseModel):
    """User view safe to return to any authenticated caller (e.g. ``/me``)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: RoleName
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    @field_validator("email")
    @classmethod
    def _validate_email_field(cls, v: str) -> str:
        return _validate_email(v)


class AdminUserView(BaseModel):
    """User view returned to administrators (includes timestamps).

    Same shape as :class:`PublicUserView`; the distinct type
    documents intent and makes future divergence (e.g. listing extra
    admin-only fields) easy.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: RoleName
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_login_at: Optional[datetime] = None

    @field_validator("email")
    @classmethod
    def _validate_email_field(cls, v: str) -> str:
        return _validate_email(v)


class AdminUserCreateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    email: str
    password: str = Field(min_length=8, max_length=4096)
    display_name: str = Field(min_length=1, max_length=120)
    role: RoleName = "VIEWER"

    @field_validator("email")
    @classmethod
    def _validate_email_field(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("display_name must be non-empty after trimming")
        return cleaned


class AdminUserPatchRequest(BaseModel):
    """Partial-update payload for admin user management.

    ``password`` is intentionally an admin-set temporary/new
    password.  No email-reset workflow exists yet (deferred).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    role: Optional[RoleName] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8, max_length=4096)

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("display_name must be non-empty after trimming")
        return cleaned


class AdminUserListResponse(BaseModel):
    users: List[AdminUserView]
    count: int


# Late-bound self-reference (LoginResponse.user)
LoginResponse.model_rebuild()


__all__ = [
    "RoleName",
    "LoginRequest",
    "LoginResponse",
    "AuthInfoResponse",
    "PublicUserView",
    "AdminUserView",
    "AdminUserCreateRequest",
    "AdminUserPatchRequest",
    "AdminUserListResponse",
]
