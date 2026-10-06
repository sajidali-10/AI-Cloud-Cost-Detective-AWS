"""FastAPI routes for the AI Cost Analyst — Phase 4.

Four endpoints, all under ``/api/ai``:

* ``GET  /api/ai/status`` — public status snapshot.
* ``POST /api/ai/executive-summary`` — grounded executive summary.
* ``POST /api/ai/analyze`` — grounded Q&A.
* ``POST /api/ai/recommendations/{recommendation_id}/explain`` —
  explain a single Phase 3 recommendation.

The router is mounted at root prefix ``/ai`` so Nginx strips
``/api/`` and the public paths become ``/api/ai/...``.

All endpoints reuse the existing sanitized error envelope
(``{"status": "error", "error_code": "...", "message": "..."}``)
and the 422 lookback validation from Phase 2/3.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.schemas.ai import (
    AnalyzeRequest,
    AIGenerationStatus,
    ExecutiveSummaryRequest,
)
from app.services.ai_service import AIService, get_ai_service

logger = logging.getLogger("cost-detective-backend.ai")

router = APIRouter(prefix="/ai", tags=["ai"])

# Module-level accessor; tests build their own ``AIService``.
_service_factory = get_ai_service


def _error(*, code: str, message: str, http_status: int = 400, **extra: Any) -> JSONResponse:
    payload = {
        "status": "error",
        "error_code": code,
        "message": message,
    }
    payload.update(extra)
    return JSONResponse(status_code=http_status, content=payload)


@router.get(
    "/status",
    summary="AI Cost Analyst capability / readiness status",
)
def ai_status() -> Any:
    """Return the AI capability status snapshot.

    Never exposes the API key, the Authorization header, or any URL
    carrying credentials.
    """
    service: AIService = _service_factory()
    return service.status().model_dump(mode="json")


@router.post(
    "/executive-summary",
    summary="Grounded executive FinOps summary",
)
def ai_executive_summary(payload: ExecutiveSummaryRequest) -> Any:
    """Produce the executive summary for ``region`` + ``days``."""
    service: AIService = _service_factory()
    try:
        service.validate_lookback(payload.days)
    except ValueError as exc:
        return _error(
            code="InvalidLookbackDays",
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            days=payload.days,
        )
    response = service.generate_executive_summary(
        region=payload.region or "",
        days=payload.days,
    )
    # 200 for SUCCESS / PARTIAL_SUCCESS / DISABLED / UNAVAILABLE so
    # callers can branch on the JSON envelope rather than the HTTP
    # code.  FAILED yields 404-equivalent semantics? No — the evidence
    # was gathered successfully; FAILED here means the LiteLLM call
    # returned a malformed payload.  Still surface as 200 with a
    # status field so the caller can render appropriately.
    return response.model_dump(mode="json")


@router.post(
    "/analyze",
    summary="Grounded Q&A against the evidence package",
)
def ai_analyze(payload: AnalyzeRequest) -> Any:
    """Answer a user question grounded in the Phase 2/3 evidence."""
    service: AIService = _service_factory()
    try:
        service.validate_lookback(payload.days)
    except ValueError as exc:
        return _error(
            code="InvalidLookbackDays",
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            days=payload.days,
        )
    try:
        service.validate_question(payload.question)
    except ValueError as exc:
        return _error(
            code="InvalidQuestion",
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            question_length=len(payload.question or ""),
        )
    response = service.generate_analysis(
        region=payload.region or "",
        days=payload.days,
        question=payload.question,
    )
    return response.model_dump(mode="json")


@router.post(
    "/recommendations/{recommendation_id}/explain",
    summary="Explain a single Phase 3 recommendation",
)
def ai_recommendation_explain(
    recommendation_id: str,
    payload: ExecutiveSummaryRequest,
) -> Any:
    """Explain the recommendation identified by ``recommendation_id``."""
    service: AIService = _service_factory()
    try:
        service.validate_lookback(payload.days)
    except ValueError as exc:
        return _error(
            code="InvalidLookbackDays",
            message=str(exc),
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            days=payload.days,
        )
    response = service.generate_recommendation_explanation(
        region=payload.region or "",
        days=payload.days,
        recommendation_id=recommendation_id,
    )
    # 404 when the recommendation does not exist in the evidence
    # package — the caller asked for a specific id and it isn't
    # present, which is a not-found condition.
    if (
        response.status == AIGenerationStatus.FAILED
        and "RecommendationNotFound" in response.warnings
    ):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "status": "error",
                "error_code": "RecommendationNotFound",
                "message": "Recommendation not found in the authoritative Phase 3 evidence.",
                "recommendation_id": recommendation_id,
                "region": payload.region,
                "days": payload.days,
            },
        )
    return response.model_dump(mode="json")


__all__ = [
    "ai_analyze",
    "ai_executive_summary",
    "ai_recommendation_explain",
    "ai_status",
    "router",
]
