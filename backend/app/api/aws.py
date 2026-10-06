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
    - Single region only - multi-region discovery is intentionally NOT
      implemented in this slice (Phase 2 territory).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import require_role
from app.core.config import get_settings
from app.db.models import AppUser
from app.services.aws.enrichment import collect_enrichment
from app.services.aws.identity import AwsIdentityError, get_caller_identity
from app.services.aws.resources import enumerate_all_services

router = APIRouter(prefix="/aws", tags=["aws"])
logger = logging.getLogger("cost-detective-backend.aws")

# Cache the settings accessor; settings itself is already lru_cached.
_settings = get_settings


@router.get("/identity", summary="AWS caller identity (STS GetCallerIdentity)")
def get_aws_identity(
    region: Optional[str] = Query(
        default=None,
        description=(
            "AWS region override (e.g. us-east-1). When omitted, falls back "
            "to AWS_DEFAULT_REGION from Settings, then Boto3's own region resolution. "
            "Single-region only: multi-region discovery is deferred."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    """Return the caller's AWS identity resolved via the default credential chain.

    On credential or STS errors, returns a sanitized 502 with the Boto3
    error code in the body — never the exception message or stack trace.
    """
    effective_region = region or _settings().aws_default_region
    try:
        identity = get_caller_identity(region=effective_region)
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
            },
        )
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
            "to AWS_DEFAULT_REGION from Settings, then Boto3's own region resolution. "
            "Single-region only: multi-region discovery is deferred."
        ),
    ),
    _user: AppUser = Depends(require_role("ADMIN", "ANALYST", "VIEWER")),
) -> Any:
    """Aggregate per-service resource inventory plus optional enrichment.

    Per-service Boto3 errors (AccessDenied, Throttling, etc.) are surfaced
    in the response body as ``status="denied"`` / ``status="error"`` per
    service — they NEVER fail the whole call. The optional enrichment
    layers (Resource Explorer, Tagging API) are likewise presented with
    ``available=False`` flags instead of 500s.
    """
    effective_region = region or _settings().aws_default_region
    services = enumerate_all_services(region=effective_region)
    enrichment = collect_enrichment(region=effective_region)

    return {
        "region": effective_region,
        "services": {name: result.model_dump(mode="json") for name, result in services.items()},
        "enrichment": enrichment,
    }
