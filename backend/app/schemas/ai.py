"""Pydantic schemas for the Phase 4 AI Cost Analyst API.

The wire contract follows the existing project conventions:

* ``status`` is one of ``SUCCESS`` / ``PARTIAL_SUCCESS`` / ``FAILED``
  for generation endpoints, and ``OK`` / ``DISABLED`` / ``DEGRADED``
  for the status endpoint.
* Errors are surfaced as ``{"status": "error", "error_code": "...",
  "message": "..."}`` matching the existing envelope.
* AI responses never leak raw provider payloads — only normalized
  fields (``answer``, ``model``, ``grounding``, ``citations``,
  ``warnings``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AIStatus(str, Enum):
    OK = "OK"
    DISABLED = "DISABLED"
    DEGRADED = "DEGRADED"


class AIGenerationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


class AIStatusResponse(BaseModel):
    """The ``GET /api/ai/status`` payload.

    Intentionally omits the API key, the full Authorization header,
    and any upstream URL containing credentials.
    """

    model_config = ConfigDict(frozen=True)

    status: AIStatus
    ai_enabled: bool
    litellm_reachable: bool
    model_alias: str
    litellm_base_url: str  # scheme://host:port (no credentials)
    timeout_seconds: int
    max_output_tokens: int
    context_limits: Dict[str, int]
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


class ExecutiveSummaryRequest(BaseModel):
    """Body for ``POST /api/ai/executive-summary``."""

    model_config = ConfigDict(extra="forbid")

    region: Optional[str] = Field(default=None)
    days: int = Field(default=30)


class AnalyzeRequest(BaseModel):
    """Body for ``POST /api/ai/analyze``."""

    model_config = ConfigDict(extra="forbid")

    region: Optional[str] = Field(default=None)
    days: int = Field(default=30)
    question: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class AIGrounding(BaseModel):
    """What evidence the AI used to produce the answer.

    Useful for clients (and the verification scripts) to confirm
    the response was grounded in real data and not hallucinated.
    """

    model_config = ConfigDict(frozen=True)

    account_id: Optional[str] = None
    region: Optional[str] = None
    days: int
    cost_evidence_used: bool
    recommendations_used: int
    capabilities_used: bool


class AIResponse(BaseModel):
    """The normalized AI response envelope.

    Used by every generation endpoint (executive summary, Q&A,
    recommendation explanation).  ``citations`` are validated
    server-side against the supplied evidence; ``warnings`` lists
    discarded citations or other advisory notes.
    """

    model_config = ConfigDict(frozen=True)

    status: AIGenerationStatus
    operation: str  # "executive_summary" | "analyze" | "explain"
    answer: str
    model: Optional[str] = None
    grounding: AIGrounding
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Error envelope (matches existing project convention).
# ---------------------------------------------------------------------------


class AIErrorEnvelope(BaseModel):
    """AI endpoint error envelope (HTTP-level errors).

    The wire shape mirrors ``{"status": "error", "error_code": "...", ...}``
    used by the Phase 1/2/3 routes.  ``message`` is always sanitized.
    """

    model_config = ConfigDict(frozen=True)

    status: str = "error"
    error_code: str
    message: str
    region: Optional[str] = None
    days: Optional[int] = None
    recommendation_id: Optional[str] = None


__all__ = [
    "AIGenerationStatus",
    "AIGrounding",
    "AIResponse",
    "AIStatus",
    "AIStatusResponse",
    "AnalyzeRequest",
    "ExecutiveSummaryRequest",
    "AIErrorEnvelope",
]
