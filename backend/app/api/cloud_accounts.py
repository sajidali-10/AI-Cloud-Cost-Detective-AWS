"""Cloud-account registry API — Phase 7B.

Mounted on the existing :data:`router` from :mod:`app.api.aws` so the
public URL is ``/api/cloud-accounts/*`` after Nginx strips ``/api/``.

Endpoints (ADMIN-only unless noted):

* ``GET    /api/cloud-accounts``                  list (ADMIN)
* ``GET    /api/cloud-accounts/selectable``       list for the frontend
                                                 selector (ADMIN/ANALYST/VIEWER)
* ``POST   /api/cloud-accounts``                  register (ADMIN)
* ``PATCH  /api/cloud-accounts/{id}``             update metadata (ADMIN)
* ``POST   /api/cloud-accounts/{id}/validate``    connectivity test (ADMIN)
* ``DELETE /api/cloud-accounts/{id}``             PERMANENT DELETE (ADMIN)
                                                 — refuses management
                                                 row and refuses
                                                 enabled target rows
                                                 (operator must first
                                                 disable).

Lifecycle (UI + backend, kept in lock-step):

* Management row — ``Edit`` metadata only.  No ``Disable`` / no
  ``Delete`` ever.
* Enabled target row — ``Edit``, ``Validate``, ``Disable``.
* Disabled target row — ``Edit``, ``Validate``, ``Enable``,
  ``Delete permanently``.

Security:

* No response shape exposes ``external_id`` (write-only at the API
  surface).
* Status responses use the stable enum (``CONNECTED``,
  ``ACCESS_DENIED``, ``ROLE_NOT_FOUND``, ``DISABLED``, ``ERROR``)
  with a sanitized reason.
* RBAC is enforced via ``require_role``; frontend visibility is
  irrelevant.  Permanent delete is ADMIN-only.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, status
from fastapi.responses import JSONResponse

from app.api.aws import router as aws_router
from app.api.deps import require_admin, require_role
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountListResponse,
    CloudAccountResponse,
    CloudAccountSelectable,
    CloudAccountSelectableListResponse,
    CloudAccountUpdate,
)
from app.services.aws.account_provider import (
    AccountAlreadyExists,
    AccountNotDeletable,
    ManagementAccountImmutable,
    create_account,
    delete_account,
    disable_account,
    get_account_by_id,
    list_accounts,
    resolve_status,
    update_account,
)
from sqlalchemy.orm import Session

logger = logging.getLogger("cost-detective-backend.cloud_accounts")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_not_found_response(account_id: int) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "status": "error",
            "error_code": "AccountNotFound",
            "message": f"cloud account id={account_id} is not registered",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/cloud-accounts  (ADMIN only — full list with metadata)
# ---------------------------------------------------------------------------


@aws_router.get(
    "/cloud-accounts",
    response_model=CloudAccountListResponse,
    summary="List registered cloud accounts (ADMIN only).",
)
def list_cloud_accounts(
    provider: str = "AWS",
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    rows = list_accounts(db, provider=provider, enabled_only=False)
    items = [
        CloudAccountResponse(
            id=row.id,
            provider=row.provider,
            account_id=row.account_id,
            display_name=row.display_name,
            role_arn=row.role_arn,
            enabled=row.enabled,
            is_management=row.is_management,
            tenant_id=row.tenant_id,
            subscription_id=row.subscription_id,
            preferred_region=row.preferred_region,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    return CloudAccountListResponse(items=items, count=len(items))


# ---------------------------------------------------------------------------
# GET /api/cloud-accounts/selectable  (ANALYST/VIEWER too)
# ---------------------------------------------------------------------------


@aws_router.get(
    "/cloud-accounts/selectable",
    response_model=CloudAccountSelectableListResponse,
    summary="Minimal selectable list for the frontend account selector.",
)
def list_selectable_cloud_accounts(
    provider: str = "AWS",
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    rows = list_accounts(db, provider=provider, enabled_only=True)
    items = [
        CloudAccountSelectable(
            id=row.id,
            account_id=row.account_id,
            display_name=row.display_name,
            provider=row.provider,
            enabled=row.enabled,
            is_management=row.is_management,
        )
        for row in rows
    ]
    return CloudAccountSelectableListResponse(items=items, count=len(items))


# ---------------------------------------------------------------------------
# POST /api/cloud-accounts  (ADMIN)
# ---------------------------------------------------------------------------


@aws_router.post(
    "/cloud-accounts",
    response_model=CloudAccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new target AWS account (ADMIN only).",
)
def create_cloud_account(
    payload: CloudAccountCreate,
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    try:
        row = create_account(db, payload)
    except AccountAlreadyExists as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "AccountAlreadyExists",
                "message": str(exc),
            },
        )
    return CloudAccountResponse(
        id=row.id,
        provider=row.provider,
        account_id=row.account_id,
        display_name=row.display_name,
        role_arn=row.role_arn,
        enabled=row.enabled,
        is_management=row.is_management,
        tenant_id=row.tenant_id,
        subscription_id=row.subscription_id,
        preferred_region=row.preferred_region,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# PATCH /api/cloud-accounts/{id}  (ADMIN)
# ---------------------------------------------------------------------------


@aws_router.patch(
    "/cloud-accounts/{account_id}",
    response_model=CloudAccountResponse,
    summary="Update a registered cloud account's metadata (ADMIN only).",
)
def update_cloud_account(
    account_id: int,
    payload: CloudAccountUpdate,
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    row = get_account_by_id(db, account_id)
    if row is None:
        return _row_not_found_response(account_id)
    try:
        row = update_account(db, row, payload)
    except ManagementAccountImmutable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "ManagementAccountImmutable",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "status": "error",
                "error_code": "InvalidUpdate",
                "message": str(exc),
            },
        )
    return CloudAccountResponse(
        id=row.id,
        provider=row.provider,
        account_id=row.account_id,
        display_name=row.display_name,
        role_arn=row.role_arn,
        enabled=row.enabled,
        is_management=row.is_management,
        tenant_id=row.tenant_id,
        subscription_id=row.subscription_id,
        preferred_region=row.preferred_region,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# POST /api/cloud-accounts/{id}/validate  (ADMIN)
# ---------------------------------------------------------------------------


@aws_router.post(
    "/cloud-accounts/{account_id}/validate",
    summary="Resolve the connectivity status for a registered account (ADMIN only).",
)
def validate_cloud_account(
    account_id: int,
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    """Validate connectivity to a registered cloud account.

    Routes through the provider registry so AWS rows go through
    the existing Phase 7B ``resolve_status`` (STS GetCallerIdentity)
    and Azure rows go through ``AzureCloudProvider.validate_account``
    (Entra token exchange + ARM subscription GET).

    Disabled rows short-circuit to ``DISABLED`` regardless of
    provider.
    """
    row = get_account_by_id(db, account_id)
    if row is None:
        return _row_not_found_response(account_id)
    provider_kind = (row.provider or "AWS").strip().upper()
    if provider_kind == "AWS":
        # AWS path unchanged — Phase 7B's resolver handles every
        # AWS-specific status mapping (ACCESS_DENIED,
        # ROLE_NOT_FOUND, ERROR, etc.).
        status_response = resolve_status(db, row)
        return status_response.model_dump(mode="json")
    # Azure / future providers: route through the registry so
    # the adapter owns its own status mapping.
    try:
        from app.services.cloud import CloudProviderKind, ProviderRegistry

        kind = CloudProviderKind(provider_kind)
        adapter = ProviderRegistry.current().require(kind)
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "error",
                "error_code": "UnknownProvider",
                "message": f"unknown provider {provider_kind!r}",
            },
        )
    result = adapter.validate_account(row)
    return result.to_dict()


# ---------------------------------------------------------------------------
# DELETE /api/cloud-accounts/{id}  (ADMIN)
# PERMANENT delete — only available for DISABLED target accounts.
# Removes ONLY the registry row; never touches AWS.
# ---------------------------------------------------------------------------


@aws_router.delete(
    "/cloud-accounts/{account_id}",
    summary="Permanently remove a registered cloud account (ADMIN only).",
)
def delete_cloud_account(
    account_id: int,
    _admin: AppUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Any:
    """Permanently delete a registered account row.

    Safety rules (refused with sanitized errors):

    * The management account is immutable — returns ``409``
      ``ManagementAccountImmutable``.  No IAM, no registry change.
    * The target account must already be DISABLED.  An enabled
      target row is refused with ``409`` ``AccountNotDeletable``
      so an operator must explicitly click Disable first.
    * Non-ADMIN roles are rejected by ``require_admin`` (403).
    * Only the local ``cloud_accounts`` row is removed.  No IAM
      change, no STS revocation, no AWS resource deletion.

    Returns ``204 No Content`` on success.
    """
    row = get_account_by_id(db, account_id)
    if row is None:
        return _row_not_found_response(account_id)
    try:
        # Capture identity BEFORE deletion so we can purge cached
        # cost rows that reference this account's id.
        aws_account_id = row.account_id
        delete_account(db, row)
    except ManagementAccountImmutable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "ManagementAccountImmutable",
                "message": str(exc),
            },
        )
    except AccountNotDeletable as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "error",
                "error_code": "AccountNotDeletable",
                "message": str(exc),
            },
        )

    # Lifecycle hygiene: drop any cached cost rows that referenced
    # this account so a stale response can never surface after the
    # registry row is gone.  Purge failure is non-fatal — the
    # entries will expire on TTL anyway, but we log a warning.
    try:
        from app.services.cost_cache import purge_account, purge_all
        purge_account(aws_account_id, db=db)
        # Drop the multi-account aggregate too — its totals are now
        # stale (one of the contributing accounts is gone).
        purge_all(db=db)
    except Exception as exc:  # noqa: BLE001 - sanitized
        logger.warning(
            "cloud_accounts.delete.purge_failed account=%s code=%s",
            aws_account_id, type(exc).__name__,
        )

    return JSONResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        content=None,
    )


__all__ = [
    "create_cloud_account",
    "delete_cloud_account",
    "list_cloud_accounts",
    "list_selectable_cloud_accounts",
    "update_cloud_account",
    "validate_cloud_account",
]
