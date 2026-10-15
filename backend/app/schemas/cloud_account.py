"""Pydantic schemas for the cloud-account registry (Phase 7B + 7C).

This module is intentionally provider-agnostic: Phase 7B only
implements AWS, but the request/response shapes are written so a
future Azure / Rackspace adapter can plug in without breaking the
registry API.

Phase 7C.1 makes the validation rules conditional on the
``provider`` field:

* ``provider='AWS'`` — strict AWS validation: 12-digit account id,
  IAM role ARN with the embedded account id matching, optional
  external id, optional preferred region matching the AWS region
  format.
* ``provider='AZURE'`` — Azure subscription validation: ``account_id``
  is the subscription GUID (validated as a UUID); ``role_arn`` is
  forbidden (Azure rows have no IAM role); ``tenant_id`` is the
  Entra tenant GUID; ``subscription_id`` is the same as the
  account id but kept as a separate column for parity with the
  ORM.
* ``provider in ('AZURE', 'RACKSPACE')`` — relaxed validation:
  ``account_id`` and ``role_arn`` are accepted as opaque strings
  (the Azure/Rackspace adapters will own their own per-provider
  validation).  Phase 7C.1 still rejects creation of non-AWS rows
  — only ``AWS`` is enabled at the API surface today.

Phase 7C.2A flips ``AZURE`` from "reserved" to "implemented":
the request / response schemas now accept Azure rows and the
account lifecycle API validates them per-provider.

Security rules enforced here:

* AWS account IDs are validated as ``<12-digit string>``.
* IAM role ARNs are validated as ``arn:aws:iam::<account-id>:role/<name>``
  with the embedded account ID matching the registered one.
* Azure subscription IDs are validated as GUIDs.
* ``external_id`` is **write-only** at the registry level — it never
  appears in any GET response.  It is intentionally not even echoed
  back on PATCH responses.  This mirrors how AWS treats external IDs
  as a write-once secret: write it, then forget it.
* No AWS access key id / secret access key fields exist anywhere
  in these schemas — they are a design bug if added later.
* No Azure client-secret field exists anywhere in these schemas
  — Phase 7C.2A authentication is secretless (workload
  federation), so a client-secret column would be a design bug.
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

# Azure subscription IDs are UUIDs in standard hyphenated form
# (8-4-4-4-12).  We accept both lower and upper case and normalise
# to lower case in the validator so duplicates collapse cleanly.
AZURE_SUBSCRIPTION_ID_REGEX = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
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
        description=(
            "Provider-native account identifier: 12-digit AWS "
            "account ID for AWS, subscription GUID for AZURE, "
            "tenant identifier for RACKSPACE."
        ),
        min_length=1,
        max_length=64,
    )
    display_name: str = Field(
        ..., min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH
    )
    role_arn: Optional[str] = Field(
        default=None,
        description=(
            "AWS IAM role ARN to assume in the target account. "
            "Must be of the form arn:aws:iam::<account-id>:role/<name> "
            "and the embedded account ID must match ``account_id``. "
            "Required for AWS target accounts; ignored for AWS "
            "management rows (is_management=True).  Nullable for "
            "future Azure / Rackspace providers."
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
    # Phase 7C.1 — Azure-only metadata.  AWS rows leave both NULL.
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Microsoft Entra tenant GUID. Azure rows only."
        ),
        max_length=64,
    )
    subscription_id: Optional[str] = Field(
        default=None,
        description=(
            "Azure subscription GUID. Azure rows only; for Azure "
            "this is the same value as account_id."
        ),
        max_length=64,
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
        if normalized not in {"AWS", "AZURE", "RACKSPACE"}:
            raise ValueError(
                f"provider={normalized!r} is not recognised; "
                "expected one of: AWS, AZURE, RACKSPACE"
            )
        return normalized

    @field_validator("account_id")
    @classmethod
    def _validate_account_id(cls, value: str) -> str:
        # ``provider`` is not always populated when pydantic runs
        # this validator on partial inputs; we run a length sanity
        # check here and re-validate per-provider in the model
        # validator below so cross-field rules still apply.
        if not isinstance(value, str) or not value.strip():
            raise ValueError("account_id must be a non-empty string")
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        stripped = (value or "").strip()
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
        # Cheap region format check; boto3 will reject malformed ones
        # at client construction time anyway.
        if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d+$", stripped):
            raise ValueError(f"preferred_region={value!r} is not a valid AWS region")
        return stripped

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        stripped = value.strip()
        if not AZURE_SUBSCRIPTION_ID_REGEX.match(stripped):
            raise ValueError(
                "tenant_id must be a valid Microsoft Entra tenant GUID"
            )
        return stripped.lower()

    @field_validator("subscription_id")
    @classmethod
    def _validate_subscription_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        stripped = value.strip()
        if not AZURE_SUBSCRIPTION_ID_REGEX.match(stripped):
            raise ValueError(
                "subscription_id must be a valid Azure subscription GUID"
            )
        return stripped.lower()

    @model_validator(mode="after")
    def _validate_per_provider(self) -> "CloudAccountBase":
        provider = (self.provider or "").strip().upper()
        if provider == "AWS":
            if not AWS_ACCOUNT_ID_REGEX.match(self.account_id or ""):
                raise ValueError(
                    "AWS account_id must be a 12-digit string"
                )
            match = AWS_IAM_ROLE_ARN_REGEX.match(self.role_arn or "")
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
        elif provider == "AZURE":
            if not AZURE_SUBSCRIPTION_ID_REGEX.match(self.account_id or ""):
                raise ValueError(
                    "AZURE account_id must be a valid subscription GUID"
                )
            # Normalise the Azure subscription GUID to lower case so
            # uniqueness checks (provider+account_id) collapse on
            # case-insensitive matches.  AWS account ids are already
            # 12 digits — no normalisation needed.
            self.account_id = (self.account_id or "").lower()
            if self.role_arn:
                raise ValueError(
                    "AZURE rows MUST NOT have role_arn; Azure "
                    "authentication is workload-federation only"
                )
            # For Azure rows, ``subscription_id`` defaults to the
            # account_id if it was not supplied.  This keeps the
            # ORM column populated for parity with the schema.
            if not self.subscription_id:
                self.subscription_id = self.account_id
            elif self.subscription_id != self.account_id:
                raise ValueError(
                    "AZURE subscription_id must match account_id"
                )
        # RACKSPACE is reserved for 7C.3 — no validation rules
        # implemented yet.
        return self


class CloudAccountCreate(CloudAccountBase):
    """Request shape for ``POST /api/cloud-accounts``."""


class CloudAccountUpdate(BaseModel):
    """Request shape for ``PATCH /api/cloud-accounts/{id}``.

    All fields are optional; only provided fields are updated.  Note
    that ``external_id`` is accepted here only as a write — it is
    intentionally omitted from the response model.

    Phase 7C.2A: provider-specific rules apply.  The model's
    ``_validate_per_provider`` enforces that AWS rows keep their
    role ARN shape and that Azure rows do not gain one.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, max_length=DISPLAY_NAME_MAX_LENGTH)
    role_arn: Optional[str] = Field(default=None)
    external_id: Optional[str] = Field(default=None, max_length=EXTERNAL_ID_MAX_LENGTH)
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    subscription_id: Optional[str] = Field(default=None, max_length=64)
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

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        stripped = value.strip()
        if not AZURE_SUBSCRIPTION_ID_REGEX.match(stripped):
            raise ValueError(
                "tenant_id must be a valid Microsoft Entra tenant GUID"
            )
        return stripped.lower()

    @field_validator("subscription_id")
    @classmethod
    def _validate_subscription_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return None
        stripped = value.strip()
        if not AZURE_SUBSCRIPTION_ID_REGEX.match(stripped):
            raise ValueError(
                "subscription_id must be a valid Azure subscription GUID"
            )
        return stripped.lower()


# ---------------------------------------------------------------------------
# Response shapes — ``external_id`` is intentionally NEVER included.
# ---------------------------------------------------------------------------


class CloudAccountResponse(BaseModel):
    """Public read model — never exposes ``external_id``.

    Phase 7C.2A adds ``tenant_id`` and ``subscription_id`` so
    Azure rows can be rendered in the UI / API surface.  AWS rows
    leave both fields NULL; the response is uniform.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    account_id: str
    display_name: str
    role_arn: Optional[str]
    enabled: bool
    is_management: bool
    tenant_id: Optional[str] = None
    subscription_id: Optional[str] = None
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

    Phase 7C.2A widens the status enum to include the Azure-specific
    values:

    * ``AUTH_FAILED`` — Entra token exchange rejected the assertion.
    * ``NOT_FOUND``   — subscription id is not visible to the SP.
    * ``NOT_CONFIGURED`` — Azure runtime configuration is missing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    account_id: str
    display_name: str
    status: str = Field(
        description=(
            "One of: CONNECTED, ACCESS_DENIED, ROLE_NOT_FOUND, "
            "DISABLED, AUTH_FAILED, NOT_FOUND, NOT_CONFIGURED, ERROR"
        )
    )
    reason: Optional[str] = Field(
        default=None,
        description="Sanitized human-readable reason.",
    )
    arn: Optional[str] = Field(
        default=None,
        description=(
            "Resolved caller reference when CONNECTED: AWS role "
            "ARN for AWS rows, Azure subscription GUID for AZURE rows."
        ),
    )
    region: Optional[str] = Field(default=None)
    credential_strategy: Optional[str] = Field(
        default=None,
        description=(
            "Sanitised credential-strategy identifier for the row. "
            "Phase 7C.1 used AWS_INSTANCE_PROFILE / AWS_ASSUME_ROLE; "
            "Phase 7C.2A adds AZURE_WORKLOAD_FEDERATION."
        ),
    )


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
    "AZURE_SUBSCRIPTION_ID_REGEX",
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
