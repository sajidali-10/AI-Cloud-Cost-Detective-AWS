"""AI context builder — Phase 4.

Transforms the authoritative Phase 2 / Phase 3 evidence into a
compact, bounded, deterministic ``AIContext`` package that the AI
service passes to LiteLLM.

Design constraints:

* **Pure transformer.**  This module does NOT call AWS.  The AI
  service layer gathers Phase 2 / Phase 3 data via the existing
  service modules and passes the resulting Pydantic models here.
* **Deterministic, bounded output.**  Top services / regions /
  recommendations are capped by configuration.  Ordering is
  testable (savings DESC, then HIGH confidence, then AWS-native
  source, then deterministic HIGH-confidence).
* **No raw AWS API dumps.**  Only the normalized fields that
  downstream code can reason about are included.
* **Citation index.**  The builder also produces a
  ``CitationIndex`` so the AI service can validate every
  recommendation / resource / service claim the model returns
  against the actual evidence supplied.
* **Savings protection.**  ``None`` savings are preserved as
  ``None`` everywhere; the builder NEVER substitutes a fabricated
  value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.core.config import Settings, get_settings
from app.schemas.cost import CostReport
from app.schemas.optimization import (
    CapabilitiesResponse,
    CapabilityStatus,
    Confidence,
    Recommendation,
    RecommendationAction,
    ResourceType,
    SavingsSource,
)

logger = logging.getLogger("cost-detective-backend.ai_context_builder")


# ---------------------------------------------------------------------------
# Bounded, deterministic context data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextServiceCost:
    service: str
    amount: Decimal
    unit: str = "USD"


@dataclass(frozen=True)
class ContextRegionCost:
    region: str
    amount: Decimal
    unit: str = "USD"


@dataclass(frozen=True)
class ContextRecommendation:
    """A trimmed, prompt-safe view of a Phase 3 ``Recommendation``.

    Only the fields a grounded AI explanation can legitimately cite
    are kept.  Anything that would let the model "make up" details
    is intentionally absent.
    """

    recommendation_id: str
    resource_id: str
    resource_type: ResourceType
    action: RecommendationAction
    title: str
    finding: str
    confidence: Confidence
    data_quality: str
    savings_source: SavingsSource
    estimated_monthly_savings: Optional[Decimal]
    currency: str
    savings_percentage: Optional[Decimal]
    region: str
    restart_needed: Optional[bool]
    rollback_possible: Optional[bool]
    reason_codes: Tuple[str, ...]
    current_configuration: Dict[str, Any]
    recommended_configuration: Dict[str, Any]


@dataclass(frozen=True)
class ContextCapabilities:
    region: str
    account_id: Optional[str]
    compute_optimizer_status: CapabilityStatus
    cost_optimization_hub_status: CapabilityStatus
    deterministic_engine_status: CapabilityStatus
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class AIContext:
    """The bounded, deterministic context package."""

    account_id: Optional[str]
    region: str
    days: int
    lookback_label: str  # e.g. "30d"
    period_start: Optional[str]
    period_end: Optional[str]
    previous_period_start: Optional[str]
    previous_period_end: Optional[str]
    current_total: Optional[Decimal]
    previous_total: Optional[Decimal]
    change_amount: Optional[Decimal]
    change_percent: Optional[Decimal]
    currency: str
    top_services: Tuple[ContextServiceCost, ...]
    top_regions: Tuple[ContextRegionCost, ...]
    capabilities: ContextCapabilities
    recommendations: Tuple[ContextRecommendation, ...]
    limitations: Tuple[str, ...]
    generated_at: str


@dataclass(frozen=True)
class CitationIndex:
    """The set of citation values the AI may legitimately emit.

    The AI service validates every citation the model returns (or
    every structured reference embedded in prose) against this
    index.  Anything not in the index is dropped with a warning.
    """

    recommendation_ids: Set[str] = field(default_factory=set)
    resource_ids: Set[str] = field(default_factory=set)
    services: Set[str] = field(default_factory=set)
    regions: Set[str] = field(default_factory=set)
    periods: Set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Ordering for recommendations
# ---------------------------------------------------------------------------


# Source precedence (lower = stronger evidence).  Mirrors the
# optimization engine's SOURCE_PRECEDENCE but at the AI layer we use
# it to rank WHAT to surface first to the model.
_SOURCE_PRECEDENCE_RANK: Dict[SavingsSource, int] = {
    SavingsSource.AWS_COST_OPTIMIZATION_HUB: 0,
    SavingsSource.AWS_COMPUTE_OPTIMIZER: 1,
    SavingsSource.CALCULATED: 2,
    SavingsSource.UNKNOWN: 3,
}

_CONFIDENCE_RANK: Dict[Confidence, int] = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


def _recommendation_sort_key(rec: Recommendation) -> Tuple[int, int, int, int, str, str]:
    """Deterministic ordering for recommendations.

    Priority: authoritative non-null savings first, then HIGH
    confidence, then AWS-native source, then deterministic
    HIGH-confidence recommendation, then resource_id, then action.
    """
    has_savings = 0 if rec.estimated_monthly_savings is not None else 1
    savings_neg = (
        -float(rec.estimated_monthly_savings)
        if rec.estimated_monthly_savings is not None
        else 0
    )
    return (
        has_savings,
        savings_neg,
        _CONFIDENCE_RANK.get(rec.confidence, 99),
        _SOURCE_PRECEDENCE_RANK.get(rec.primary_source, 99),
        rec.resource_id,
        rec.action.value,
    )


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


class AIContextBuilder:
    """Build an :class:`AIContext` from Phase 2 / Phase 3 evidence."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    # ---- entry point -----------------------------------------------------

    def build(
        self,
        *,
        region: str,
        days: int,
        cost_report: Optional[CostReport],
        capabilities: Optional[CapabilitiesResponse],
        recommendations: Sequence[Recommendation],
    ) -> Tuple[AIContext, CitationIndex]:
        """Return ``(AIContext, CitationIndex)``.

        Parameters
        ----------
        region
            The AWS region used for the evidence pull.
        days
            The lookback window in days (must be 7 / 30 / 60 / 90).
        cost_report
            The Phase 2 ``CostReport`` for the lookback, or ``None``
            if Cost Explorer failed.  The builder never invents a
            report.
        capabilities
            The Phase 3 capabilities response, or ``None``.
        recommendations
            The deduplicated Phase 3 recommendations for this region
            and lookback.
        """
        lookback_label = f"{days}d"
        top_services = self._top_services(cost_report)
        top_regions = self._top_regions(cost_report)
        caps = self._normalize_capabilities(capabilities, region=region)
        limited_recs = self._bounded_recommendations(recommendations)
        limitations = self._derive_limitations(
            cost_report=cost_report,
            capabilities=capabilities,
            recommendations=recommendations,
            limited_count=len(limited_recs),
            total_count=len(recommendations),
        )

        ctx = AIContext(
            account_id=(cost_report.account_id if cost_report else None),
            region=region,
            days=days,
            lookback_label=lookback_label,
            period_start=str(cost_report.period.start) if cost_report else None,
            period_end=str(cost_report.period.end) if cost_report else None,
            previous_period_start=(
                str(cost_report.previous_period.start) if cost_report else None
            ),
            previous_period_end=(
                str(cost_report.previous_period.end) if cost_report else None
            ),
            current_total=(cost_report.total_cost if cost_report else None),
            previous_total=(cost_report.previous_period_cost if cost_report else None),
            change_amount=(cost_report.change_amount if cost_report else None),
            change_percent=(cost_report.change_percent if cost_report else None),
            currency=(cost_report.currency if cost_report else "USD"),
            top_services=tuple(top_services),
            top_regions=tuple(top_regions),
            capabilities=caps,
            recommendations=tuple(limited_recs),
            limitations=tuple(limitations),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

        index = CitationIndex(
            recommendation_ids={r.recommendation_id for r in limited_recs},
            resource_ids={r.resource_id for r in limited_recs},
            services={s.service for s in top_services},
            regions={r.region for r in top_regions} | {region},
            periods={lookback_label},
        )
        return ctx, index

    # ---- normalization helpers ------------------------------------------

    def _top_services(self, cost_report: Optional[CostReport]) -> List[ContextServiceCost]:
        if cost_report is None:
            return []
        limit = self._settings.ai_max_context_services
        # ``by_service`` is already sorted by amount descending by the
        # cost service layer; preserve that ordering for determinism.
        out: List[ContextServiceCost] = []
        for svc in cost_report.by_service[:limit]:
            out.append(ContextServiceCost(service=svc.service, amount=svc.amount, unit=svc.unit))
        return out

    def _top_regions(self, cost_report: Optional[CostReport]) -> List[ContextRegionCost]:
        if cost_report is None:
            return []
        limit = self._settings.ai_max_context_regions
        out: List[ContextRegionCost] = []
        for reg in cost_report.by_region[:limit]:
            out.append(ContextRegionCost(region=reg.region, amount=reg.amount, unit=reg.unit))
        return out

    def _normalize_capabilities(
        self,
        capabilities: Optional[CapabilitiesResponse],
        *,
        region: str,
    ) -> ContextCapabilities:
        if capabilities is None:
            return ContextCapabilities(
                region=region,
                account_id=None,
                compute_optimizer_status=CapabilityStatus.UNAVAILABLE,
                cost_optimization_hub_status=CapabilityStatus.UNAVAILABLE,
                deterministic_engine_status=CapabilityStatus.AVAILABLE,
                warnings=("Capabilities evidence was not available.",),
            )
        warnings = tuple(
            f"{w.source}:{w.code}:{w.message}" for w in capabilities.warnings
        )
        return ContextCapabilities(
            region=capabilities.region or region,
            account_id=capabilities.account_id,
            compute_optimizer_status=capabilities.compute_optimizer.status,
            cost_optimization_hub_status=capabilities.cost_optimization_hub.status,
            deterministic_engine_status=capabilities.deterministic_engine.status,
            warnings=warnings,
        )

    def _bounded_recommendations(
        self, recommendations: Sequence[Recommendation]
    ) -> List[ContextRecommendation]:
        limit = self._settings.ai_max_context_recommendations
        sorted_recs = sorted(recommendations, key=_recommendation_sort_key)
        out: List[ContextRecommendation] = []
        for rec in sorted_recs[:limit]:
            out.append(self._trim_recommendation(rec))
        return out

    @staticmethod
    def _trim_recommendation(rec: Recommendation) -> ContextRecommendation:
        """Project a ``Recommendation`` into a prompt-safe shape.

        Tags and free-form AWS metadata are intentionally NOT
        carried into the prompt — they are untrusted DATA that the
        context builder treats as inert strings (the parent
        ``ai_service`` separates them out for safe rendering).
        """
        return ContextRecommendation(
            recommendation_id=rec.recommendation_id,
            resource_id=rec.resource_id,
            resource_type=rec.resource_type,
            action=rec.action,
            title=rec.title,
            finding=rec.finding,
            confidence=rec.confidence,
            data_quality=rec.data_quality,
            savings_source=rec.savings_source,
            estimated_monthly_savings=rec.estimated_monthly_savings,
            currency=rec.currency,
            savings_percentage=rec.savings_percentage,
            region=rec.region,
            restart_needed=rec.restart_needed,
            rollback_possible=rec.rollback_possible,
            reason_codes=tuple(rec.reason_codes),
            current_configuration=dict(rec.current_configuration),
            recommended_configuration=dict(rec.recommended_configuration),
        )

    @staticmethod
    def _derive_limitations(
        *,
        cost_report: Optional[CostReport],
        capabilities: Optional[CapabilitiesResponse],
        recommendations: Sequence[Recommendation],
        limited_count: int,
        total_count: int,
    ) -> List[str]:
        notes: List[str] = []
        if cost_report is None:
            notes.append("Cost Explorer evidence was not available.")
        if capabilities is None:
            notes.append("AWS-native optimization capabilities could not be determined.")
        else:
            if capabilities.compute_optimizer.status != CapabilityStatus.ACTIVE:
                notes.append(
                    "AWS Compute Optimizer is not ACTIVE; its recommendations are unavailable."
                )
            if capabilities.cost_optimization_hub.status not in (
                CapabilityStatus.ACTIVE,
                CapabilityStatus.AVAILABLE,
            ):
                notes.append(
                    "AWS Cost Optimization Hub is not enrolled; its recommendations are unavailable."
                )
        without_savings = sum(1 for r in recommendations if r.estimated_monthly_savings is None)
        if without_savings:
            notes.append(
                f"{without_savings} of {len(recommendations)} recommendations have no "
                "authoritative savings figure; treat their cost impact as unknown."
            )
        if total_count > limited_count:
            notes.append(
                f"Only the top {limited_count} of {total_count} recommendations are included."
            )
        if not notes:
            notes.append("No additional limitations identified.")
        return notes


# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------


def validate_recommendation_citation(
    citation: Dict[str, Any],
    *,
    index: CitationIndex,
) -> bool:
    """Return ``True`` iff a recommendation citation is grounded.

    Accepts the shape::

        {"type": "recommendation", "id": "det-...", "resource_id": "vol-..."}

    Both ``id`` (recommendation_id) and ``resource_id`` (if present)
    must be present in the supplied index.
    """
    if not isinstance(citation, dict):
        return False
    if citation.get("type") != "recommendation":
        return False
    rid = citation.get("id")
    if not isinstance(rid, str) or rid not in index.recommendation_ids:
        return False
    res = citation.get("resource_id")
    if res is not None and (not isinstance(res, str) or res not in index.resource_ids):
        return False
    return True


def validate_cost_service_citation(
    citation: Dict[str, Any],
    *,
    index: CitationIndex,
) -> bool:
    """Return ``True`` iff a service / region / period citation is grounded."""
    if not isinstance(citation, dict):
        return False
    ctype = citation.get("type")
    if ctype == "cost_service":
        svc = citation.get("service")
        return isinstance(svc, str) and svc in index.services
    if ctype == "cost_region":
        reg = citation.get("region")
        return isinstance(reg, str) and reg in index.regions
    if ctype == "cost_period":
        period = citation.get("period")
        return isinstance(period, str) and period in index.periods
    return False


def filter_grounded_citations(
    citations: Iterable[Dict[str, Any]],
    *,
    index: CitationIndex,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Drop citations that are not grounded in the supplied evidence.

    Returns ``(kept, warnings)``.  Unsupported citation types are
    dropped silently (no warning); citation types we recognize but
    whose values are NOT in the index generate a warning so the AI
    service can surface it.
    """
    kept: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        ctype = citation.get("type")
        if ctype == "recommendation":
            if validate_recommendation_citation(citation, index=index):
                kept.append(citation)
            else:
                warnings.append(
                    f"Discarded unsupported recommendation citation: {citation!r}"
                )
            continue
        if ctype in ("cost_service", "cost_region", "cost_period"):
            if validate_cost_service_citation(citation, index=index):
                kept.append(citation)
            else:
                warnings.append(
                    f"Discarded unsupported {ctype} citation: {citation!r}"
                )
            continue
        # Unknown citation type — silently drop (defensive).
    return kept, warnings


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_context_text(ctx: AIContext) -> str:
    """Render an :class:`AIContext` to a prompt-safe text block.

    The output is deterministic and contains ONLY the values already
    present in the context.  No raw AWS API dumps, no tags, no
    resource metadata beyond the trimmed fields.
    """
    lines: List[str] = []
    lines.append("Account:")
    lines.append(f"  account_id: {ctx.account_id if ctx.account_id is not None else 'unknown'}")
    lines.append(f"  region: {ctx.region}")
    lines.append(f"  lookback_days: {ctx.days}")
    lines.append("")
    lines.append("Cost evidence (authoritative):")
    if ctx.current_total is None:
        lines.append("  current_period_total: unavailable")
    else:
        lines.append(
            f"  current_period_total: {_format_decimal(ctx.current_total)} {ctx.currency}"
        )
        lines.append(
            f"  current_period: {ctx.period_start} to {ctx.period_end}"
        )
    if ctx.previous_total is not None:
        lines.append(
            f"  previous_period_total: {_format_decimal(ctx.previous_total)} {ctx.currency}"
        )
        lines.append(
            f"  previous_period: {ctx.previous_period_start} to {ctx.previous_period_end}"
        )
    if ctx.change_amount is not None:
        sign = "+" if ctx.change_amount >= 0 else ""
        lines.append(f"  change_amount: {sign}{_format_decimal(ctx.change_amount)} {ctx.currency}")
    if ctx.change_percent is not None:
        sign = "+" if ctx.change_percent >= 0 else ""
        lines.append(f"  change_percent: {sign}{_format_decimal(ctx.change_percent * 100)}%")
    lines.append("")
    if ctx.top_services:
        lines.append("Top services by spend:")
        for svc in ctx.top_services:
            lines.append(f"  - {svc.service}: {_format_decimal(svc.amount)} {svc.unit}")
        lines.append("")
    if ctx.top_regions:
        lines.append("Top regions by spend:")
        for reg in ctx.top_regions:
            lines.append(f"  - {reg.region}: {_format_decimal(reg.amount)} {reg.unit}")
        lines.append("")
    caps = ctx.capabilities
    lines.append("Capabilities:")
    lines.append(
        "  compute_optimizer: "
        f"{caps.compute_optimizer_status.value}"
    )
    lines.append(
        "  cost_optimization_hub: "
        f"{caps.cost_optimization_hub_status.value}"
    )
    lines.append(
        "  deterministic_engine: "
        f"{caps.deterministic_engine_status.value}"
    )
    lines.append("")
    if ctx.recommendations:
        lines.append("Optimization recommendations (ordered by priority):")
        for rec in ctx.recommendations:
            lines.append(_render_recommendation_line(rec))
        lines.append("")
    if ctx.limitations:
        lines.append("Limitations of this evidence:")
        for note in ctx.limitations:
            lines.append(f"  - {note}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_recommendation_line(rec: ContextRecommendation) -> str:
    parts = [
        f"- recommendation_id: {rec.recommendation_id}",
        f"  resource_id: {rec.resource_id}",
        f"  resource_type: {rec.resource_type.value}",
        f"  action: {rec.action.value}",
        f"  region: {rec.region}",
        f"  confidence: {rec.confidence.value}",
        f"  data_quality: {rec.data_quality}",
        f"  savings_source: {rec.savings_source.value}",
        f"  title: {rec.title}",
        f"  finding: {rec.finding}",
    ]
    if rec.estimated_monthly_savings is None:
        parts.append(
            "  estimated_monthly_savings: null "
            "(authoritative monthly savings are not available for this recommendation)"
        )
    else:
        parts.append(
            f"  estimated_monthly_savings: {_format_decimal(rec.estimated_monthly_savings)} "
            f"{rec.currency}"
        )
    if rec.savings_percentage is not None:
        parts.append(f"  savings_percentage: {_format_decimal(rec.savings_percentage)}%")
    if rec.reason_codes:
        parts.append(f"  reason_codes: {', '.join(rec.reason_codes)}")
    if rec.restart_needed is not None:
        parts.append(f"  restart_needed: {str(rec.restart_needed).lower()}")
    if rec.rollback_possible is not None:
        parts.append(f"  rollback_possible: {str(rec.rollback_possible).lower()}")
    if rec.current_configuration:
        # Trimmed view — only primitive fields.
        parts.append(
            "  current_configuration: "
            f"{_render_config(rec.current_configuration)}"
        )
    if rec.recommended_configuration:
        parts.append(
            "  recommended_configuration: "
            f"{_render_config(rec.recommended_configuration)}"
        )
    return "\n".join(parts)


def _render_config(cfg: Dict[str, Any]) -> str:
    if not cfg:
        return "{}"
    items = []
    for k, v in cfg.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            items.append(f"{k}={v!r}")
        else:
            items.append(f"{k}=<opaque>")
    return "{" + ", ".join(items) + "}"


def _format_decimal(value: Decimal) -> str:
    """Format a Decimal with up to 4 fractional digits, no scientific."""
    # Quantize to 4 fractional digits, strip trailing zeros, keep at
    # least 2 fractional digits for monetary amounts.
    quantized = value.quantize(Decimal("0.0001"))
    text = format(quantized, "f")
    if "." in text:
        int_part, frac_part = text.split(".", 1)
        frac_part = frac_part.rstrip("0")
        if not frac_part:
            text = int_part
        else:
            text = f"{int_part}.{frac_part}"
    return text


__all__ = [
    "AIContext",
    "AIContextBuilder",
    "CitationIndex",
    "ContextCapabilities",
    "ContextRecommendation",
    "ContextRegionCost",
    "ContextServiceCost",
    "filter_grounded_citations",
    "render_context_text",
    "validate_cost_service_citation",
    "validate_recommendation_citation",
]
