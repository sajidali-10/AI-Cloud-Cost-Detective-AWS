"""Pydantic schemas for the cloud-account registry (Phase 7B).

This module is intentionally provider-agnostic: Phase 7B only
implements AWS, but the request/response shapes are written so a
future Azure / Rackspace adapter can plug in without breaking the
registry API.

Security rules enforced here:

* AWS account IDs are validated as ``<12-digit string>``.
* IAM role ARNs are validated as ``arn:aws:iam::<account-id>:role/<name>``
  with the embedded account ID matching the registered one.
* ``external_id`` is **write-only** at the registry level — it never
  appears in any GET response.  It is intentionally not even echoed
  back on PATCH responses.  This mirrors how AWS treats external IDs
  as a write-once secret: write it, then forget it.
* No AWS access key id / secret access key fields exist anywhere
  in these schemas — they are a design bug if added later.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

# AWS account IDs are exactly 12 digits.  Owner account IDs in
# consolidated billing are also 12-digit strings.
AWS_ACCOUNT_ID_REGEX = re.compile(r"^\d{12}$")

# We accept the standard partition ARNs.  China / GovCloud partitions
# are explicitly deferred — adding them is a one-line change once a
# deployment targets those regions.
AWS_IAM_ROLE_ARN_REGEX = re.compile(
    r"^arn:aws:iam::(?P<account_id>\d{12}):role/(?P<role_name>[\w+=,.@-]+)$"
)

# External IDs are 2-1224 char strings of [A-Za-z0-9=,.@:\/\-_].  We
# only require non-empty + a sane max length here; AWS does not pin
# a tighter format.
EXTERNAL_ID_MAX_LENGTH = 1224

# Display names are user-facing labels; we keep them short and
# printable so the selector UI doesn't truncate awkwardly.
DISPLAY_NAME_MAX_LENGTH = 120

# Role names as embedded in ARNs are capped at 64 chars by IAM; we
# only enforce a sensible upper bound to catch typos.
ROLE_NAME_MAX_LENGTH = 64


class CloudAccountBase(BaseModel):
    """Common fields for all registry requests."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="AWS", description="Cloud provider identifier.")
    account_id: str = Field(
        ...,
        description="12-digit AWS account ID for AWS accounts.",
        min_length=12,
        max_length=12,
    )
    display_name: str = Field(
        ..., min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH
    )
    role_arn: str = Field(
        ...,
        description=(
            "IAM role ARN to assume in the target AWS account. "
            "Must be of the form arn:aws:iam::<account-id>:role/<name> "
            "and the embedded account ID must match ``account_id``."
        ),
    )
    external_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional STS AssumeRole external ID. Write-only: never "
            "returned on any GET / PATCH response. Never logged."
        ),
        max_length=EXTERNAL_ID_MAX_LENGTH,
    )
    enabled: bool = Field(default=True)
    preferred_region: Optional[str] = Field(
        default=None,
        description=(
            "Optional preferred AWS region for this account "
            "(e.g. us-east-1).  Falls back to AWS_DEFAULT_REGION when unset."
        ),
        max_length=32,
    )

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized != "AWS":
            raise ValueError(
                "Only provider='AWS' is implemented in Phase 7B. "
                "Azure / Rackspace adapters belong to Phase 7C."
            )
        return normalized

    @field_validator("account_id")
    @classmethod
    def _validate_account_id(cls, value: str) -> str:
        if not isinstance(value, str) or not AWS_ACCOUNT_ID_REGEX.match(value):
            raise ValueError("account_id must be a 12-digit AWS account ID")
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        stripped = (value or "").strip()
        if not stripped:
            raise ValueError("display_name must not be empty")
        return stripped

    @field_validator("role_arn")
    @classmethod
    def _validate_role_arn(cls, value: str) -> str:
        if not isinstance(value, str) or not AWS_IAM_ROLE_ARN_REGEX.match(value):
            raise ValueError(
                "role_arn must be of the form "
                "arn:aws:iam::<12-digit-account-id>:role/<role-name>"
            )
        return value

    @field_validator("preferred_region")
    @classmethod
    def _validate_preferred_region(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        # Cheap region format check; boto3 will reject malformed ones
        # at client construction time anyway.
        if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d+$", stripped):
            raise ValueError(f"preferred_region={value!r} is not a valid AWS region")
        return stripped

    @model_validator(mode="after")
    def _validate_arn_matches_account(self) -> "CloudAccountBase":
        match = AWS_IAM_ROLE_ARN_REGEX.match(self.role_arn)
        if match and match.group("account_id") != self.account_id:
            raise ValueError(
                "role_arn embeds an account_id that does not match "
                "the account_id field"
            )
        role_name = match.group("role_name") if match else ""
        if len(role_name) > ROLE_NAME_MAX_LENGTH:
            raise ValueError(
                f"role name {role_name!r} exceeds {ROLE_NAME_MAX_LENGTH} characters"
            )
        return self


class CloudAccountCreate(CloudAccountBase):
    """Request shape for ``POST /api/cloud-accounts``."""


class CloudAccountUpdate(BaseModel):
    """Request shape for ``PATCH /api/cloud-accounts/{id}``.

    All fields are optional; only provided fields are updated.  Note
    that ``external_id`` is accepted here only as a write — it is
    intentionally omitted from the response model.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=DISPLAY_NAME_MAX_LENGTH)
    role_arn: Optional[str] = Field(default=None)
    external_id: Optional[str] = Field(default=None, max_length=EXTERNAL_ID_MAX_LENGTH)
    enabled: Optional[bool] = Field(default=None)
    preferred_region: Optional[str] = Field(default=None, max_length=32)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name must not be empty")
        return stripped

    @field_validator("role_arn")
    @classmethod
    def _validate_role_arn(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or not AWS_IAM_ROLE_ARN_REGEX.match(value):
            raise ValueError(
                "role_arn must be of the form "
                "arn:aws:iam::<12-digit-account-id>:role/<role-name>"
            )
        return value

    @field_validator("preferred_region")
    @classmethod
    def _validate_preferred_region(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d+$", stripped):
            raise ValueError(f"preferred_region={value!r} is not a valid AWS region")
        return stripped


# ---------------------------------------------------------------------------
# Response shapes — ``external_id`` is intentionally NEVER included.
# ---------------------------------------------------------------------------


class CloudAccountResponse(BaseModel):
    """Public read model — never exposes ``external_id``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    account_id: str
    display_name: str
    role_arn: str
    enabled: bool
    is_management: bool
    preferred_region: Optional[str]
    created_at: datetime
    updated_at: datetime


class CloudAccountSelectable(BaseModel):
    """Minimal shape used by the frontend account selector."""

    id: int
    account_id: str
    display_name: str
    provider: str
    enabled: bool
    is_management: bool


class CloudAccountStatus(BaseModel):
    """Connectivity test / status response.

    Status codes are intentionally stable so the frontend can map them
    to UI states.  ``reason`` is sanitized — never raw AWS exception
    payloads.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: str
    display_name: str
    status: str = Field(
        description=(
            "One of: CONNECTED, ACCESS_DENIED, ROLE_NOT_FOUND, "
            "DISABLED, ERROR"
        )
    )
    reason: Optional[str] = Field(
        default=None,
        description="Sanitized human-readable reason.",
    )
    arn: Optional[str] = Field(
        default=None,
        description="STS-resolved caller ARN when CONNECTED.",
    )
    region: Optional[str] = Field(default=None)


class CloudAccountListResponse(BaseModel):
    items: list[CloudAccountResponse]
    count: int


class CloudAccountSelectableListResponse(BaseModel):
    """Selector payload — includes ``all`` as the synthetic first entry."""

    items: list[CloudAccountSelectable]
    count: int


__all__ = [
    "AWS_ACCOUNT_ID_REGEX",
    "AWS_IAM_ROLE_ARN_REGEX",
    "CloudAccountBase",
    "CloudAccountCreate",
    "CloudAccountListResponse",
    "CloudAccountResponse",
    "CloudAccountSelectable",
    "CloudAccountSelectableListResponse",
    "CloudAccountStatus",
    "CloudAccountUpdate",
    "EXTERNAL_ID_MAX_LENGTH",
]
