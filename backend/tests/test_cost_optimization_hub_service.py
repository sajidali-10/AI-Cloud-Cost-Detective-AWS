"""Unit tests for ``app.services.aws.cost_optimization_hub`` — Phase 3.

Mock-only tests.  Each test substitutes the Boto3 ``cost-optimization-hub``
client with a stateful fake that records calls and returns canned pages.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.aws.cost_optimization_hub import (
    CostOptimizationHubError,
    HUB_PAGE_SIZE,
    get_preferences,
    get_recommendation,
    list_enrollment_statuses,
    list_recommendation_summaries,
    list_recommendations,
)


def _make_client(page_factory: Callable[[str, Dict[str, Any]], Dict[str, Any]]) -> MagicMock:
    recorded = []

    def _make_method(op_name: str) -> Callable[..., Dict[str, Any]]:
        def _method(**kwargs: Any) -> Dict[str, Any]:
            recorded.append((op_name, dict(kwargs)))
            return page_factory(op_name, dict(kwargs))

        return _method

    client = MagicMock()
    for op in (
        "list_enrollment_statuses",
        "get_preferences",
        "list_recommendations",
        "list_recommendation_summaries",
        "get_recommendation",
    ):
        setattr(client, op, _make_method(op))
    client._recorded = recorded  # type: ignore[attr-defined]
    return client


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": f"{code} for test"}},
        operation_name="Op",
    )


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


class TestEnrollment:
    def test_active(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [{"accountId": "111122223333", "status": "Active"}]
            }
            if op == "list_enrollment_statuses"
            else {}
        )
        assert list_enrollment_statuses(client) == "ACTIVE"

    def test_inactive(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [{"accountId": "111122223333", "status": "Inactive"}]
            }
            if op == "list_enrollment_statuses"
            else {}
        )
        assert list_enrollment_statuses(client) == "INACTIVE"

    def test_pending(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [{"accountId": "111122223333", "status": "Pending"}]
            }
            if op == "list_enrollment_statuses"
            else {}
        )
        assert list_enrollment_statuses(client) == "PENDING"

    def test_failed(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [{"accountId": "111122223333", "status": "Failed"}]
            }
            if op == "list_enrollment_statuses"
            else {}
        )
        assert list_enrollment_statuses(client) == "FAILED"

    def test_empty_response_maps_to_inactive(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {"items": []}
            if op == "list_enrollment_statuses"
            else {}
        )
        assert list_enrollment_statuses(client) == "INACTIVE"

    def test_access_denied_is_sanitized(self) -> None:
        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op == "list_enrollment_statuses":
                raise _client_error("AccessDeniedException")
            return {}

        client = _make_client(page_factory=_page)
        with pytest.raises(CostOptimizationHubError) as exc:
            list_enrollment_statuses(client)
        assert exc.value.code == "AccessDeniedException"


class TestPreferences:
    def test_returns_dict(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {"memberAccountDiscountVisibility": "ALL"}
            if op == "get_preferences"
            else {}
        )
        prefs = get_preferences(client)
        assert prefs["memberAccountDiscountVisibility"] == "ALL"


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def _coh_recommendation(
    *,
    recommendation_id: str = "rec-1",
    resource_id: str = "i-1",
    resource_type: str = "Ec2Instance",
    action_type: str = "Rightsize",
    region: str = "us-east-1",
    savings: float = 12.5,
    restart_needed: bool = False,
    rollback_possible: bool = True,
    implementation_effort: str = "Low",
) -> Dict[str, Any]:
    return {
        "recommendationId": recommendation_id,
        "resourceId": resource_id,
        "resourceArn": f"arn:aws:ec2:{region}:111122223333:instance/{resource_id}",
        "resourceType": resource_type,
        "actionType": action_type,
        "region": region,
        "accountId": "111122223333",
        "estimatedMonthlySavings": savings,
        "savingsPercentage": 30.0,
        "currencyCode": "USD",
        "currentResourceSummary": {"instanceType": "m5.large"},
        "recommendedResourceSummary": {"instanceType": "m5.medium"},
        "restartNeeded": restart_needed,
        "rollbackPossible": rollback_possible,
        "implementationEffort": implementation_effort,
    }


class TestListRecommendations:
    def test_normalization(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [_coh_recommendation()],
            }
            if op == "list_recommendations"
            else {}
        )
        rows = list_recommendations(client, region="us-east-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.recommendation_id == "rec-1"
        assert row.resource_id == "i-1"
        assert row.resource_type == "Ec2Instance"
        assert row.action_type == "Rightsize"
        assert row.estimated_monthly_savings == Decimal("12.5")
        assert row.restart_needed is False
        assert row.rollback_possible is True
        assert row.implementation_effort == "Low"

    def test_pagination(self) -> None:
        calls = {"n": 0}

        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op != "list_recommendations":
                return {}
            calls["n"] += 1
            if calls["n"] == 1:
                assert "nextToken" not in kw
                return {
                    "items": [_coh_recommendation(recommendation_id="r1")],
                    "nextToken": "page-2",
                }
            assert kw.get("nextToken") == "page-2"
            return {"items": [_coh_recommendation(recommendation_id="r2")]}

        client = _make_client(page_factory=_page)
        rows = list_recommendations(client, region="us-east-1")
        assert calls["n"] == 2
        assert [r.recommendation_id for r in rows] == ["r1", "r2"]

    def test_max_pages_caps_pagination(self) -> None:
        # ``max_pages=1`` should fetch exactly one page.
        calls = {"n": 0}

        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op != "list_recommendations":
                return {}
            calls["n"] += 1
            return {
                "items": [_coh_recommendation(recommendation_id=f"r{calls['n']}")],
                "nextToken": "still-more",
            }

        client = _make_client(page_factory=_page)
        rows = list_recommendations(client, region="us-east-1", max_pages=1)
        assert calls["n"] == 1
        assert len(rows) == 1

    def test_empty_response(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {"items": []}
            if op == "list_recommendations"
            else {}
        )
        assert list_recommendations(client, region="us-east-1") == []

    def test_restart_needed_and_rollback_possible(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [_coh_recommendation(
                    action_type="StopIdle", restart_needed=True, rollback_possible=False,
                )]
            }
            if op == "list_recommendations"
            else {}
        )
        rows = list_recommendations(client, region="us-east-1")
        assert rows[0].restart_needed is True
        assert rows[0].rollback_possible is False

    def test_missing_savings_stays_none(self) -> None:
        row = _coh_recommendation()
        row["estimatedMonthlySavings"] = None
        client = _make_client(
            page_factory=lambda op, kw: {"items": [row]}
            if op == "list_recommendations"
            else {}
        )
        rows = list_recommendations(client, region="us-east-1")
        assert rows[0].estimated_monthly_savings is None

    def test_access_denied(self) -> None:
        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op == "list_recommendations":
                raise _client_error("AccessDeniedException")
            return {}

        client = _make_client(page_factory=_page)
        with pytest.raises(CostOptimizationHubError):
            list_recommendations(client, region="us-east-1")


class TestSummaries:
    def test_normalization(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "items": [
                    {
                        "group": "Ec2Instance",
                        "count": 7,
                        "estimatedMonthlySavings": 21.0,
                        "currencyCode": "USD",
                    }
                ]
            }
            if op == "list_recommendation_summaries"
            else {}
        )
        rows = list_recommendation_summaries(client)
        assert len(rows) == 1
        assert rows[0]["group"] == "Ec2Instance"
        assert rows[0]["count"] == 7
        assert rows[0]["estimated_monthly_savings"] == Decimal("21.0")


class TestGetRecommendation:
    def test_returns_payload(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {"recommendationId": "rec-1", "estimatedMonthlySavings": 10}
            if op == "get_recommendation"
            else {}
        )
        out = get_recommendation(client, recommendation_id="rec-1")
        assert out["recommendationId"] == "rec-1"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_hub_page_size_is_positive() -> None:
    assert HUB_PAGE_SIZE > 0
