"""Phase 7D.4 — multi-cloud AI FinOps Analyst API.

Two canonical endpoints under ``/api/cloud/ai``:

* ``GET  /api/cloud/ai/status`` — AI capability status snapshot.
* ``POST /api/cloud/ai/analyze`` — grounded multi-cloud Q&A.

Both reuse the existing Phase 4 ``require_role`` dependency so
admin / analyst / dev RBAC remains consistent with every other
canonical endpoint.  The router mounts at the same ``/ai`` prefix
the Phase 4 endpoints use so the public URL is ``/api/cloud/ai/...``
(after Nginx strips ``/api/``).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db.models import AppUser
from app.db.session import get_db
from app.schemas.cloud_ai import FinOpsAIStatus, FinOpsAnalyzeRequest
from app.services.cloud.finops_ai_service import FinOpsAIService, get_finops_ai_service

logger = logging.getLogger("cost-detective-backend.cloud_ai.api")

router = APIRouter(prefix="/ai", tags=["ai"])

_service_factory = get_finops_ai_service


def _error(
    *, code: str, message: str, http_status: int = 400, **extra: Any
) -> JSONResponse:
    payload = {
        "status": "error",
        "error_code": code,
        "message": message,
    }
    payload.update(extra)
    return JSONResponse(status_code=http_status, content=payload)


@router.get(
    "/status",
    summary="AI FinOps Analyst capability / readiness status",
)
def ai_status() -> Any:
    """Return the AI capability status snapshot.

    Never exposes the API key, the Authorization header, or any
    URL carrying credentials.
    """
    service: FinOpsAIService = _service_factory()
    status_obj = service.status()
    return status_obj.model_dump(mode="json")


@router.post(
    "/analyze",
    summary="Grounded multi-cloud AI FinOps Q&A",
)
async def ai_analyze(
    payload: FinOpsAnalyzeRequest,
    user: AppUser = Depends(require_role("ADMIN", "ANALYST")),
    db: Session = Depends(get_db),
) -> Any:
    """Answer a user question grounded in the canonical multi-cloud
    evidence.  AWS + Azure are first-class.  Cross-cloud totals are
    explicitly NOT performed — the response includes per-provider
    evidence cards only.
    """
    service: FinOpsAIService = _service_factory()
    try:
        response = await service.analyze(request=payload, user=user, db=db)
    except ValueError as exc:
        return _error(
            code="InvalidRequest",
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return response.model_dump(mode="json")


__all__ = ["ai_analyze", "ai_status", "router"]
