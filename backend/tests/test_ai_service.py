"""Tests for the AI service + FastAPI router — Phase 4.

These tests construct the AI service with fake collaborators
(``FakeLiteLLMClient``, ``FakeEvidenceGatherer``) so the entire
path can be exercised without a real LiteLLM gateway or AWS calls.

The FastAPI TestClient is used for the router-level tests so the
real ``AIService`` is wired up (via the
``app.api.ai._service_factory`` injection point) against the fake
collaborators.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.schemas.ai import (
    AIGenerationStatus,
    AIStatus,
)
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
from app.services.ai_context_builder import AIContextBuilder
from app.services.ai_service import AIService
from app.services.litellm_client import (
    ChatMessage,
    CompletionResult,
    LiteLLMClient,
    LiteLLMEmptyCompletion,
    LiteLLMProviderError,
    LiteLLMRateLimit,
    LiteLLMTimeout,
    LiteLLMUnavailable,
)


# ---------------------------------------------------------------------------
# Test fixtures (fakes)
# ---------------------------------------------------------------------------


def _settings(
    *, ai_enabled: bool = True, **overrides: Any
) -> Settings:
    base: Dict[str, Any] = {
        "ai_enabled": ai_enabled,
        "litellm_base_url": "http://litellm.local:4000",
        "litellm_model": "cost-detective-free",
        "litellm_api_key": "sk-test",
        "ai_request_timeout_seconds": 5,
        "ai_max_output_tokens": 400,
        "ai_max_context_recommendations": 20,
        "ai_max_context_services": 15,
        "ai_max_context_regions": 10,
        "ai_max_question_length": 100,
    }
    base.update(overrides)
    return Settings(**base)


def _cost_report() -> CostReport:
    period = CostPeriod(start=date(2025, 9, 1), end=date(2025, 10, 1), days=30)
    previous = CostPeriod(start=date(2025, 8, 2), end=date(2025, 9, 1), days=30)
    return CostReport(
        account_id="111122223333",
        period=period,
        previous_period=previous,
        currency="USD",
        total_cost=Decimal("100.00"),
        previous_period_cost=Decimal("80.00"),
        change_amount=Decimal("20.00"),
        change_percent=Decimal("0.25"),
        estimated=False,
        daily_trend=[DailyCostPoint(date=date(2025, 9, 1), amount=Decimal("3.20"))],
        by_service=[
            ServiceCost(service="Amazon EC2", amount=Decimal("60.00")),
            ServiceCost(service="Amazon S3", amount=Decimal("40.00")),
        ],
        by_region=[RegionCost(region="us-east-1", amount=Decimal("100.00"))],
        source="AWS_COST_EXPLORER",
    )


def _capabilities() -> CapabilitiesResponse:
    return CapabilitiesResponse(
        region="us-east-1",
        account_id="111122223333",
        compute_optimizer=ServiceCapability(status=CapabilityStatus.INACTIVE),
        cost_optimization_hub=ServiceCapability(status=CapabilityStatus.NOT_ENROLLED),
        deterministic_engine=ServiceCapability(status=CapabilityStatus.AVAILABLE),
        supported_resource_types=[ResourceType.EC2, ResourceType.EBS_VOLUME],
        supported_lookback_days=[7, 30, 60, 90],
        warnings=[],
    )


def _recommendation(
    *,
    rid: str = "det-1",
    resource_id: str = "vol-aaa",
    estimated_monthly_savings: Optional[Decimal] = None,
    confidence: Confidence = Confidence.HIGH,
    savings_source: SavingsSource = SavingsSource.UNKNOWN,
    action: RecommendationAction = RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
    resource_type: ResourceType = ResourceType.EBS_VOLUME,
) -> Recommendation:
    return Recommendation(
        recommendation_id=rid,
        resource_id=resource_id,
        resource_arn=None,
        resource_type=resource_type,
        region="us-east-1",
        account_id="111122223333",
        action=action,
        title=f"{action.value} {resource_type.value} {resource_id}",
        finding="Detected via deterministic rule.",
        current_configuration={"size_gb": 100, "state": "available"},
        recommended_configuration={"action": "delete"},
        estimated_monthly_savings=estimated_monthly_savings,
        currency="USD",
        savings_percentage=None,
        savings_source=savings_source,
        primary_source=savings_source,
        sources=[savings_source],
        confidence=confidence,
        data_quality="high",
        reason_codes=["unused_volume"],
        restart_needed=None,
        rollback_possible=True,
        evidence=[
            RecommendationEvidence(
                source=savings_source,
                confidence=confidence,
                data={"rule": "rule_unattached_ebs"},
                reason_codes=["unused_volume"],
            )
        ],
        aws_recommendation_ids=[],
    )


class FakeLiteLLMClient:
    """Replacement for :class:`LiteLLMClient` that never touches the network."""

    def __init__(
        self,
        *,
        completions: Optional[Sequence[CompletionResult]] = None,
        error: Optional[Exception] = None,
        health: bool = True,
    ) -> None:
        self._completions = list(completions or [])
        self._error = error
        self._health = health
        self.complete_calls: List[Tuple[List[ChatMessage], Optional[int], float]] = []
        self.health_calls = 0

    def health_check(self) -> bool:
        self.health_calls += 1
        return self._health

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: Optional[int] = None,
        temperature: float = 0.2,
    ) -> CompletionResult:
        self.complete_calls.append((list(messages), max_tokens, temperature))
        if self._error is not None:
            raise self._error
        if not self._completions:
            return CompletionResult(content="(no canned completion)", model="test-model")
        return self._completions.pop(0)


class FakeEvidenceGatherer:
    def __init__(
        self,
        *,
        cost_report: Optional[CostReport] = None,
        capabilities: Optional[CapabilitiesResponse] = None,
        recommendations: Optional[List[Recommendation]] = None,
        raise_exc: Optional[Exception] = None,
    ) -> None:
        self.cost_report = cost_report
        self.capabilities = capabilities
        self.recommendations = list(recommendations or [])
        self.raise_exc = raise_exc
        self.calls: List[Tuple[str, int]] = []

    def gather(
        self, *, region: str, days: int
    ) -> Tuple[
        Optional[CostReport],
        Optional[CapabilitiesResponse],
        List[Recommendation],
    ]:
        self.calls.append((region, days))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.cost_report, self.capabilities, list(self.recommendations)


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    def test_status_reports_disabled(self) -> None:
        service = AIService(settings=_settings(ai_enabled=False))
        status = service.status()
        assert status.status == AIStatus.DISABLED
        assert status.ai_enabled is False
        assert status.litellm_reachable is False

    def test_generation_when_disabled_returns_disabled_status(self) -> None:
        client = FakeLiteLLMClient()
        service = AIService(
            settings=_settings(ai_enabled=False),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.DISABLED
        # Crucial: no LLM call was made.
        assert client.complete_calls == []

    def test_disabled_mode_does_not_contact_provider(self) -> None:
        client = FakeLiteLLMClient()
        service = AIService(
            settings=_settings(ai_enabled=False),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        service.generate_analysis(
            region="us-east-1",
            days=30,
            question="Why did our bill rise?",
        )
        assert client.complete_calls == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestExecutiveSummary:
    def test_executive_summary_success(self) -> None:
        rec = _recommendation()
        client = FakeLiteLLMClient(
            completions=[
                CompletionResult(
                    content="This is a grounded summary.",
                    model="cost-detective-free",
                    prompt_tokens=42,
                    completion_tokens=8,
                    total_tokens=50,
                )
            ]
        )
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[rec],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.SUCCESS
        assert response.answer == "This is a grounded summary."
        assert response.model == "cost-detective-free"
        assert response.grounding.days == 30
        assert response.grounding.recommendations_used == 1
        assert response.grounding.cost_evidence_used is True
        # Citations: 1 recommendation + up to 5 services.
        assert any(
            c["type"] == "recommendation" and c["id"] == "det-1" for c in response.citations
        )
        assert any(c["type"] == "cost_service" for c in response.citations)
        # The system prompt + evidence + question delimiters were sent.
        sent_messages = client.complete_calls[0][0]
        assert any(m.role == "system" for m in sent_messages)
        user_msg = next(m for m in sent_messages if m.role == "user")
        assert "<aws_evidence>" in user_msg.content
        assert "</aws_evidence>" in user_msg.content
        assert "<user_question>" in user_msg.content
        assert "</user_question>" in user_msg.content


class TestAnalyze:
    def test_qa_success(self) -> None:
        client = FakeLiteLLMClient(
            completions=[
                CompletionResult(content="Top service: Amazon EC2.", model="cost-detective-free")
            ]
        )
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_analysis(
            region="us-east-1", days=30, question="Which services cost the most?"
        )
        assert response.status == AIGenerationStatus.SUCCESS
        assert "EC2" in response.answer

    def test_question_too_long_rejected(self) -> None:
        service = AIService(settings=_settings(ai_max_question_length=10))
        with pytest.raises(ValueError):
            service.validate_question("x" * 50)

    def test_empty_question_rejected(self) -> None:
        service = AIService(settings=_settings())
        with pytest.raises(ValueError):
            service.validate_question("")
        with pytest.raises(ValueError):
            service.validate_question("   ")

    def test_unsupported_lookback_rejected(self) -> None:
        service = AIService(settings=_settings())
        with pytest.raises(ValueError):
            service.validate_lookback(15)
        # Allowed values pass.
        for d in (7, 30, 60, 90):
            service.validate_lookback(d)  # should not raise


# ---------------------------------------------------------------------------
# Recommendation explanation
# ---------------------------------------------------------------------------


class TestRecommendationExplanation:
    def test_explain_existing_recommendation(self) -> None:
        rec = _recommendation(rid="det-known", resource_id="vol-known")
        client = FakeLiteLLMClient(
            completions=[
                CompletionResult(
                    content="This resource is an unattached EBS volume of 100 GB.",
                    model="cost-detective-free",
                )
            ]
        )
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[rec],
            ),
        )
        response = service.generate_recommendation_explanation(
            region="us-east-1", days=30, recommendation_id="det-known"
        )
        assert response.status == AIGenerationStatus.SUCCESS
        # Citation for the recommendation is included.
        assert any(
            c["type"] == "recommendation" and c["id"] == "det-known"
            for c in response.citations
        )

    def test_explain_nonexistent_recommendation_returns_failed(self) -> None:
        client = FakeLiteLLMClient()
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation(rid="det-known")],
            ),
        )
        response = service.generate_recommendation_explanation(
            region="us-east-1", days=30, recommendation_id="det-missing"
        )
        assert response.status == AIGenerationStatus.FAILED
        assert "RecommendationNotFound" in response.warnings
        # No LLM call when the recommendation does not exist.
        assert client.complete_calls == []


# ---------------------------------------------------------------------------
# Provider failure paths
# ---------------------------------------------------------------------------


class TestProviderFailures:
    def test_timeout_yields_partial_success_without_crash(self) -> None:
        client = FakeLiteLLMClient(error=LiteLLMTimeout("LiteLLM request timed out."))
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.PARTIAL_SUCCESS
        assert "litellm_timeout" in response.warnings

    def test_rate_limit_yields_partial_success(self) -> None:
        client = FakeLiteLLMClient(error=LiteLLMRateLimit("Too many requests."))
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_analysis(
            region="us-east-1", days=30, question="Why?"
        )
        assert response.status == AIGenerationStatus.PARTIAL_SUCCESS
        assert "litellm_rate_limit" in response.warnings

    def test_unavailable_yields_unavailable(self) -> None:
        client = FakeLiteLLMClient(error=LiteLLMUnavailable("LiteLLM gateway is unreachable."))
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.UNAVAILABLE
        assert "litellm_unavailable" in response.warnings

    def test_provider_error_yields_unavailable(self) -> None:
        client = FakeLiteLLMClient(error=LiteLLMProviderError("LiteLLM upstream provider error."))
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.UNAVAILABLE
        assert "litellm_provider_error" in response.warnings

    def test_empty_completion_yields_unavailable(self) -> None:
        client = FakeLiteLLMClient(error=LiteLLMEmptyCompletion("LiteLLM returned no content."))
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        response = service.generate_executive_summary(region="us-east-1", days=30)
        assert response.status == AIGenerationStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Citation validation against supplied evidence
# ---------------------------------------------------------------------------


class TestCitationValidationEndToEnd:
    def test_invented_recommendation_id_discarded(self) -> None:
        client = FakeLiteLLMClient(
            completions=[CompletionResult(content="Grounded.", model="cost-detective-free")]
        )
        # Inject an extra citation that references an id NOT in the
        # evidence.  The explain path adds the targeted
        # recommendation, so we use the analyze path with a forged
        # gatherer response that the service cannot influence.  Instead,
        # we patch the AI service to inject a forged citation into the
        # post-processing step.
        from app.services.ai_service import AIService

        rec = _recommendation(rid="det-real", resource_id="vol-real")
        service = AIService(
            settings=_settings(),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[rec],
            ),
        )
        # Monkey-patch _issue_completion to add a forged citation.
        original = service._issue_completion
        from app.services.ai_context_builder import (
            AIContext,
            CitationIndex,
        )

        def patched(
            *,
            operation,
            ctx,
            index,
            user_question,
            extra_citations=None,
        ):
            forged = list(extra_citations or []) + [
                {"type": "recommendation", "id": "det-forged", "resource_id": "vol-real"}
            ]
            return original(
                operation=operation,
                ctx=ctx,
                index=index,
                user_question=user_question,
                extra_citations=forged,
            )

        service._issue_completion = patched  # type: ignore[assignment]
        response = service.generate_executive_summary(region="us-east-1", days=30)
        # The forged citation is dropped with a warning; the real one
        # is kept.
        ids = [c.get("id") for c in response.citations if c.get("type") == "recommendation"]
        assert "det-real" in ids
        assert "det-forged" not in ids
        assert any("Discarded unsupported recommendation citation" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_service_with_fakes():
    """Wire a fake service into the FastAPI router."""
    from app.api import ai as ai_module

    client = FakeLiteLLMClient(
        completions=[
            CompletionResult(content="Executive answer.", model="cost-detective-free"),
            CompletionResult(content="Analyze answer.", model="cost-detective-free"),
            CompletionResult(content="Explain answer.", model="cost-detective-free"),
        ]
    )
    service = AIService(
        settings=_settings(),
        client=client,  # type: ignore[arg-type]
        gatherer=FakeEvidenceGatherer(
            cost_report=_cost_report(),
            capabilities=_capabilities(),
            recommendations=[_recommendation()],
        ),
    )
    # Patch the factory used inside the router.
    ai_module._service_factory = lambda: service
    return service, client


@pytest.fixture
def fastapi_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ai as ai_module

    app = FastAPI()
    app.include_router(ai_module.router)
    return TestClient(app)


class TestRouter:
    def test_status_endpoint(self, ai_service_with_fakes, fastapi_client) -> None:
        _service, _client = ai_service_with_fakes
        r = fastapi_client.get("/ai/status")
        assert r.status_code == 200
        body = r.json()
        assert body["ai_enabled"] is True
        assert body["model_alias"] == "cost-detective-free"
        assert body["status"] in ("OK", "DEGRADED", "DISABLED")

    def test_executive_summary_endpoint(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/executive-summary",
            json={"region": "us-east-1", "days": 30},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["operation"] == "executive_summary"
        assert body["answer"]  # non-empty canned answer

    def test_analyze_endpoint(self, ai_service_with_fakes, fastapi_client) -> None:
        r = fastapi_client.post(
            "/ai/analyze",
            json={"region": "us-east-1", "days": 30, "question": "Why did bill rise?"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["operation"] == "analyze"
        assert body["answer"]

    def test_explain_endpoint_success(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/recommendations/det-1/explain",
            json={"region": "us-east-1", "days": 30},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["operation"] == "explain"
        assert body["answer"]

    def test_explain_endpoint_missing_returns_404(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/recommendations/det-missing/explain",
            json={"region": "us-east-1", "days": 30},
        )
        assert r.status_code == 404
        body = r.json()
        assert body["status"] == "error"
        assert body["error_code"] == "RecommendationNotFound"

    def test_unsupported_lookback_returns_422(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/executive-summary",
            json={"region": "us-east-1", "days": 15},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["status"] == "error"
        assert body["error_code"] == "InvalidLookbackDays"

    def test_empty_question_returns_422(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/analyze",
            json={"region": "us-east-1", "days": 30, "question": "   "},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error_code"] == "InvalidQuestion"

    def test_oversized_question_returns_422(
        self, ai_service_with_fakes, fastapi_client
    ) -> None:
        r = fastapi_client.post(
            "/ai/analyze",
            json={"region": "us-east-1", "days": 30, "question": "x" * 500},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error_code"] == "InvalidQuestion"

    @pytest.mark.parametrize("days", [7, 30, 60, 90])
    def test_allowed_lookbacks_accepted(
        self, ai_service_with_fakes, fastapi_client, days
    ) -> None:
        r = fastapi_client.post(
            "/ai/executive-summary",
            json={"region": "us-east-1", "days": days},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# Disabled mode at the router level
# ---------------------------------------------------------------------------


class TestDisabledModeRouter:
    def test_status_reports_disabled(self) -> None:
        from app.api import ai as ai_module
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        service = AIService(settings=_settings(ai_enabled=False))
        ai_module._service_factory = lambda: service
        app = FastAPI()
        app.include_router(ai_module.router)
        client = TestClient(app)

        r = client.get("/ai/status")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "DISABLED"
        assert body["ai_enabled"] is False

        r = client.post(
            "/ai/executive-summary", json={"region": "us-east-1", "days": 30}
        )
        # Disabled is a 200 with status=DISABLED, NOT a 500.
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "DISABLED"
        assert "AI_DISABLED" in body["warnings"]


# ---------------------------------------------------------------------------
# Failure isolation — Phase 0-3 endpoints must not break
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_disabled_mode_does_not_touch_provider(self) -> None:
        """No LiteLLM call when AI is disabled."""
        client = FakeLiteLLMClient()
        service = AIService(
            settings=_settings(ai_enabled=False),
            client=client,  # type: ignore[arg-type]
            gatherer=FakeEvidenceGatherer(
                cost_report=_cost_report(),
                capabilities=_capabilities(),
                recommendations=[_recommendation()],
            ),
        )
        # All three generation methods.
        service.generate_executive_summary(region="us-east-1", days=30)
        service.generate_analysis(region="us-east-1", days=30, question="Why?")
        service.generate_recommendation_explanation(
            region="us-east-1", days=30, recommendation_id="det-1"
        )
        assert client.complete_calls == []
