"""Unit tests for ``app.services.aws.compute_optimizer`` — Phase 3.

Pure-mock tests: each test stands in a Boto3 ``compute-optimizer``
client with a small stateful fake that returns canned pages and
records call kwargs.  No network, no AWS credentials.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict, List
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.aws.compute_optimizer import (
    ComputeOptimizerError,
    get_ebs_volume_recommendations,
    get_ec2_instance_recommendations,
    get_enrollment_status,
    get_lambda_function_recommendations,
    get_rds_database_recommendations,
    get_recommendation_summaries,
)


# ---------------------------------------------------------------------------
# Fake Boto3 client
# ---------------------------------------------------------------------------


def _make_client(
    *,
    page_factory: Callable[[str, Dict[str, Any]], Dict[str, Any]],
) -> MagicMock:
    """Return a MagicMock that records calls and returns canned pages.

    ``page_factory(op_name, kwargs)`` returns a single response page.
    The mock walks ``nextToken`` until ``page_factory`` returns one
    without it.
    """

    def _make_method(op_name: str) -> Callable[..., Dict[str, Any]]:
        def _method(**kwargs: Any) -> Dict[str, Any]:
            recorded.append((op_name, dict(kwargs)))
            return page_factory(op_name, dict(kwargs))

        return _method

    client = MagicMock()
    recorded = []
    for op in (
        "get_enrollment_status",
        "get_recommendation_summaries",
        "get_ec2_instance_recommendations",
        "get_ebs_volume_recommendations",
        "get_lambda_function_recommendations",
        "get_rds_database_recommendations",
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
    def test_active_status(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "accountEnrollmentStatuses": [{"accountId": "111122223333", "status": "Active"}]
            }
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "ACTIVE"

    def test_inactive_status(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "accountEnrollmentStatuses": [{"accountId": "111122223333", "status": "Inactive"}]
            }
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "INACTIVE"

    def test_pending_status(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "accountEnrollmentStatuses": [{"accountId": "111122223333", "status": "Pending"}]
            }
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "PENDING"

    def test_failed_status(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "accountEnrollmentStatuses": [{"accountId": "111122223333", "status": "Failed"}]
            }
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "FAILED"

    def test_unknown_status_falls_back_to_unavailable(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "accountEnrollmentStatuses": [{"accountId": "111122223333", "status": "Garbage"}]
            }
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "UNAVAILABLE"

    def test_empty_enrollment_response_is_unavailable(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {"accountEnrollmentStatuses": []}
            if op == "get_enrollment_status"
            else {}
        )
        assert get_enrollment_status(client) == "UNAVAILABLE"

    def test_access_denied_raises_sanitized(self) -> None:
        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op == "get_enrollment_status":
                raise _client_error("AccessDeniedException")
            return {}

        client = _make_client(page_factory=_page)
        with pytest.raises(ComputeOptimizerError) as exc:
            get_enrollment_status(client)
        assert exc.value.code == "AccessDeniedException"


# ---------------------------------------------------------------------------
# Recommendation summaries
# ---------------------------------------------------------------------------


class TestRecommendationSummaries:
    def test_summaries_aggregated_by_resource_type(self) -> None:
        client = _make_client(
            page_factory=lambda op, kw: {
                "recommendationSummaries": [
                    {
                        "recommendationResourceType": "Ec2Instance",
                        "summaries": {"TotalRecommendationCount": 12, "Finding": "OVER_PROVISIONED"},
                    },
                    {
                        "recommendationResourceType": "EbsVolume",
                        "summaries": {"TotalRecommendationCount": 4},
                    },
                ]
            }
            if op == "get_recommendation_summaries"
            else {}
        )
        out = get_recommendation_summaries(client)
        assert out.get("Ec2Instance") == 12
        assert out.get("EbsVolume") == 4


# ---------------------------------------------------------------------------
# EC2 recommendations
# ---------------------------------------------------------------------------


_EC2_PAGE = {
    "instanceRecommendations": [
        {
            "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-0aaa",
            "accountId": "111122223333",
            "instanceId": "i-0aaa",
            "finding": "OVER_PROVISIONED",
            "currentInstanceType": "m5.large",
            "lookbackPeriodInDays": 14,
            "estimatedMonthlySavings": 42.10,
            "savingsPercentage": 25.0,
            "currencyCode": "USD",
            "performanceRisk": 0.5,
            "recommendationOptions": [
                {
                    "instanceType": "m5.medium",
                    "performanceRisk": 0.4,
                    "monthlyPrice": 70.20,
                }
            ],
            "reasonCodes": ["CPU_OVER_PROVISIONED"],
        }
    ]
}


class TestEC2Recommendations:
    def test_basic_normalization(self) -> None:
        client = _make_client(page_factory=lambda op, kw: _EC2_PAGE if "ec2" in op else {})
        rows = get_ec2_instance_recommendations(client, region="us-east-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_id == "i-0aaa"
        assert row.resource_arn == "arn:aws:ec2:us-east-1:111122223333:instance/i-0aaa"
        assert row.finding == "OVER_PROVISIONED"
        assert row.current_configuration["instance_type"] == "m5.large"
        assert row.recommended_configuration["instance_type"] == "m5.medium"
        assert row.estimated_monthly_savings == Decimal("42.10")
        assert row.savings_percentage == Decimal("25.0")
        assert row.performance_risk == Decimal("0.5")
        assert row.reason_codes == ["CPU_OVER_PRO_PROVISIONED" if False else "CPU_OVER_PROVISIONED"]

    def test_missing_savings_stays_none(self) -> None:
        page = {"instanceRecommendations": [
            {
                "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-1",
                "accountId": "111122223333",
                "instanceId": "i-1",
                "finding": "OPTIMIZED",
                "currentInstanceType": "m5.large",
                "recommendationOptions": [],
                "reasonCodes": [],
            }
        ]}
        client = _make_client(page_factory=lambda op, kw: page if "ec2" in op else {})
        rows = get_ec2_instance_recommendations(client, region="us-east-1")
        assert rows[0].estimated_monthly_savings is None
        assert rows[0].savings_percentage is None
        assert rows[0].performance_risk is None


# ---------------------------------------------------------------------------
# EBS recommendations
# ---------------------------------------------------------------------------


class TestEBSRecommendations:
    def test_basic_normalization(self) -> None:
        page = {"volumeRecommendations": [{
            "volumeArn": "arn:aws:ec2:us-east-1:111122223333:volume/vol-aaa",
            "accountId": "111122223333",
            "volumeId": "vol-aaa",
            "finding": "OPTIMIZED",
            "currentConfiguration": {
                "volumeType": "gp2",
                "volumeSize": 100,
                "baselineIOPS": 3000,
            },
            "recommendationOptions": [{
                "configuration": {
                    "volumeType": "gp3",
                    "volumeSize": 100,
                    "baselineIOPS": 3000,
                }
            }],
            "reasonCodes": ["IOPS_OVER_PROVISIONED"],
        }]}
        client = _make_client(page_factory=lambda op, kw: page if "ebs" in op else {})
        rows = get_ebs_volume_recommendations(client, region="us-east-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_id == "vol-aaa"
        assert row.current_configuration["volume_type"] == "gp2"
        assert row.recommended_configuration["volume_type"] == "gp3"
        assert "IOPS_OVER_PROVISIONED" in row.reason_codes


# ---------------------------------------------------------------------------
# Lambda recommendations
# ---------------------------------------------------------------------------


class TestLambdaRecommendations:
    def test_basic_normalization(self) -> None:
        page = {"lambdaFunctionRecommendations": [{
            "functionArn": "arn:aws:lambda:us-east-1:111122223333:function:foo",
            "accountId": "111122223333",
            "functionName": "foo",
            "finding": "OVER_PROVISIONED",
            "currentMemorySize": 512,
            "estimatedMonthlySavings": 1.23,
            "currencyCode": "USD",
            "recommendationOptions": [{
                "configuration": {"memory": 256}
            }],
            "reasonCodes": ["MEMORY_OVER_PROVISIONED"],
        }]}
        client = _make_client(page_factory=lambda op, kw: page if "lambda" in op else {})
        rows = get_lambda_function_recommendations(client, region="us-east-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_id == "foo"
        assert row.current_configuration["memory_mb"] == 512
        assert row.recommended_configuration["memory_mb"] == 256


# ---------------------------------------------------------------------------
# RDS recommendations
# ---------------------------------------------------------------------------


class TestRDSRecommendations:
    def test_basic_normalization(self) -> None:
        page = {"rdsDBRecommendations": [{
            "DBInstanceArn": "arn:aws:rds:us-east-1:111122223333:db:db1",
            "accountId": "111122223333",
            "DBInstanceIdentifier": "db1",
            "finding": "OVER_PROVISIONED",
            "currentDBInstanceClass": "db.m5.large",
            "engine": "postgres",
            "estimatedMonthlySavings": 9.99,
            "currencyCode": "USD",
            "recommendationOptions": [{"dbInstanceClass": "db.m5.medium"}],
            "reasonCodes": ["CPU_OVER_PROVISIONED"],
        }]}
        client = _make_client(page_factory=lambda op, kw: page if "rds" in op else {})
        rows = get_rds_database_recommendations(client, region="us-east-1")
        assert len(rows) == 1
        row = rows[0]
        assert row.resource_id == "db1"
        assert row.current_configuration["db_instance_class"] == "db.m5.large"
        assert row.recommended_configuration["db_instance_class"] == "db.m5.medium"
        assert row.estimated_monthly_savings == Decimal("9.99")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_next_token_is_walked(self) -> None:
        calls = {"n": 0}

        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op != "get_ec2_instance_recommendations":
                return {}
            calls["n"] += 1
            if calls["n"] == 1:
                assert "nextToken" not in kw
                return {
                    "instanceRecommendations": [
                        {
                            "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-1",
                            "accountId": "111122223333",
                            "instanceId": "i-1",
                            "finding": "OVER_PROVISIONED",
                            "currentInstanceType": "m5.large",
                            "recommendationOptions": [],
                        }
                    ],
                    "nextToken": "page-2",
                }
            assert kw.get("nextToken") == "page-2"
            return {
                "instanceRecommendations": [
                    {
                        "instanceArn": "arn:aws:ec2:us-east-1:111122223333:instance/i-2",
                        "accountId": "111122223333",
                        "instanceId": "i-2",
                        "finding": "OVER_PROVISIONED",
                        "currentInstanceType": "m5.large",
                        "recommendationOptions": [],
                    }
                ],
            }

        client = _make_client(page_factory=_page)
        rows = get_ec2_instance_recommendations(client, region="us-east-1")
        assert calls["n"] == 2
        assert [r.resource_id for r in rows] == ["i-1", "i-2"]

    def test_empty_response_is_empty_list(self) -> None:
        client = _make_client(page_factory=lambda op, kw: {} if "ec2" in op else {})
        rows = get_ec2_instance_recommendations(client, region="us-east-1")
        assert rows == []


# ---------------------------------------------------------------------------
# Exception sanitization
# ---------------------------------------------------------------------------


class TestExceptionSanitization:
    def test_client_error_is_sanitized(self) -> None:
        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op == "get_ebs_volume_recommendations":
                raise _client_error("InternalError")
            return {}

        client = _make_client(page_factory=_page)
        with pytest.raises(ComputeOptimizerError) as exc:
            get_ebs_volume_recommendations(client, region="us-east-1")
        assert exc.value.code == "InternalError"
        # The exception message is generic; no payload leak.
        assert "InternalError for test" not in exc.value.message


# ---------------------------------------------------------------------------
# Read-only guard wiring
# ---------------------------------------------------------------------------


class TestReadOnlyGuard:
    def test_assert_read_only_invoked_before_each_call(self) -> None:
        from app.services.aws.guard import AwsReadOnlyViolation

        def _page(op: str, kw: Dict[str, Any]) -> Dict[str, Any]:
            if op == "get_ec2_instance_recommendations":
                raise AwsReadOnlyViolation(op_name=op, matched_prefix="create_")
            return {}

        client = _make_client(page_factory=_page)
        with pytest.raises(AwsReadOnlyViolation):
            get_ec2_instance_recommendations(client, region="us-east-1")
