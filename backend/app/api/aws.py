"""AWS FastAPI router — Phase 1 (identity + resource discovery).

Mounted under ``/api/aws`` publicly. The FastAPI ``include_router``
prefix is empty (root): Nginx strips the ``/api/`` prefix before
proxying, so the public path ``/api/aws/identity`` and
``/api/aws/resources`` land on this router's ``/identity`` and
``/resources`` paths.

Region handling (applies to BOTH endpoints):
    - Caller may pass ``?region=<r>`` per request.
    - If absent, falls back to ``settings.aws_default_region``.
    - If absent again, falls back to Boto3's own region resolution.

Phase 7B: callers may pass ``?account_id=<selection>`` to scope the
request to a registered target account.  When omitted, the
management account / instance profile is used (preserves Phase 7A
behaviour).  ``account_id`` accepts either ``"acct:<id>"`` or a
registry row id.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.core.config import get_settings
from app.db.models import AppUser
from app.db.session import get_db
from app.services.aws.account_provider import (
    get_management_account,
    resolve_account_or_404,
)
from app.services.aws.enrichment import collect_enrichment
from app.services.aws.identity import AwsIdentityError, get_caller_identity
from app.services.aws.resources import enumerate_all_services
from app.services.aws.session_factory import (
    get_default_session_factory,
)

router = APIRouter(prefix="/aws", tags=["aws"])
logger = logging.getLogger("cost-detective-backend.aws")

# Cache the settings accessor; settings itself is already lru_cached.
_settings = get_settings


def _resolve_account_or_management(
    db: Session,
    selection: Optional[str],
):
    """Resolve ``?account_id`` to a registry row, defaulting to management."""
    if selection is None or selection.strip() == "" or selection == "all":
        return get_management_account(db)
    return resolve_account_or_404(db, selection.strip())


@router.get("/identity", summary="AWS caller identity (STS GetCallerIdentity)")
def get_aws_identity(
    region: Optional[str] = Query(
        default=None,
        description=(
            "AWS region override (e.g. us-east-1). When omitted, falls back "
            "to AWS_DEFAULT_REGION from Settings, then Boto3's own region resolution."
        ),
    ),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all' for backwards-compatibility with Phase 7A."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    """Return the caller's AWS identity.

    With no ``account_id`` (default), this resolves the management
    instance profile exactly as Phase 1/7A did.  When
    ``account_id`` points at a registered target account, this
    issues an ``STS AssumeRole`` against that account's role and
    returns the assumed-role identity.
    """
    settings = _settings()
    effective_region = region or settings.aws_default_region

    selection = (account_id or "all").strip()
    row = _resolve_account_or_management(db, selection)
    if row is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "AccountNotFound",
                "message": f"no AWS account matches selection={selection!r}",
            },
        )

    factory = get_default_session_factory()
    try:
        if row.is_management:
            session = factory.management_session()
        else:
            session = factory.target_session(
                role_arn=row.role_arn,
                external_id=row.external_id,
                region=effective_region,
            )
        identity = get_caller_identity(
            region=effective_region, session=session
        )
    except AwsIdentityError as exc:
        # Log the code (not the message) so an operator can debug
        # without leaking credentials into logs.
        logger.warning("aws.identity error_code=%s", exc.code)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "error",
                "error_code": exc.code,
                "region": effective_region,
                "account_id": row.account_id,
            },
        )
    identity["account_id"] = row.account_id
    identity["account_display_name"] = row.display_name
    identity["is_management"] = row.is_management
    return identity


@router.get(
    "/resources",
    summary="AWS multi-service resource inventory with optional enrichment",
)
def get_aws_resources(
    region: Optional[str] = Query(
        default=None,
        description=(
            "AWS region override (e.g. us-east-1). When omitted, falls back "
            "to AWS_DEFAULT_REGION from Settings, then Boto3's own region resolution."
        ),
    ),
    account_id: Optional[str] = Query(
        default=None,
        description=(
            "Phase 7B: 'all' (default), 'acct:<id>' or a registry row id. "
            "Omitted is treated as 'all'."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    """Aggregate per-service resource inventory plus optional enrichment.

    Phase 7B: when ``account_id`` is set to a specific account the
    discovery is scoped to that account's session.  When
    ``account_id`` is omitted (default ``all``) the response is the
    management account's inventory, which preserves Phase 7A
    behaviour exactly.  The dedicated multi-account aggregate lives
    at ``/api/aws/resources/all``.

    Per-service Boto3 errors (AccessDenied, Throttling, etc.) are surfaced
    in the response body as ``status="denied"`` / ``status="error"`` per
    service — they NEVER fail the whole call. The optional enrichment
    layers (Resource Explorer, Tagging API) are likewise presented with
    ``available=False`` flags instead of 500s.
    """
    settings = _settings()
    effective_region = region or settings.aws_default_region
    selection = (account_id or "all").strip()

    if selection == "all":
        # Preserve Phase 7A behaviour exactly.
        services = enumerate_all_services(region=effective_region)
        enrichment = collect_enrichment(region=effective_region)
        return {
            "region": effective_region,
            "services": {name: result.model_dump(mode="json") for name, result in services.items()},
            "enrichment": enrichment,
        }

    # Specific account path.
    row = _resolve_account_or_management(db, selection)
    if row is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "AccountNotFound",
                "message": f"no AWS account matches selection={selection!r}",
            },
        )
    factory = get_default_session_factory()
    from app.services.aws import clients as _clients_module

    def _patched(service_name: str, region: Optional[str] = None):
        if row.is_management:
            return factory.management_client(service_name, region=region or effective_region)
        return factory.target_client(
            service_name,
            role_arn=row.role_arn,
            external_id=row.external_id,
            region=region or effective_region,
        )

    original = _clients_module.get_aws_client
    _clients_module.get_aws_client = _patched
    try:
        services = enumerate_all_services(region=effective_region)
        enrichment = collect_enrichment(region=effective_region)
    finally:
        _clients_module.get_aws_client = original
    payload = {
        "region": effective_region,
        "account_id": row.account_id,
        "account_display_name": row.display_name,
        "services": {name: result.model_dump(mode="json") for name, result in services.items()},
        "enrichment": enrichment,
    }
    # Tag every emitted item with account attribution so the
    # frontend never has to guess which account a resource belongs to.
    for service_name, result in payload["services"].items():
        for item in result.get("items", []):
            item.setdefault("provider", "AWS")
            item["account_id"] = row.account_id
            item["account_display_name"] = row.display_name
    return payload


@router.get(
    "/resources/all",
    summary="Phase 7B — multi-account resource inventory aggregate",
)
async def get_aws_resources_all(
    region: Optional[str] = Query(default=None),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
    db: Session = Depends(get_db),
) -> Any:
    """Fan out resource discovery across every enabled account.

    Every resource carries a composite identity
    ``(provider, account_id, region, resource_type, resource_id)`` so
    two accounts with the same ``i-0123`` appear as distinct rows.
    Per-account failures are captured in ``account_errors`` and do
    not fail the request.  Bounded concurrency via
    ``PHASE7B_RESOURCES_MAX_CONCURRENCY``.
    """
    from app.services.aws.account_provider import list_accounts
    from app.services.aws.resource_aggregator import (
        aggregate_inventory_for_context,
    )

    settings = _settings()
    effective_region = region or settings.aws_default_region
    rows = list_accounts(db, provider="AWS", enabled_only=True)
    if not rows:
        mgmt = get_management_account(db)
        if mgmt is not None:
            rows = [mgmt]
    inventory = await aggregate_inventory_for_context(
        rows=rows, region=effective_region
    )
    return inventory.to_summary_dict(scope="all")
