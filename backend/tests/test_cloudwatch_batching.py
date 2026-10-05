"""Unit tests for ``app.services.aws.cloudwatch_metrics`` — Phase 2.

These tests use ``MagicMock`` to stand in for the Boto3 CloudWatch
client, exercising:

* per-resource metric specs for EC2 / RDS / Lambda / ALB / NLB;
* ``build_metric_queries`` ordering and dimension correctness;
* ``batch_query`` chunking under the 500-query AWS limit, including
  multi-batch + ``NextToken`` pagination;
* ``classify_data_quality`` coverage-based classification;
* sanitized ``CloudWatchError`` propagation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List

import pytest
from botocore.exceptions import ClientError

from app.services.aws.cloudwatch_metrics import (
    ALLOWED_LOOKBACK_DAYS,
    AWS_GET_METRIC_DATA_MAX_QUERIES,
    DataQuality,
    MetricSpec,
    ResourceDescriptor,
    aggregate_results,
    batch_query,
    build_metric_queries,
    classify_data_quality,
    period_seconds_for,
    specs_for_resource,
    to_aws_query_payload,
)


# ---------------------------------------------------------------------------
# classify_data_quality
# ---------------------------------------------------------------------------


class TestClassifyDataQuality:
    def test_no_data_when_zero_datapoints(self) -> None:
        assert classify_data_quality(datapoint_count=0, lookback_days=7, period_seconds=3600) == DataQuality.NO_DATA

    def test_high_at_or_above_80_percent_coverage(self) -> None:
        # 7 days at 3600s -> expected 168. 80% = 134.4 -> 135+ is HIGH.
        assert classify_data_quality(datapoint_count=168, lookback_days=7, period_seconds=3600) == DataQuality.HIGH
        assert classify_data_quality(datapoint_count=135, lookback_days=7, period_seconds=3600) == DataQuality.HIGH

    def test_medium_at_40_to_80_percent(self) -> None:
        # 67 of 168 = 39.9% -> LOW; 100 of 168 = 59.5% -> MEDIUM.
        assert classify_data_quality(datapoint_count=100, lookback_days=7, period_seconds=3600) == DataQuality.MEDIUM

    def test_low_below_40_percent(self) -> None:
        assert classify_data_quality(datapoint_count=10, lookback_days=7, period_seconds=3600) == DataQuality.LOW


class TestPeriodSecondsFor:
    @pytest.mark.parametrize("days", [7, 30, 60, 90])
    def test_allowed_lookback_returns_period(self, days: int) -> None:
        p = period_seconds_for(days)
        assert p > 0

    def test_disallowed_lookback_raises(self) -> None:
        from app.services.aws.cloudwatch_metrics import CloudWatchError
        with pytest.raises(CloudWatchError):
            period_seconds_for(14)


# ---------------------------------------------------------------------------
# Per-resource metric specs
# ---------------------------------------------------------------------------


class TestSpecsForResource:
    def test_ec2_has_four_metrics(self) -> None:
        specs = specs_for_resource(ResourceDescriptor(resource_id="i-abc", resource_type="ec2", region="us-east-1"))
        names = {(s.metric_name, s.statistic) for s in specs}
        assert ("CPUUtilization", "Average") in names
        assert ("CPUUtilization", "Maximum") in names
        assert ("NetworkIn", "Sum") in names
        assert ("NetworkOut", "Sum") in names
        for s in specs:
            assert s.dimension_name == "InstanceId"
            assert s.dimension_value == "i-abc"

    def test_rds_has_six_metrics(self) -> None:
        specs = specs_for_resource(ResourceDescriptor(resource_id="db1", resource_type="rds", region="us-east-1"))
        names = {(s.metric_name, s.statistic) for s in specs}
        assert ("CPUUtilization", "Average") in names
        assert ("DatabaseConnections", "Maximum") in names
        assert ("FreeableMemory", "Minimum") in names
        for s in specs:
            assert s.dimension_name == "DBInstanceIdentifier"

    def test_lambda_has_five_metrics(self) -> None:
        specs = specs_for_resource(ResourceDescriptor(resource_id="fn1", resource_type="lambda", region="us-east-1"))
        names = {(s.metric_name, s.statistic) for s in specs}
        assert ("Invocations", "Sum") in names
        assert ("Duration", "Maximum") in names
        assert ("Errors", "Sum") in names
        assert ("Throttles", "Sum") in names
        for s in specs:
            assert s.dimension_name == "FunctionName"

    def test_alb_uses_load_balancer_arn(self) -> None:
        arn = "arn:aws:elasticloadbalancing:us-east-1:111:loadbalancer/app/foo/abc"
        specs = specs_for_resource(ResourceDescriptor(resource_id=arn, resource_type="alb", region="us-east-1"))
        names = {s.metric_name for s in specs}
        assert "RequestCount" in names
        assert "ProcessedBytes" in names
        for s in specs:
            assert s.dimension_name == "LoadBalancerArn"
            assert s.dimension_value == arn

    def test_nlb_uses_load_balancer_arn(self) -> None:
        arn = "arn:aws:elasticloadbalancing:us-east-1:111:loadbalancer/net/foo/abc"
        specs = specs_for_resource(ResourceDescriptor(resource_id=arn, resource_type="nlb", region="us-east-1"))
        names = {s.metric_name for s in specs}
        assert names == {"ProcessedBytes"}
        for s in specs:
            assert s.dimension_name == "LoadBalancerArn"


# ---------------------------------------------------------------------------
# build_metric_queries + to_aws_query_payload
# ---------------------------------------------------------------------------


class TestBuildMetricQueries:
    def test_returns_one_query_per_spec(self) -> None:
        resources = [
            ResourceDescriptor(resource_id="i-a", resource_type="ec2", region="us-east-1"),
            ResourceDescriptor(resource_id="db1", resource_type="rds", region="us-east-1"),
        ]
        queries = build_metric_queries(resources)
        # 4 EC2 + 6 RDS = 10 queries.
        assert len(queries) == 10
        # Every query id is unique.
        assert len({q.id for q in queries}) == 10
        # First query is for the first resource's first spec.
        assert queries[0].metric_spec.namespace == "AWS/EC2"

    def test_aws_payload_shape(self) -> None:
        resources = [
            ResourceDescriptor(resource_id="i-a", resource_type="ec2", region="us-east-1"),
        ]
        queries = build_metric_queries(resources)
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        payload = to_aws_query_payload(queries, start=start, end=end, period_seconds=3600)
        assert payload[0]["Id"]
        assert payload[0]["MetricStat"]["Metric"]["Namespace"] == "AWS/EC2"
        assert payload[0]["MetricStat"]["Period"] == 3600
        assert payload[0]["MetricStat"]["Stat"] in {"Average", "Sum", "Maximum", "Minimum"}
        assert payload[0]["MetricStat"]["Metric"]["Dimensions"] == [
            {"Name": "InstanceId", "Value": "i-a"}
        ]


# ---------------------------------------------------------------------------
# batch_query — chunking + pagination + sanitization
# ---------------------------------------------------------------------------


class _FakeCW:
    """Stateful fake CloudWatch client for batch_query tests.

    ``pages_per_call`` controls how many ``GetMetricData`` responses
    to return for a single call (exercising ``NextToken``).
    """

    def __init__(self, *, pages_per_call: int = 1, raises: Exception | None = None) -> None:
        self.pages_per_call = pages_per_call
        self.raised = raises
        self.calls: List[dict[str, Any]] = []
        self._call_index = 0

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raised is not None:
            raise self.raised
        # Decide whether to issue a NextPageToken.
        token: Any = None
        if self.pages_per_call > 1 and self._call_index < self.pages_per_call - 1:
            token = f"TOKEN-{self._call_index}"
        self._call_index += 1
        return {
            "MetricDataResults": [
                {
                    "Id": q["Id"],
                    "Label": f"{q['MetricStat']['Metric']['MetricName']} | Bytes",
                    "Timestamps": [],
                    "Values": [],
                }
                for q in kwargs.get("MetricDataQueries", [])
            ],
            "NextPageToken": token,
        }


class TestBatchQueryChunking:
    def test_single_chunk_under_500(self) -> None:
        # 10 EC2 resources x 4 metrics = 40 queries — well under 500,
        # so all of them should fit in a single GetMetricData call.
        resources = [
            ResourceDescriptor(resource_id=f"i-{i}", resource_type="ec2", region="us-east-1")
            for i in range(10)
        ]
        queries = build_metric_queries(resources)
        assert len(queries) == 40
        fake = _FakeCW(pages_per_call=1)
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        results = batch_query(fake, queries, start=start, end=end, period_seconds=3600)
        assert len(results) == 40
        assert len(fake.calls) == 1
        assert len(fake.calls[0]["MetricDataQueries"]) == 40

    def test_chunks_over_500_queries(self) -> None:
        # Build 600 queries (150 resources x 4 EC2 metrics).
        resources = [
            ResourceDescriptor(resource_id=f"i-{i}", resource_type="ec2", region="us-east-1")
            for i in range(150)
        ]
        queries = build_metric_queries(resources)
        assert len(queries) == 600
        fake = _FakeCW(pages_per_call=1)
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        results = batch_query(fake, queries, start=start, end=end, period_seconds=3600)
        assert len(results) == 600
        assert len(fake.calls) == 2  # 500 + 100
        assert len(fake.calls[0]["MetricDataQueries"]) == 500
        assert len(fake.calls[1]["MetricDataQueries"]) == 100

    def test_walks_next_token_within_a_chunk(self) -> None:
        # 3 EC2 resources -> 12 queries. pages_per_call=2 means the
        # pagination loop makes exactly 2 calls per chunk: the first
        # returns a NextPageToken, the second returns None.
        resources = [
            ResourceDescriptor(resource_id=f"i-{i}", resource_type="ec2", region="us-east-1")
            for i in range(3)
        ]
        queries = build_metric_queries(resources)
        assert len(queries) == 12
        fake = _FakeCW(pages_per_call=2)  # 1 NextPageToken page + 1 final
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        results = batch_query(fake, queries, start=start, end=end, period_seconds=3600)
        # 2 pages x 12 queries each -> 24 aggregated result rows.
        assert len(results) == 24
        # First call has no prior token, so no NextPageToken kwarg.
        assert "NextPageToken" not in fake.calls[0]
        # Second call receives the NextPageToken from the first response.
        assert fake.calls[1]["NextPageToken"] == "TOKEN-0"

    def test_empty_queries_returns_empty(self) -> None:
        fake = _FakeCW()
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        assert batch_query(fake, [], start=start, end=end, period_seconds=3600) == []
        assert fake.calls == []

    def test_access_denied_sanitized(self) -> None:
        resources = [ResourceDescriptor(resource_id="i-a", resource_type="ec2", region="us-east-1")]
        queries = build_metric_queries(resources)
        fake = _FakeCW(
            raises=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "User: AKIAEXAMPLE is not authorized"}},
                "get_metric_data",
            )
        )
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 10, 1, tzinfo=timezone.utc)
        from app.services.aws.cloudwatch_metrics import CloudWatchError
        with pytest.raises(CloudWatchError) as excinfo:
            batch_query(fake, queries, start=start, end=end, period_seconds=3600)
        assert excinfo.value.code == "AccessDenied"
        assert "AKIAEXAMPLE" not in excinfo.value.message


# ---------------------------------------------------------------------------
# aggregate_results — query id -> series, data quality per series
# ---------------------------------------------------------------------------


class TestAggregateResults:
    def test_data_quality_classified_per_series(self) -> None:
        # 4 EC2 specs; produce data only for 2 of them to exercise
        # the "no data" branch on the others.
        resources = [ResourceDescriptor(resource_id="i-a", resource_type="ec2", region="us-east-1")]
        queries = build_metric_queries(resources)
        results_by_id = {q.id: {"Id": q.id, "Label": f"{q.metric_spec.metric_name} | Percent", "Timestamps": [], "Values": []} for q in queries}
        # Populate CPU Average + Maximum with 168 datapoints each.
        for q in queries:
            if q.metric_spec.metric_name == "CPUUtilization":
                now = datetime.now(timezone.utc)
                results_by_id[q.id] = {
                    "Id": q.id,
                    "Label": f"{q.metric_spec.metric_name} | Percent",
                    "Timestamps": [now for _ in range(168)],
                    "Values": [10.0 for _ in range(168)],
                }
        results = list(results_by_id.values())
        series = aggregate_results(
            results,
            queries=queries,
            resources_by_id={d.resource_id: d for d in resources},
            lookback_days=7,
            period_seconds=3600,
        )
        assert len(series) == len(queries)
        cpu_avg = next(s for s in series if s.metric_name == "CPUUtilization" and s.statistic == "Average")
        assert cpu_avg.data_quality == DataQuality.HIGH
        net_in = next(s for s in series if s.metric_name == "NetworkIn")
        assert net_in.data_quality == DataQuality.NO_DATA
