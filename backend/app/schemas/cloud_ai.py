"""Phase 7D.4 — canonical multi-cloud AI FinOps schemas.

Additive on top of the existing Phase 4 ``app.schemas.ai`` module.
The new ``/api/cloud/ai/analyze`` endpoint uses these schemas so
the request / response shape is provider-aware (AWS + Azure) and
scope-aware (``acct:<id>`` / ``sub:<id>`` / ``all``).

Design choices
--------------

* ``provider`` is REQUIRED on the request and on the response.
  The Phase 4 ``/api/ai/*`` endpoints stay AWS-only for
  backwards compatibility; the new canonical endpoints serve
  both providers.
* ``scope`` follows the existing canonical selector rules:

  * ``AWS``   → ``acct:<account-id>`` or ``all``
  * ``AZURE`` → ``sub:<guid>`` or ``all``

  The provider-aware validation lives in the route layer.
* ``period`` matches the canonical cloud-cost period contract:
  ``month`` (e.g. ``"2026-10"``), ``days`` (e.g. ``30``), or
  ``start_date``/``end_date``.
* ``data_freshness`` on the response reflects the freshness of
  the canonical envelope that grounded the answer.  It can be
  ``LIVE`` / ``CACHED`` / ``STALE`` / ``PARTIAL`` / ``UNAVAILABLE``.
* ``evidence`` is generated from deterministic application data
  — NEVER invented by the model.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ai import AIGenerationStatus


class FinOpsAIProvider(str, Enum):
    AWS = "AWS"
    AZURE = "AZURE"


class FinOpsAIFreshness(str, Enum):
    LIVE = "LIVE"
    CACHED = "CACHED"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class FinOpsAIPeriod(BaseModel):
    """Mirrors the canonical cost-period shape."""

    model_config = ConfigDict(frozen=True)

    kind: str  # "month" | "days" | "explicit"
    start: str
    end: str
    days: int


class FinOpsAnalyzeRequest(BaseModel):
    """Request body for ``POST /api/cloud/ai/analyze``.

    All fields are explicit so the front-end cannot accidentally
    forget the scope / provider / period the user is currently
    looking at.
    """

    model_config = ConfigDict(frozen=True)

    provider: FinOpsAIProvider
    # Provider-aware scope selector.  Validated by the route layer
    # against the registry; we do NOT enforce a regex here because
    # some Azure subscriptions carry complex GUID formats.
    scope: str = "all"
    question: str = Field(..., min_length=3, max_length=2000)
    # Optional period override.  When omitted the service falls
    # back to the user's currently-selected FinOps period via the
    # cloud-costs route (the response carries the resolved period).
    period: Optional[Dict[str, Any]] = None
    # Optional provider-specific overrides (region for AWS,
    # account_id filter for Azure).  Mirrors the canonical cost
    # route parameters.
    region: Optional[str] = None


class FinOpsEvidence(BaseModel):
    """Deterministic, server-side evidence card.

    The model is forbidden from inventing these values; the route
    layer extracts them from the canonical envelopes the AI
    service grounded on.
    """

    model_config = ConfigDict(frozen=True)

    type: str  # "cost" | "service" | "region" | "resource" | "recommendation" | "subscription"
    label: str
    value: str
    currency: Optional[str] = None
    secondary: Optional[str] = None
    scope: Optional[str] = None
    reference_id: Optional[str] = None


class FinOpsAIAnswer(BaseModel):
    """Response envelope for ``POST /api/cloud/ai/analyze``."""

    model_config = ConfigDict(frozen=True)

    # Prose answer from the LLM (may be empty when the model is
    # unavailable — the route still returns a deterministic
    # answer built from the evidence cards).
    answer: str
    status: AIGenerationStatus
    provider: FinOpsAIProvider
    scope: str
    account_display_name: Optional[str] = None
    period: Optional[FinOpsAIPeriod] = None
    data_freshness: FinOpsAIFreshness
    evidence: List[FinOpsEvidence] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    model: Optional[str] = None
    duration_ms: int = 0


class FinOpsAIStatus(BaseModel):
    """AI capability status snapshot (canonical)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    model: Optional[str] = None
    status: str  # "READY" | "DISABLED" | "UNAVAILABLE"
    message: Optional[str] = None


__all__ = [
    "FinOpsAIProvider",
    "FinOpsAIFreshness",
    "FinOpsAIPeriod",
    "FinOpsAnalyzeRequest",
    "FinOpsEvidence",
    "FinOpsAIAnswer",
    "FinOpsAIStatus",
]
