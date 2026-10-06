"""Mocked end-to-end Phase 4 test.

Demonstrates the full request path WITHOUT any external service:

  FastAPI request
    -> app.api.ai route
      -> AIService.generate_executive_summary
        -> EvidenceGatherer.gather  (returns canned Phase 2/3 data)
        -> AIContextBuilder.build    (bounded deterministic context)
        -> LiteLLMClient.complete    (mocked at httpx boundary)
      <- AIResponse (normalized, validated citations)

The single deterministic recommendation below has ``estimated_monthly_savings = None``;
the test asserts the AI output explicitly states that authoritative savings are
not available rather than inventing a figure.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from app.core.config import Settings
from app.schemas.cost import (
    CostPeriod,
    CostReport,
    DailyCostPoint,
    RegionCost,
    ServiceCost,
)
from app.schemas.optimization import (
    CapabilitiesResponse,
    CapabilityStatus,
    Confidence,
    Recommendation,
    RecommendationAction,
    RecommendationEvidence,
    ResourceType,
    SavingsSource,
    ServiceCapability,
)
from app.services.ai_service import AIService
from app.services.litellm_client import (
    LiteLLMClient,
)


# ---------------------------------------------------------------------------
# Canned Phase 2 + Phase 3 evidence
# ---------------------------------------------------------------------------


def _canned_cost_report() -> CostReport:
    period = CostPeriod(start=date(2025, 9, 1), end=date(2025, 10, 1), days=30)
    previous = CostPeriod(start=date(2025, 8, 2), end=date(2025, 9, 1), days=30)
    return CostReport(
        account_id="111122223333",
        period=period,
        previous_period=previous,
        currency="USD",
        total_cost=Decimal("123.45"),
        previous_period_cost=Decimal("100.00"),
        change_amount=Decimal("23.45"),
        change_percent=Decimal("0.2345"),
        estimated=False,
        daily_trend=[DailyCostPoint(date=date(2025, 9, 1), amount=Decimal("4.10"))],
        by_service=[
            ServiceCost(service="Amazon Elastic Compute Cloud - Compute", amount=Decimal("70.00")),
            ServiceCost(service="Amazon Simple Storage Service", amount=Decimal("30.00")),
            ServiceCost(service="AWS Lambda", amount=Decimal("23.45")),
        ],
        by_region=[RegionCost(region="us-east-1", amount=Decimal("123.45"))],
        source="AWS_COST_EXPLORER",
    )


def _canned_capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        region="us-east-1",
        account_id="111122223333",
        compute_optimizer=ServiceCapability(status=CapabilityStatus.INACTIVE),
        cost_optimization_hub=ServiceCapability(status=CapabilityStatus.NOT_ENROLLED),
        deterministic_engine=ServiceCapability(status=CapabilityStatus.AVAILABLE),
        supported_resource_types=[ResourceType.EBS_VOLUME],
        supported_lookback_days=[7, 30, 60, 90],
        warnings=[],
    )


def _canned_deterministic_recommendation() -> Recommendation:
    """A real deterministic rule recommendation with ``savings=None``."""
    return Recommendation(
        recommendation_id="det-ebs-001",
        resource_id="vol-0123456789abcdef0",
        resource_arn=None,
        resource_type=ResourceType.EBS_VOLUME,
        region="us-east-1",
        account_id="111122223333",
        action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
        title="Review delete unattached EBS volume-0123456789abcdef0",
        finding="EBS volume is in 'available' state with zero attachments.",
        current_configuration={"size_gb": 100, "state": "available", "attachments": 0},
        recommended_configuration={"action": "delete_after_review"},
        estimated_monthly_savings=None,
        currency="USD",
        savings_percentage=None,
        savings_source=SavingsSource.UNKNOWN,
        primary_source=SavingsSource.UNKNOWN,
        sources=[SavingsSource.UNKNOWN],
        confidence=Confidence.HIGH,
        data_quality="high",
        reason_codes=["unused_volume"],
        restart_needed=None,
        rollback_possible=True,
        evidence=[
            RecommendationEvidence(
                source=SavingsSource.UNKNOWN,
                confidence=Confidence.HIGH,
                data={"rule": "rule_unattached_ebs", "size_gb": 100},
                reason_codes=["unused_volume"],
            )
        ],
        aws_recommendation_ids=[],
    )


# ---------------------------------------------------------------------------
# LiteLLM mock — deterministic canned assistant content.
# ---------------------------------------------------------------------------


def _canned_model_response() -> Dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "AWS spend for account 111122223333 in us-east-1 over the last 30 days "
                        "totals $123.45, up $23.45 (23.45%) from the prior period. "
                        "The largest contributors are Amazon EC2 and Amazon S3. "
                        "There is 1 optimization recommendation: review whether volume "
                        "vol-0123456789abcdef0 is still required before making changes. "
                        "Authoritative monthly savings are not available for this recommendation."
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180},
        "model": "cost-detective-free",
    }


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestEndToEndMocked:
    def test_full_request_path_normalizes_response(self) -> None:
        settings = Settings(
            ai_enabled=True,
            litellm_base_url="http://litellm.local:4000",
            litellm_model="cost-detective-free",
            litellm_api_key="sk-test-key",
            ai_request_timeout_seconds=5,
            ai_max_output_tokens=400,
            ai_max_context_recommendations=20,
            ai_max_context_services=15,
            ai_max_context_regions=10,
            ai_max_question_length=2000,
            app_env="test",
        )

        # Mock at the httpx boundary — no real LiteLLM call.
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(200, json=_canned_model_response())

        mock_client = LiteLLMClient(
            settings, transport=httpx.MockTransport(handler)
        )

        rec = _canned_deterministic_recommendation()

        # Inline gatherer — no AWS, no DB.
        class _InlineGatherer:
            def gather(inner_self, *, region: str, days: int):
                return _canned_cost_report(), _canned_capabilities(), [rec]

        service = AIService(settings=settings, client=mock_client, gatherer=_InlineGatherer())

        response = service.generate_executive_summary(region="us-east-1", days=30)

        # Normalized envelope shape.
        assert response.status.value == "SUCCESS"
        assert response.operation == "executive_summary"
        assert response.model == "cost-detective-free"
        assert response.grounding.account_id == "111122223333"
        assert response.grounding.region == "us-east-1"
        assert response.grounding.days == 30
        assert response.grounding.cost_evidence_used is True
        assert response.grounding.recommendations_used == 1

        # Citations: the deterministic recommendation IS in the
        # supplied evidence; the model didn't invent anything.
        recommendation_citations = [
            c for c in response.citations if c.get("type") == "recommendation"
        ]
        assert len(recommendation_citations) == 1
        assert recommendation_citations[0]["id"] == "det-ebs-001"
        assert recommendation_citations[0]["resource_id"] == "vol-0123456789abcdef0"

        # Savings protection: the assistant response explicitly
        # states that authoritative savings are not available rather
        # than inventing a figure.  The recommendation's savings
        # remain None.
        assert rec.estimated_monthly_savings is None
        assert "Authoritative monthly savings are not available" in response.answer
        # No invented dollar amount appears in the model's reply.
        for banned in ("$10/month", "$50/month", "around $", "approximately $"):
            assert banned not in response.answer.lower()

        # No warnings about unsupported citations.
        assert response.warnings == []

    def test_explain_known_recommendation_normalizes_response(self) -> None:
        settings = Settings(
            ai_enabled=True,
            litellm_base_url="http://litellm.local:4000",
            litellm_model="cost-detective-free",
            litellm_api_key="sk-test-key",
            ai_request_timeout_seconds=5,
            ai_max_output_tokens=400,
            ai_max_context_recommendations=20,
            ai_max_context_services=15,
            ai_max_context_regions=10,
            ai_max_question_length=2000,
            app_env="test",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "The deterministic engine flagged "
                                    "vol-0123456789abcdef0 as an unattached EBS volume "
                                    "(state=available, attachments=0, size_gb=100). "
                                    "Review whether this resource is still required "
                                    "before making changes. Authoritative monthly "
                                    "savings are not available for this recommendation."
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "model": "cost-detective-free",
                },
            )

        mock_client = LiteLLMClient(settings, transport=httpx.MockTransport(handler))
        rec = _canned_deterministic_recommendation()

        class _InlineGatherer:
            def gather(inner_self, *, region: str, days: int):
                return _canned_cost_report(), _canned_capabilities(), [rec]

        service = AIService(settings=settings, client=mock_client, gatherer=_InlineGatherer())

        response = service.generate_recommendation_explanation(
            region="us-east-1", days=30, recommendation_id="det-ebs-001"
        )
        assert response.status.value == "SUCCESS"
        assert response.operation == "explain"
        assert "vol-0123456789abcdef0" in response.answer
        # Citation for the explained recommendation.
        assert any(
            c["id"] == "det-ebs-001" and c["resource_id"] == "vol-0123456789abcdef0"
            for c in response.citations
        )

    def test_explain_unknown_recommendation_returns_404_no_llm_call(self) -> None:
        settings = Settings(
            ai_enabled=True,
            litellm_base_url="http://litellm.local:4000",
            litellm_model="cost-detective-free",
            litellm_api_key="sk-test-key",
            ai_request_timeout_seconds=5,
            ai_max_output_tokens=400,
            ai_max_context_recommendations=20,
            ai_max_context_services=15,
            ai_max_context_regions=10,
            ai_max_question_length=2000,
            app_env="test",
        )

        # LiteLLM mock that REJECTS any call.
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("LiteLLM was called but should not have been.")

        mock_client = LiteLLMClient(settings, transport=httpx.MockTransport(handler))
        rec = _canned_deterministic_recommendation()

        class _InlineGatherer:
            def gather(inner_self, *, region: str, days: int):
                return _canned_cost_report(), _canned_capabilities(), [rec]

        service = AIService(settings=settings, client=mock_client, gatherer=_InlineGatherer())
        response = service.generate_recommendation_explanation(
            region="us-east-1", days=30, recommendation_id="det-does-not-exist"
        )
        assert response.status.value == "FAILED"
        assert "RecommendationNotFound" in response.warnings
