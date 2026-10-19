"""Phase 7D.4 — multi-cloud AI FinOps analyst service.

Composes the canonical context builder, the grounded system prompt,
and the LiteLLM client into a single ``FinOpsAIService``.  The
service:

* Builds the deterministic context via
  :class:`FinOpsContextBuilder`.
* Renders the prompt via the prompt module.
* Invokes LiteLLM through the existing :class:`LiteLLMClient`
  (reused — no new AI SDK).
* Returns a :class:`FinOpsAIAnswer` with deterministic evidence
  cards (server-side, never LLM-generated).
* When AI is disabled / unavailable, returns a controlled
  ``UNAVAILABLE`` answer without calling the model.

The service does NOT touch AWS / Azure directly.  It only reads
canonical envelopes from the registered provider adapter.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, List, Optional

from app.core.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import SessionLocal
from app.schemas.ai import AIGenerationStatus
from app.schemas.cloud_ai import (
    FinOpsAIAnswer,
    FinOpsAIFreshness,
    FinOpsAIProvider,
    FinOpsAIStatus,
    FinOpsAnalyzeRequest,
    FinOpsEvidence,
)
from app.services.cloud.finops_context import (
    FinOpsContext,
    FinOpsContextBuilder,
)
from app.services.cloud.finops_prompt import (
    SYSTEM_PROMPT,
    evidence_block,
    user_question_block,
)
from app.services.litellm_client import (
    ChatMessage,
    LiteLLMAuthError,
    LiteLLMClient,
    LiteLLMEmptyCompletion,
    LiteLLMError,
    LiteLLMMalformedResponse,
    LiteLLMProviderError,
    LiteLLMQuotaExhausted,
    LiteLLMRateLimit,
    LiteLLMTimeout,
    LiteLLMUnavailable,
)

logger = logging.getLogger("cost-detective-backend.cloud_ai.service")


class FinOpsAIService:
    """Multi-cloud grounded AI analyst."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        builder: Optional[FinOpsContextBuilder] = None,
        client: Optional[LiteLLMClient] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._builder = builder or FinOpsContextBuilder()
        self._client = client  # lazy-initialised in ``_litellm``.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        *,
        request: FinOpsAnalyzeRequest,
        user: AppUser,
        db=None,
    ) -> FinOpsAIAnswer:
        """Produce a grounded answer for the request.

        On AI failure the answer falls back to a deterministic
        evidence-only response (the LLM is optional; the canonical
        data is the source of truth).
        """
        own_db = db is None
        session = db or SessionLocal()
        try:
            context = await self._builder.build(
                provider=request.provider,
                scope=request.scope,
                user=user,
                db=session,
                days=(request.period or {}).get("days"),
                month=(request.period or {}).get("month"),
                start_date=(request.period or {}).get("start_date"),
                end_date=(request.period or {}).get("end_date"),
                region=request.region,
            )
        finally:
            if own_db:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass

        evidence = self._build_evidence(context)

        # Disabled / unavailable → deterministic answer only.
        if not self._settings.ai_enabled:
            return FinOpsAIAnswer(
                answer=self._unavailable_answer(context, "AI generation is disabled in configuration."),
                status=AIGenerationStatus.DISABLED,
                provider=request.provider,
                scope=context.scope,
                account_display_name=context.account_display_name,
                period=context.period,
                data_freshness=self._freshness_from_context(context),
                evidence=evidence,
                warnings=["AI generation is disabled in configuration."],
                model=None,
                duration_ms=0,
            )

        rendered_context = self._render_context(context)
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=(
                    evidence_block(rendered_context)
                    + "\n\n"
                    + user_question_block(request.question)
                ),
            ),
        ]
        start = time.monotonic()
        try:
            completion = self._litellm().complete(messages, temperature=0.2)
        except LiteLLMAuthError as exc:
            logger.warning("cloud_ai.litellm.auth_error code=%s", exc.code)
            return self._fallback(
                request=request,
                context=context,
                evidence=evidence,
                answer=self._unavailable_answer(context, "AI analyst temporarily unavailable (auth)."),
                status=AIGenerationStatus.UNAVAILABLE,
                warnings=["AI analyst temporarily unavailable (auth)."],
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except (LiteLLMTimeout, LiteLLMRateLimit, LiteLLMQuotaExhausted,
                LiteLLMProviderError, LiteLLMUnavailable, LiteLLMEmptyCompletion,
                LiteLLMMalformedResponse, LiteLLMError) as exc:
            logger.warning(
                "cloud_ai.litellm.error code=%s type=%s",
                getattr(exc, "code", "unknown"), type(exc).__name__,
            )
            return self._fallback(
                request=request,
                context=context,
                evidence=evidence,
                answer=self._unavailable_answer(context, "AI analyst temporarily unavailable."),
                status=AIGenerationStatus.UNAVAILABLE,
                warnings=[f"AI analyst temporarily unavailable: {type(exc).__name__}."],
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        answer_text = (completion.text or "").strip() or self._unavailable_answer(
            context, "AI analyst returned an empty response."
        )
        return FinOpsAIAnswer(
            answer=answer_text,
            status=AIGenerationStatus.SUCCESS,
            provider=request.provider,
            scope=context.scope,
            account_display_name=context.account_display_name,
            period=context.period,
            data_freshness=self._freshness_from_context(context),
            evidence=evidence,
            warnings=list(context.warnings),
            model=completion.model or self._settings.litellm_model,
            duration_ms=duration_ms,
        )

    def status(self) -> FinOpsAIStatus:
        if not self._settings.ai_enabled:
            return FinOpsAIStatus(
                enabled=False,
                model=None,
                status="DISABLED",
                message="AI generation is disabled in configuration.",
            )
        try:
            healthy = self._litellm().health_check()
        except Exception:  # noqa: BLE001
            healthy = False
        return FinOpsAIStatus(
            enabled=True,
            model=self._settings.litellm_model,
            status="READY" if healthy else "UNAVAILABLE",
            message=None if healthy else "LiteLLM health probe failed.",
        )

    # ------------------------------------------------------------------
    # Deterministic evidence card generation
    # ------------------------------------------------------------------

    def _build_evidence(self, context: FinOpsContext) -> List[FinOpsEvidence]:
        """Generate evidence cards server-side from the canonical slices.

        The LLM NEVER sees these values during generation — they
        are computed after the model returns so the user can
        verify every claim against a structured source.
        """
        cards: List[FinOpsEvidence] = []
        if context.cost is not None:
            cost = context.cost
            if cost.total_cost is not None:
                cards.append(FinOpsEvidence(
                    type="cost",
                    label=f"Current spend ({cost.currency})",
                    value=f"{cost.total_cost}",
                    currency=cost.currency,
                    secondary=(
                        f"Change vs previous: "
                        f"{cost.change_amount if cost.change_amount is not None else '—'}"
                    ),
                    scope=context.scope,
                ))
            if cost.previous_period_cost is not None:
                cards.append(FinOpsEvidence(
                    type="cost",
                    label=f"Previous-period spend ({cost.currency})",
                    value=f"{cost.previous_period_cost}",
                    currency=cost.currency,
                ))
            if cost.change_percent is not None:
                cards.append(FinOpsEvidence(
                    type="cost",
                    label="Change percentage",
                    value=f"{cost.change_percent}",
                    secondary=(
                        "Azure savings_period may be ANNUAL; savings are projection-level."
                        if context.provider == FinOpsAIProvider.AZURE else None
                    ),
                ))
            for svc in cost.top_services[:5]:
                cards.append(FinOpsEvidence(
                    type="service",
                    label=svc["service"],
                    value=svc["amount"],
                    currency=svc["currency"],
                    scope=context.scope,
                ))
            for reg in cost.top_regions[:5]:
                cards.append(FinOpsEvidence(
                    type="region",
                    label=reg["region"],
                    value=reg["amount"],
                    currency=reg["currency"],
                    scope=context.scope,
                ))
            for acct in cost.by_account:
                cards.append(FinOpsEvidence(
                    type="subscription",
                    label=acct.get("display_name") or acct.get("account_id") or "—",
                    value=str(acct.get("amount") or "0.00"),
                    currency=str(acct.get("currency") or "USD"),
                    scope=str(acct.get("account_id") or ""),
                ))
        if context.resources is not None:
            res = context.resources
            cards.append(FinOpsEvidence(
                type="resource",
                label="Total resources",
                value=f"{res.total}",
                secondary=f"status={res.status}",
            ))
            for row in res.by_type[:5]:
                cards.append(FinOpsEvidence(
                    type="resource",
                    label=f"Type: {row['type']}",
                    value=f"{row['count']}",
                ))
        if context.recommendations is not None:
            rec = context.recommendations
            cards.append(FinOpsEvidence(
                type="recommendation",
                label=f"Total recommendations ({rec.savings_period or '—'})",
                value=f"{rec.count}",
                secondary=(
                    f"Estimated savings: "
                    f"{rec.estimated_savings if rec.estimated_savings is not None else 'unavailable'} "
                    f"{rec.currency or 'USD'}"
                ),
            ))
            for item in rec.top_items[:5]:
                cards.append(FinOpsEvidence(
                    type="recommendation",
                    label=item.get("title") or item.get("recommendation_id") or "—",
                    value=item.get("estimated_savings") or "unavailable",
                    currency=item.get("currency"),
                    reference_id=item.get("recommendation_id"),
                    scope=context.scope,
                ))
        return cards

    # ------------------------------------------------------------------
    # Context rendering for the prompt
    # ------------------------------------------------------------------

    def _render_context(self, context: FinOpsContext) -> str:
        """Render the bounded deterministic context as a compact
        prompt-safe text block.  The LLM receives ONLY this text —
        no raw resource lists, no unbounded daily trend (capped at
        30 points for the prompt).
        """
        lines: List[str] = []
        lines.append(f"provider: {context.provider.value}")
        lines.append(f"scope: {context.scope}")
        if context.account_display_name:
            lines.append(f"account_display_name: {context.account_display_name}")
        if context.period is not None:
            lines.append(
                f"period: {context.period.kind} "
                f"start={context.period.start} end={context.period.end} days={context.period.days}"
            )
        else:
            lines.append("period: unavailable")
        # data_freshness + status may be string or enum depending on
        # the test path; coerce to string for safe rendering.
        def _enum_str(value: Any) -> str:
            return str(getattr(value, "value", value))
        lines.append(f"data_freshness: {_enum_str(context.data_freshness)}")
        lines.append(f"context_status: {_enum_str(context.status)}")
        if context.partial_sources:
            lines.append(f"partial_sources: {','.join(context.partial_sources)}")
        if context.unavailable_sources:
            lines.append(
                f"unavailable_sources: {','.join(context.unavailable_sources)}"
            )
        if context.cost is not None:
            cost = context.cost
            lines.append("\n[cost]")
            lines.append(f"status: {cost.status}")
            lines.append(f"currency: {cost.currency}")
            lines.append(f"successful_accounts: {cost.successful_accounts}")
            lines.append(f"failed_accounts: {cost.failed_accounts}")
            if cost.total_cost is not None:
                lines.append(f"total_cost: {cost.total_cost}")
            else:
                lines.append("total_cost: unavailable")
            if cost.previous_period_unavailable or cost.previous_period_cost is None:
                lines.append("previous_period_cost: unavailable")
            else:
                lines.append(f"previous_period_cost: {cost.previous_period_cost}")
            if cost.change_amount is not None:
                lines.append(f"change_amount: {cost.change_amount}")
            else:
                lines.append("change_amount: unavailable")
            if cost.change_percent is not None:
                lines.append(f"change_percent: {cost.change_percent}")
            else:
                lines.append("change_percent: unavailable")
            lines.append("top_services:")
            for svc in cost.top_services:
                lines.append(
                    f"  - {svc['service']}: {svc['amount']} {svc['currency']}"
                )
            lines.append("top_regions:")
            for reg in cost.top_regions:
                lines.append(
                    f"  - {reg['region']}: {reg['amount']} {reg['currency']}"
                )
            if cost.by_account:
                lines.append("by_account:")
                for acct in cost.by_account:
                    lines.append(
                        f"  - {acct.get('account_id','—')} "
                        f"({acct.get('display_name','—')}): "
                        f"{acct.get('amount','0.00')} {acct.get('currency','USD')}"
                    )
            # Trend is capped at 30 points to keep the prompt
            # bounded (token control per Phase 7D.4 spec).
            if cost.daily_trend:
                lines.append("daily_trend (capped 30 points):")
                for pt in cost.daily_trend[-30:]:
                    lines.append(
                        f"  - {pt['date']}: {pt['amount']} {pt['currency']}"
                    )
        else:
            lines.append("\n[cost] unavailable")

        if context.resources is not None:
            res = context.resources
            lines.append("\n[resources]")
            lines.append(f"status: {res.status}")
            lines.append(f"total: {res.total}")
            if res.by_type:
                lines.append("by_type:")
                for row in res.by_type:
                    lines.append(f"  - {row['type']}: {row['count']}")
            if res.by_region:
                lines.append("by_region:")
                for row in res.by_region:
                    lines.append(f"  - {row['region']}: {row['count']}")
            if res.by_subscription:
                lines.append("by_subscription:")
                for row in res.by_subscription:
                    lines.append(
                        f"  - {row['subscription_id']}: {row['count']}"
                    )
        else:
            lines.append("\n[resources] unavailable")

        if context.recommendations is not None:
            rec = context.recommendations
            lines.append("\n[recommendations]")
            lines.append(f"count: {rec.count}")
            lines.append(f"savings_period: {rec.savings_period or '—'}")
            lines.append(
                f"estimated_savings: "
                f"{rec.estimated_savings if rec.estimated_savings is not None else 'unavailable'} "
                f"{rec.currency or 'USD'}"
            )
            if rec.sources:
                lines.append(f"sources: {','.join(rec.sources)}")
            if rec.top_items:
                lines.append("top_recommendations:")
                for it in rec.top_items:
                    lines.append(
                        f"  - {it.get('title','—')} "
                        f"({it.get('recommendation_id','—')}): "
                        f"est_savings={it.get('estimated_savings') or 'unavailable'} "
                        f"{it.get('currency') or ''} source={it.get('source','—')}"
                    )
        else:
            lines.append("\n[recommendations] unavailable")

        if context.warnings:
            lines.append("\n[warnings]")
            for w in context.warnings:
                lines.append(f"  - {w}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _litellm(self) -> LiteLLMClient:
        if self._client is None:
            self._client = LiteLLMClient(self._settings)
        return self._client

    def _freshness_from_context(self, context: FinOpsContext) -> FinOpsAIFreshness:
        if context.status.value == "UNAVAILABLE":
            return FinOpsAIFreshness.UNAVAILABLE
        if context.status.value == "PARTIAL":
            return FinOpsAIFreshness.PARTIAL
        if context.status.value == "STALE":
            return FinOpsAIFreshness.STALE
        if context.status.value == "CACHED":
            return FinOpsAIFreshness.CACHED
        return FinOpsAIFreshness.LIVE

    def _unavailable_answer(self, context: FinOpsContext, message: str) -> str:
        """Build a deterministic, model-free answer from the context."""
        lines: List[str] = [message, ""]
        if context.cost is not None and context.cost.total_cost is not None:
            lines.append(
                f"Current spend: {context.cost.total_cost} {context.cost.currency} "
                f"(scope={context.scope}, data_freshness={context.data_freshness.value})."
            )
        elif context.cost is not None:
            lines.append(
                f"Current spend is unavailable for scope={context.scope} "
                f"(data_freshness={context.data_freshness.value})."
            )
        if context.resources is not None:
            lines.append(
                f"Resources: total={context.resources.total} (status={context.resources.status})."
            )
        if context.recommendations is not None and context.recommendations.count:
            lines.append(
                f"Recommendations: {context.recommendations.count} "
                f"(savings_period={context.recommendations.savings_period})."
            )
        if context.warnings:
            lines.append("")
            for w in context.warnings:
                lines.append(f"Note: {w}")
        return "\n".join(lines).strip()

    def _fallback(
        self,
        *,
        request: FinOpsAnalyzeRequest,
        context: FinOpsContext,
        evidence: List[FinOpsEvidence],
        answer: str,
        status: AIGenerationStatus,
        warnings: List[str],
        duration_ms: int,
    ) -> FinOpsAIAnswer:
        return FinOpsAIAnswer(
            answer=answer,
            status=status,
            provider=request.provider,
            scope=context.scope,
            account_display_name=context.account_display_name,
            period=context.period,
            data_freshness=self._freshness_from_context(context),
            evidence=evidence,
            warnings=warnings + list(context.warnings),
            model=None,
            duration_ms=duration_ms,
        )


def get_finops_ai_service() -> FinOpsAIService:
    """Module-level accessor used by the route layer."""
    return FinOpsAIService()


__all__ = ["FinOpsAIService", "get_finops_ai_service"]
