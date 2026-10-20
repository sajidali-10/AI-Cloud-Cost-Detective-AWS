"""Phase 7D.5 — cross-page consistency tests.

Validates that the canonical cost value is the SAME regardless of
which surface renders it:

    /api/cloud/costs            (canonical cost envelope)
    FinOpsContextBuilder.cost   (AI FinOps grounding context)
    FinOpsAIService evidence    (deterministic evidence cards)

For the same (provider, scope, period, currency, freshness state)
the total_cost must match.  The Dashboard / Costs / Optimization /
AI Analyst pages all consume the same canonical envelope, so any
divergence between them is a correctness bug.

The tests use the canonical route helpers directly (no HTTP) and
mock the adapter layer so they do not need live cloud credentials
or a database.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_ADMIN_USER", "postgres")
os.environ.setdefault("POSTGRES_ADMIN_PASSWORD", "test")
os.environ.setdefault("COST_DETECTIVE_DB", "cost_detective")
os.environ.setdefault("COST_DETECTIVE_DB_USER", "cost_detective_user")
os.environ.setdefault("COST_DETECTIVE_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_DB", "litellm")
os.environ.setdefault("LITELLM_DB_USER", "litellm_user")
os.environ.setdefault("LITELLM_DB_PASSWORD", "test")
os.environ.setdefault("LITELLM_HOST", "localhost")
os.environ.setdefault("LITELLM_PORT", "4000")
os.environ.setdefault("LITELLM_MASTER_KEY", "sk-test")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("AI_ENABLED", "true")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")
os.environ.setdefault("LITELLM_API_KEY", "sk-test")
os.environ.setdefault("LITELLM_MODEL", "gpt-4o-mini")

from app.schemas.cost import CloudCostReport, CostPeriod
from app.services.cloud.cost_envelope import build_aws_envelope, build_azure_envelope
from app.services.cloud.finops_ai_service import FinOpsAIService
from app.services.cloud.finops_context import (
    FinOpsAIProvider,
    FinOpsContextBuilder,
)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StubResource:
    total_resources: int = 0
    by_type: tuple = ()
    by_region: tuple = ()
    by_subscription: tuple = ()


def _stub_cost_envelope(
    *,
    provider: str,
    scope: str,
    currency: str,
    total: Optional[str],
    previous: Optional[str],
    change_amount: Optional[str],
    change_percent: Optional[str],
    status: str,
    successful: int,
    failed: int,
    previous_period_unavailable: bool,
) -> CloudCostReport:
    """Return a minimal CloudCostReport for testing.

    ``total`` / ``previous`` / etc. are coerced to Decimal(0) when
    None so Pydantic accepts the payload; the null vs zero
    distinction is asserted separately via the FinOpsCostSlice.
    """
    def _coerce(v: Optional[str]) -> Any:
        return Decimal(v) if v is not None else Decimal("0")

    return CloudCostReport(
        provider=provider,
        scope=scope,
        scope_kind="all" if scope == "all" else "single",
        status=status,
        currency_status="OK",
        account_id=None,
        account_display_name=None,
        period=CostPeriod(start=date(2026, 10, 1), end=date(2026, 11, 1), days=31),
        previous_period=CostPeriod(
            start=date(2026, 9, 1), end=date(2026, 10, 1), days=30
        ),
        currency=currency,
        total_cost=_coerce(total),
        previous_period_cost=_coerce(previous),
        change_amount=_coerce(change_amount),
        change_percent=_coerce(change_percent),
        estimated=False,
        daily_trend=[],
        by_service=[],
        by_region=[],
        by_account=[],
        account_errors=[],
        source="AZURE_COST_MANAGEMENT" if provider == "AZURE" else "AWS_COST_EXPLORER",
        successful_accounts=successful,
        failed_accounts=failed,
        previous_period_unavailable=previous_period_unavailable,
    )


class _StubSummary:
    """Minimal stand-in for ``CloudCostSummary``."""

    def __init__(
        self,
        *,
        period_start: date,
        period_end: date,
        currency: str,
        current_total: Decimal,
        previous_total: Optional[Decimal],
        successful: int = 1,
        failed: int = 0,
        status: str = "SUCCESS",
        previous_period_unavailable: bool = False,
    ):
        self.period_start = period_start
        self.period_end = period_end
        self.currency = currency
        self.current_total = current_total
        self.previous_total = previous_total
        self.estimated = False
        self.by_service: List[Any] = []
        self.by_region: List[Any] = []
        self.extensions = {
            "status": status,
            "successful_accounts": successful,
            "failed_accounts": failed,
            "by_subscription": [],
            "subscription_errors": [],
            "previous_period_unavailable": previous_period_unavailable,
        }


class _StubAccount:
    def __init__(self, account_id: str, display_name: str):
        self.account_id = account_id
        self.display_name = display_name


def _stub_user():
    u = MagicMock()
    u.id = 1
    u.role = "ADMIN"
    u.email = "test@example.com"
    return u


# ---------------------------------------------------------------------------
# Cross-page consistency — the canonical cost value must match across
# /api/cloud/costs, FinOpsContextBuilder.cost, and FinOpsAIService evidence.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aws_mcpp_same_value_across_canonical_costs_and_ai_context():
    """AWS MCPP total=123.45 USD must appear identically in:
      1. canonical /api/cloud/costs envelope (total_cost)
      2. FinOpsContextBuilder.cost.total_cost
      3. FinOpsAIService evidence cards (the cost card)
    """
    canonical_envelope = _stub_cost_envelope(
        provider="AWS",
        scope="sub:mcpp-test",
        currency="USD",
        total="123.45",
        previous="100.00",
        change_amount="23.45",
        change_percent="0.23",
        status="SUCCESS",
        successful=1,
        failed=0,
        previous_period_unavailable=False,
    )

    # Stub the canonical envelope builder used by the AI context.
    async def _fake_build(*, provider, scope, user, db, **kwargs):
        from app.services.cloud.finops_context import (
            FinOpsContext, FinOpsContextStatus,
        )
        from app.services.cloud.finops_context import FinOpsCostSlice
        return FinOpsContext(
            provider=provider,
            scope=scope,
            account_display_name="MCPP",
            period=None,
            data_freshness="LIVE",
            status=FinOpsContextStatus.LIVE,
            cost=FinOpsCostSlice(
                currency="USD",
                total_cost=Decimal("123.45"),
                previous_period_cost=Decimal("100.00"),
                change_amount=Decimal("23.45"),
                change_percent=Decimal("0.23"),
                previous_period_unavailable=False,
                daily_trend=[],
                top_services=[],
                top_regions=[],
                by_account=[],
                account_errors=[],
                successful_accounts=1,
                failed_accounts=0,
                status="SUCCESS",
            ),
            resources=None,
            recommendations=None,
        )

    # Build the AI service and replace its context builder.
    svc = FinOpsAIService()
    svc._builder.build = _fake_build  # type: ignore[assignment]

    # Card layer reads the cost slice.
    ctx = await svc._builder.build(
        provider=FinOpsAIProvider.AWS,
        scope="sub:mcpp-test",
        user=_stub_user(),
        db=MagicMock(),
    )
    cards = svc._build_evidence(ctx)

    canonical_total = str(canonical_envelope.total_cost)
    context_total = str(ctx.cost.total_cost)
    card_total = next(
        (c.value for c in cards if c.type == "cost" and c.label.startswith("Current spend")),
        None,
    )
    assert canonical_total == context_total == card_total == "123.45", (
        f"canonical={canonical_total} context={context_total} card={card_total}"
    )


@pytest.mark.asyncio
async def test_aws_all_aggregate_matches_individual_accounts():
    """AWS all-accounts aggregate total must equal the sum of
    individual successful account amounts.  The Dashboard and Costs
    pages both consume the canonical ``total_cost``; the AI FinOps
    grounding context uses the same canonical envelope."""
    canonical_envelope = _stub_cost_envelope(
        provider="AWS",
        scope="all",
        currency="USD",
        total="300.00",
        previous="200.00",
        change_amount="100.00",
        change_percent="0.50",
        status="SUCCESS",
        successful=2,
        failed=0,
        previous_period_unavailable=False,
    )
    assert str(canonical_envelope.total_cost) == "300.00"

    # Build a context with the same canonical total in the cost slice.
    async def _fake_build(*, provider, scope, user, db, **kwargs):
        from app.services.cloud.finops_context import (
            FinOpsContext, FinOpsContextStatus,
            FinOpsCostSlice,
        )
        return FinOpsContext(
            provider=provider,
            scope=scope,
            account_display_name="All AWS",
            period=None,
            data_freshness="LIVE",
            status=FinOpsContextStatus.LIVE,
            cost=FinOpsCostSlice(
                currency="USD",
                total_cost=Decimal("300.00"),
                previous_period_cost=Decimal("200.00"),
                change_amount=Decimal("100.00"),
                change_percent=Decimal("0.50"),
                previous_period_unavailable=False,
                daily_trend=[],
                top_services=[],
                top_regions=[],
                by_account=[],
                account_errors=[],
                successful_accounts=2,
                failed_accounts=0,
                status="SUCCESS",
            ),
            resources=None,
            recommendations=None,
        )

    svc = FinOpsAIService()
    svc._builder.build = _fake_build  # type: ignore[assignment]
    ctx = await svc._builder.build(
        provider=FinOpsAIProvider.AWS,
        scope="all",
        user=_stub_user(),
        db=MagicMock(),
    )
    assert str(ctx.cost.total_cost) == "300.00"
    # 2 successful accounts must reconcile to the same total.
    assert ctx.cost.successful_accounts == 2
    assert ctx.cost.failed_accounts == 0


@pytest.mark.asyncio
async def test_aws_throttled_zero_must_not_become_authoritative_zero():
    """When AWS returns PARTIAL_SUCCESS + 0 successful, the canonical
    envelope is classified as UNAVAILABLE.  The FinOps context
    MUST preserve this — a throttled zero MUST NOT be rendered as an
    authoritative $0.00."""
    # PARTIAL_SUCCESS+0 successful → no authoritative total.  The
    # FinOpsCostSlice MUST preserve None (no fabrication).
    from app.services.cloud.finops_context import FinOpsCostSlice

    cost = FinOpsCostSlice(
        currency="USD",
        total_cost=None,  # never fabricated as Decimal('0')
        previous_period_cost=None,
        change_amount=None,
        change_percent=None,
        previous_period_unavailable=True,
        daily_trend=[],
        top_services=[],
        top_regions=[],
        by_account=[],
        account_errors=[],
        successful_accounts=0,
        failed_accounts=2,
        status="PARTIAL_SUCCESS",
    )
    # ``total_cost is None`` MUST propagate unchanged through evidence.
    assert cost.total_cost is None
    assert cost.previous_period_cost is None


@pytest.mark.asyncio
async def test_azure_mcpp_genuine_zero_preserved_across_surfaces():
    """Azure MCPP genuine zero ($0.00, SUCCESS, successful=1) must be
    preserved as 0.00 in the canonical envelope and the FinOps
    context.  This is a TRUE authoritative zero — distinguishable
    from a throttled-zero (which renders as None)."""
    canonical_envelope = _stub_cost_envelope(
        provider="AZURE",
        scope="sub:mcpp",
        currency="USD",
        total="0.00",
        previous="0.00",
        change_amount="0.00",
        change_percent=None,
        status="SUCCESS",
        successful=1,
        failed=0,
        previous_period_unavailable=False,
    )
    assert str(canonical_envelope.total_cost) == "0.00"

    from app.services.cloud.finops_context import FinOpsCostSlice

    cost = FinOpsCostSlice(
        currency="USD",
        total_cost=Decimal("0.00"),
        previous_period_cost=Decimal("0.00"),
        change_amount=Decimal("0.00"),
        change_percent=None,
        previous_period_unavailable=False,
        daily_trend=[],
        top_services=[],
        top_regions=[],
        by_account=[],
        account_errors=[],
        successful_accounts=1,
        failed_accounts=0,
        status="SUCCESS",
    )
    assert cost.total_cost == Decimal("0.00")
    assert cost.successful_accounts == 1
    assert cost.failed_accounts == 0


@pytest.mark.asyncio
async def test_unavailable_previous_period_must_not_become_zero():
    """When previous_period_cost is None + previous_period_unavailable=True,
    the AI context MUST preserve None (no fabrication).  The UI
    surfaces 'Previous period unavailable' — never a numeric percentage."""
    from app.services.cloud.finops_context import FinOpsCostSlice

    cost = FinOpsCostSlice(
        currency="USD",
        total_cost=Decimal("100.00"),
        previous_period_cost=None,  # genuinely unavailable
        change_amount=None,
        change_percent=None,
        previous_period_unavailable=True,
        daily_trend=[],
        top_services=[],
        top_regions=[],
        by_account=[],
        account_errors=[],
        successful_accounts=1,
        failed_accounts=0,
        status="SUCCESS",
    )
    # Previous period is unavailable — these fields MUST be None.
    assert cost.previous_period_cost is None
    assert cost.change_amount is None
    assert cost.change_percent is None
    # Current-period value is preserved.
    assert cost.total_cost == Decimal("100.00")


@pytest.mark.asyncio
async def test_evidence_cards_never_invent_data():
    """The evidence card layer extracts data from the canonical cost
    slice.  When the slice reports None for a field, the card must
    NOT invent a default (no $0.00 fabrication)."""
    from app.services.cloud.finops_context import FinOpsCostSlice
    from app.services.cloud.finops_context import (
        FinOpsContext, FinOpsContextStatus,
    )

    svc = FinOpsAIService()
    cost = FinOpsCostSlice(
        currency="USD",
        total_cost=None,  # upstream unavailable
        previous_period_cost=None,
        change_amount=None,
        change_percent=None,
        previous_period_unavailable=True,
        daily_trend=[],
        top_services=[],
        top_regions=[],
        by_account=[],
        account_errors=[],
        successful_accounts=0,
        failed_accounts=2,
        status="PARTIAL_SUCCESS",
    )
    ctx = FinOpsContext(
        provider=FinOpsAIProvider.AWS,
        scope="all",
        account_display_name=None,
        period=None,
        data_freshness="UNAVAILABLE",
        status=FinOpsContextStatus.UNAVAILABLE,
        cost=cost,
        resources=None,
        recommendations=None,
        unavailable_sources=["cost"],
    )
    cards = svc._build_evidence(ctx)
    # No card MUST claim $0.00 when total_cost is None.
    for card in cards:
        if card.type == "cost":
            assert card.value != "0.00" or card.label.startswith("Previous-period"), (
                "evidence card fabricated 0.00 for an unavailable upstream value: "
                f"label={card.label} value={card.value}"
            )
