"""Phase 7D.4 — multi-cloud AI FinOps Analyst unit tests.

Focused unit tests on the deterministic contracts:

* System prompt: grounding rules present, no credential markers.
* FinOps cost slice: preserves unavailable values, no fabrication.
* Context classification: LIVE / PARTIAL / UNAVAILABLE.
* Request schema: rejects malformed provider / question.

Integration paths (HTTP routing, real LiteLLM call, real
adapter fan-out) are exercised by the existing Phase 7C.2B cost
tests + Phase 4 AI tests.  We deliberately keep this file at
the pure-unit layer so the suite runs without live services.
"""
from __future__ import annotations

import os
import pytest

# Required settings before app imports.
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

from app.schemas.cloud_ai import FinOpsAIProvider, FinOpsAnalyzeRequest
from app.services.cloud.finops_prompt import SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Prompt grounding — must NEVER carry credentials / tokens.
# ---------------------------------------------------------------------------


def test_system_prompt_contains_grounding_rules():
    for marker in [
        "MUST NOT invent",
        "data_freshness",
        "savings_period",
        "ANNUAL",
        "MONTHLY",
        "read-only",
        "do NOT claim any change has been applied",
    ]:
        assert marker in SYSTEM_PROMPT, f"prompt missing marker: {marker}"


def test_system_prompt_does_not_include_credential_markers():
    for forbidden in [
        "Bearer ",
        "Authorization:",
        "AWS_ACCESS_KEY",
        "AWS_SECRET",
        "AZURE_CLIENT_SECRET",
        "ENTRA_TOKEN",
    ]:
        assert forbidden not in SYSTEM_PROMPT, (
            f"prompt must not contain {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# Request schema — provider / question validation.
# ---------------------------------------------------------------------------


def test_request_accepts_valid_provider_aws():
    req = FinOpsAnalyzeRequest(
        provider=FinOpsAIProvider.AWS, scope="all",
        question="How much are we spending?",
    )
    assert req.provider == FinOpsAIProvider.AWS
    assert req.scope == "all"


def test_request_accepts_valid_provider_azure():
    req = FinOpsAnalyzeRequest(
        provider=FinOpsAIProvider.AZURE, scope="sub:12345",
        question="What is happening in Azure?",
    )
    assert req.provider == FinOpsAIProvider.AZURE
    assert req.scope == "sub:12345"


def test_request_accepts_empty_scope_defaults_to_all():
    req = FinOpsAnalyzeRequest(
        provider=FinOpsAIProvider.AWS, scope="",
        question="How much are we spending?",
    )
    assert req.scope == ""


def test_request_rejects_invalid_provider():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FinOpsAnalyzeRequest(
            provider="GCP",  # type: ignore[arg-type]
            scope="all",
            question="How much are we spending?",
        )


def test_request_rejects_short_question():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FinOpsAnalyzeRequest(
            provider=FinOpsAIProvider.AWS, scope="all", question="hi",
        )


def test_request_rejects_empty_question():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FinOpsAnalyzeRequest(
            provider=FinOpsAIProvider.AWS, scope="all", question="",
        )


def test_request_rejects_too_long_question():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        FinOpsAnalyzeRequest(
            provider=FinOpsAIProvider.AWS, scope="all",
            question="x" * 2001,
        )


# ---------------------------------------------------------------------------
# FinOps cost slice — preserves unavailable values, no fabrication.
# ---------------------------------------------------------------------------


@pytest.fixture
def cost_envelope_dict():
    return {
        "provider": "AWS",
        "scope": "all",
        "scope_kind": "all",
        "status": "SUCCESS",
        "currency_status": "OK",
        "account_id": None,
        "account_display_name": None,
        "period": {"start": "2026-10-01", "end": "2026-11-01", "days": 31},
        "previous_period": {"start": "2026-09-01", "end": "2026-10-01", "days": 30},
        "currency": "USD",
        "total_cost": "0.00",
        "previous_period_cost": "0.00",
        "change_amount": "0.00",
        "change_percent": None,
        "estimated": False,
        "daily_trend": [],
        "by_service": [
            {"service": "EC2", "amount": "0.00", "unit": "USD"},
        ],
        "by_region": [
            {"region": "us-east-1", "amount": "0.00", "unit": "USD"},
        ],
        "by_account": [],
        "account_errors": [],
        "source": "AWS_COST_EXPLORER",
        "successful_accounts": 1,
        "failed_accounts": 0,
        "previous_period_unavailable": False,
    }


def test_slice_preserves_genuine_zero(cost_envelope_dict):
    from decimal import Decimal
    from app.schemas.cost import CloudCostReport
    from app.services.cloud.finops_context import FinOpsContextBuilder

    builder = FinOpsContextBuilder()
    envelope = CloudCostReport.model_validate(cost_envelope_dict)
    slice_cost = builder._slice_cost(envelope)
    assert slice_cost.total_cost == Decimal("0.00")
    assert slice_cost.status == "SUCCESS"
    assert slice_cost.successful_accounts == 1


def test_slice_preserves_unavailable_previous_period(cost_envelope_dict):
    from decimal import Decimal
    from app.schemas.cost import CloudCostReport
    from app.services.cloud.finops_context import FinOpsContextBuilder

    cost_envelope_dict["previous_period_cost"] = None
    cost_envelope_dict["change_amount"] = None
    cost_envelope_dict["change_percent"] = None
    cost_envelope_dict["previous_period_unavailable"] = True

    builder = FinOpsContextBuilder()
    envelope = CloudCostReport.model_validate(cost_envelope_dict)
    slice_cost = builder._slice_cost(envelope)
    assert slice_cost.previous_period_cost is None
    assert slice_cost.change_amount is None
    assert slice_cost.change_percent is None
    assert slice_cost.previous_period_unavailable is True


def test_classify_partial_when_one_source_unavailable():
    from app.services.cloud.finops_context import (
        FinOpsContextBuilder,
        FinOpsContextStatus,
        FinOpsCostSlice,
        FinOpsResourceSlice,
        FinOpsRecommendationSlice,
    )
    builder = FinOpsContextBuilder()
    warnings: list = []
    cost = FinOpsCostSlice(
        currency="USD", total_cost=__import__("decimal").Decimal("10"),
        previous_period_cost=None, change_amount=None, change_percent=None,
        previous_period_unavailable=False, daily_trend=[], top_services=[],
        top_regions=[], by_account=[], account_errors=[],
        successful_accounts=1, failed_accounts=0, status="SUCCESS",
    )
    resources = FinOpsResourceSlice(total=10, by_type=[], by_region=[], by_subscription=[], status="SUCCESS")
    recs = FinOpsRecommendationSlice(
        count=1, estimated_savings=__import__("decimal").Decimal("5"), currency="USD",
        savings_period="MONTHLY", top_items=[], sources=["AWS_COMPUTE_OPTIMIZER"],
    )
    partial, unavailable = builder._classify_availability(cost, resources, recs, warnings)
    # When all three sources are present and authoritative, status
    # is LIVE, not PARTIAL.
    assert unavailable == []
    assert partial == []
    assert builder._resolve_status(
        partial, unavailable, "LIVE",
    ) == "LIVE"


def test_classify_unavailable_when_all_sources_fail():
    from app.services.cloud.finops_context import FinOpsContextBuilder
    builder = FinOpsContextBuilder()
    warnings: list = []
    partial, unavailable = builder._classify_availability(None, None, None, warnings)
    assert "cost" in unavailable
    assert "resources" in unavailable
    assert "recommendations" in unavailable
    # When all sources are unavailable, status is UNAVAILABLE.
    from app.services.cloud.finops_context import FinOpsContextStatus
    assert builder._resolve_status(partial, unavailable, "UNAVAILABLE") == \
        FinOpsContextStatus.UNAVAILABLE


def test_partial_when_recommendations_empty():
    from app.services.cloud.finops_context import (
        FinOpsContextBuilder,
        FinOpsContextStatus,
        FinOpsCostSlice,
        FinOpsResourceSlice,
        FinOpsRecommendationSlice,
    )
    from decimal import Decimal
    builder = FinOpsContextBuilder()
    warnings: list = []
    cost = FinOpsCostSlice(
        currency="USD", total_cost=Decimal("100"), previous_period_cost=None,
        change_amount=None, change_percent=None, previous_period_unavailable=False,
        daily_trend=[], top_services=[], top_regions=[], by_account=[],
        account_errors=[], successful_accounts=1, failed_accounts=0, status="SUCCESS",
    )
    resources = FinOpsResourceSlice(total=10, by_type=[], by_region=[], by_subscription=[], status="SUCCESS")
    recs = FinOpsRecommendationSlice(
        count=0, estimated_savings=None, currency=None,
        savings_period="MONTHLY", top_items=[], sources=[],
    )
    partial, unavailable = builder._classify_availability(cost, resources, recs, warnings)
    assert "recommendations" in partial
    assert builder._resolve_status(
        partial, unavailable, "LIVE",
    ) == FinOpsContextStatus.PARTIAL


# ---------------------------------------------------------------------------
# Prompt rendering — never embeds credentials / auth tokens.
# ---------------------------------------------------------------------------


def test_render_does_not_leak_secrets():
    from decimal import Decimal
    from app.services.cloud.finops_ai_service import FinOpsAIService
    from app.services.cloud.finops_context import (
        FinOpsContext,
        FinOpsContextStatus,
        FinOpsCostSlice,
    )
    svc = FinOpsAIService()
    cost = FinOpsCostSlice(
        currency="USD", total_cost=Decimal("100"),
        previous_period_cost=None, change_amount=None, change_percent=None,
        previous_period_unavailable=False, daily_trend=[],
        top_services=[{"service": "EC2", "amount": "100.00", "currency": "USD"}],
        top_regions=[{"region": "us-east-1", "amount": "100.00", "currency": "USD"}],
        by_account=[],
        account_errors=[], successful_accounts=1, failed_accounts=0,
        status="SUCCESS",
    )
    ctx = FinOpsContext(
        provider=FinOpsAIProvider.AWS, scope="all",
        account_display_name=None,
        period=None,
        data_freshness="LIVE",
        status=FinOpsContextStatus.LIVE,
        cost=cost, resources=None, recommendations=None,
    )
    rendered = svc._render_context(ctx)
    assert "Bearer " not in rendered
    assert "AWS_ACCESS_KEY_ID" not in rendered
    assert "AZURE_CLIENT_SECRET" not in rendered
