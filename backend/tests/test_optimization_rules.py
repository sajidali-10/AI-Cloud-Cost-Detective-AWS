"""Unit tests for the deterministic rule engine — Phase 3.

Each rule is exercised in isolation against synthetic Phase 1 / Phase 2
inputs.  The rules are pure: no AWS calls, no DB, no FastAPI.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

from app.schemas.optimization import (
    Confidence,
    RecommendationAction,
    ResourceType,
)
from app.services.aws.cloudwatch_metrics import (
    DataQuality,
    MetricDatapoint,
    MetricSeries,
)
from app.services.optimization_rules import (
    deterministic_recommendation_id,
    rule_idle_load_balancer,
    rule_idle_nat_gateway,
    rule_low_utilization_ec2,
    rule_rds_underutilization,
    rule_unattached_ebs,
    rule_unused_eip,
)


def _series(
    *,
    resource_id: str,
    metric_name: str,
    statistic: str,
    values: List[float],
    quality: DataQuality = DataQuality.HIGH,
    namespace: str = "AWS/EC2",
) -> MetricSeries:
    ts = datetime.now(timezone.utc)
    return MetricSeries(
        query_id=f"{resource_id}__{metric_name}__{statistic}",
        resource_id=resource_id,
        resource_type="ec2",
        namespace=namespace,
        metric_name=metric_name,
        statistic=statistic,
        unit="Percent",
        datapoints=[
            MetricDatapoint(timestamp=ts, value=Decimal(str(v))) for v in values
        ],
        data_quality=quality,
    )


# ---------------------------------------------------------------------------
# Rule 1 — Unattached EBS
# ---------------------------------------------------------------------------


class TestUnattachedEbs:
    def test_available_volume_flagged(self) -> None:
        vols = [
            {"volume_id": "vol-1", "size_gb": 100, "state": "available", "availability_zone": "us-east-1a", "attachments": 0, "tags": {}},
        ]
        out = rule_unattached_ebs(account_id=None, region="us-east-1", ebs_volumes=vols)
        assert len(out) == 1
        c = out[0]
        assert c.resource_type == ResourceType.EBS_VOLUME
        assert c.action == RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS
        assert c.confidence == Confidence.HIGH

    def test_in_use_volume_not_flagged(self) -> None:
        vols = [
            {"volume_id": "vol-1", "state": "in-use", "attachments": 1, "tags": {}},
            {"volume_id": "vol-2", "state": "in-use", "attachments": 2, "tags": {}},
        ]
        out = rule_unattached_ebs(account_id=None, region="us-east-1", ebs_volumes=vols)
        assert out == []

    def test_creating_volume_not_flagged(self) -> None:
        vols = [{"volume_id": "vol-1", "state": "creating", "attachments": 0, "tags": {}}]
        assert rule_unattached_ebs(account_id=None, region="us-east-1", ebs_volumes=vols) == []


# ---------------------------------------------------------------------------
# Rule 2 — Unused EIP
# ---------------------------------------------------------------------------


class TestUnusedEip:
    def test_unassociated_flagged(self) -> None:
        eips = [{"public_ip": "1.2.3.4", "allocation_id": "eipalloc-1", "tags": {}}]
        out = rule_unused_eip(account_id=None, region="us-east-1", elastic_ips=eips)
        assert len(out) == 1
        assert out[0].action == RecommendationAction.REVIEW_RELEASE_UNUSED_EIP

    def test_associated_with_instance_not_flagged(self) -> None:
        eips = [
            {"public_ip": "1.2.3.4", "association_id": "eipassoc-1", "instance_id": "i-1", "tags": {}}
        ]
        assert rule_unused_eip(account_id=None, region="us-east-1", elastic_ips=eips) == []

    def test_associated_with_eni_not_flagged(self) -> None:
        eips = [
            {"public_ip": "1.2.3.4", "association_id": "eipassoc-1", "network_interface_id": "eni-1", "tags": {}}
        ]
        assert rule_unused_eip(account_id=None, region="us-east-1", elastic_ips=eips) == []


# ---------------------------------------------------------------------------
# Rule 3 — Low-utilization EC2
# ---------------------------------------------------------------------------


class TestLowUtilizationEc2:
    def test_low_cpu_flagged(self) -> None:
        inst = [{"instance_id": "i-1", "instance_type": "m5.large", "state": "running"}]
        util = {
            "i-1": [
                _series(resource_id="i-1", metric_name="CPUUtilization", statistic="Average", values=[3, 4, 5]),
                _series(resource_id="i-1", metric_name="CPUUtilization", statistic="Maximum", values=[10, 15, 20]),
            ]
        }
        out = rule_low_utilization_ec2(
            account_id=None,
            region="us-east-1",
            ec2_instances=inst,
            utilization_by_resource=util,
            lookback_days=30,
        )
        assert len(out) == 1
        assert out[0].action == RecommendationAction.REVIEW_LOW_UTILIZATION_EC2
        assert out[0].confidence == Confidence.MEDIUM

    def test_high_cpu_not_flagged(self) -> None:
        inst = [{"instance_id": "i-1", "instance_type": "m5.large", "state": "running"}]
        util = {
            "i-1": [
                _series(resource_id="i-1", metric_name="CPUUtilization", statistic="Average", values=[40, 50, 60]),
                _series(resource_id="i-1", metric_name="CPUUtilization", statistic="Maximum", values=[80, 90, 95]),
            ]
        }
        out = rule_low_utilization_ec2(
            account_id=None,
            region="us-east-1",
            ec2_instances=inst,
            utilization_by_resource=util,
            lookback_days=30,
        )
        assert out == []

    def test_no_data_not_flagged(self) -> None:
        inst = [{"instance_id": "i-1", "instance_type": "m5.large", "state": "running"}]
        out = rule_low_utilization_ec2(
            account_id=None,
            region="us-east-1",
            ec2_instances=inst,
            utilization_by_resource={},
            lookback_days=30,
        )
        assert out == []

    def test_low_quality_not_flagged(self) -> None:
        inst = [{"instance_id": "i-1", "instance_type": "m5.large", "state": "running"}]
        util = {
            "i-1": [
                _series(
                    resource_id="i-1",
                    metric_name="CPUUtilization",
                    statistic="Average",
                    values=[3],
                    quality=DataQuality.LOW,
                ),
                _series(
                    resource_id="i-1",
                    metric_name="CPUUtilization",
                    statistic="Maximum",
                    values=[15],
                    quality=DataQuality.LOW,
                ),
            ]
        }
        out = rule_low_utilization_ec2(
            account_id=None,
            region="us-east-1",
            ec2_instances=inst,
            utilization_by_resource=util,
            lookback_days=30,
        )
        assert out == []

    def test_short_lookback_not_flagged(self) -> None:
        # Spec: lookback >= 7 days required.
        inst = [{"instance_id": "i-1", "instance_type": "m5.large", "state": "running"}]
        util = {
            "i-1": [
                _series(resource_id="i-1", metric_name="CPUUtilization", statistic="Average", values=[1]),
            ]
        }
        out = rule_low_utilization_ec2(
            account_id=None,
            region="us-east-1",
            ec2_instances=inst,
            utilization_by_resource=util,
            lookback_days=3,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Rule 4 — Idle NAT Gateway
# ---------------------------------------------------------------------------


class TestIdleNatGateway:
    def test_all_zero_high_quality_flagged(self) -> None:
        nat = [{"nat_gateway_id": "nat-1", "state": "available"}]
        util = {
            "nat-1": [
                _series(
                    resource_id="nat-1",
                    metric_name="BytesInFromDestination",
                    statistic="Sum",
                    values=[0, 0, 0],
                    namespace="AWS/NATGateway",
                ),
            ]
        }
        out = rule_idle_nat_gateway(
            account_id=None,
            region="us-east-1",
            nat_gateways=nat,
            utilization_by_resource=util,
        )
        assert len(out) == 1
        assert out[0].action == RecommendationAction.REVIEW_IDLE_NAT_GATEWAY

    def test_active_nat_not_flagged(self) -> None:
        nat = [{"nat_gateway_id": "nat-1", "state": "available"}]
        util = {
            "nat-1": [
                _series(
                    resource_id="nat-1",
                    metric_name="BytesInFromDestination",
                    statistic="Sum",
                    values=[0, 100, 0],
                    namespace="AWS/NATGateway",
                ),
            ]
        }
        out = rule_idle_nat_gateway(
            account_id=None,
            region="us-east-1",
            nat_gateways=nat,
            utilization_by_resource=util,
        )
        assert out == []

    def test_low_quality_not_flagged(self) -> None:
        nat = [{"nat_gateway_id": "nat-1", "state": "available"}]
        util = {
            "nat-1": [
                _series(
                    resource_id="nat-1",
                    metric_name="BytesInFromDestination",
                    statistic="Sum",
                    values=[0, 0, 0],
                    quality=DataQuality.MEDIUM,
                    namespace="AWS/NATGateway",
                ),
            ]
        }
        out = rule_idle_nat_gateway(
            account_id=None,
            region="us-east-1",
            nat_gateways=nat,
            utilization_by_resource=util,
        )
        assert out == []

    def test_pending_nat_not_flagged(self) -> None:
        nat = [{"nat_gateway_id": "nat-1", "state": "pending"}]
        util = {
            "nat-1": [
                _series(
                    resource_id="nat-1",
                    metric_name="BytesInFromDestination",
                    statistic="Sum",
                    values=[0, 0, 0],
                    namespace="AWS/NATGateway",
                ),
            ]
        }
        out = rule_idle_nat_gateway(
            account_id=None,
            region="us-east-1",
            nat_gateways=nat,
            utilization_by_resource=util,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Rule 5 — Idle Load Balancer
# ---------------------------------------------------------------------------


class TestIdleLoadBalancer:
    def test_alb_zero_traffic_flagged(self) -> None:
        lbs = [{"arn": "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/foo/123", "name": "foo", "type": "application"}]
        util = {
            lbs[0]["arn"]: [
                _series(
                    resource_id=lbs[0]["arn"],
                    metric_name="RequestCount",
                    statistic="Sum",
                    values=[0, 0, 0],
                    namespace="AWS/ApplicationELB",
                ),
            ]
        }
        out = rule_idle_load_balancer(
            account_id=None,
            region="us-east-1",
            load_balancers=lbs,
            utilization_by_resource=util,
        )
        assert len(out) == 1
        assert out[0].action == RecommendationAction.REVIEW_IDLE_LOAD_BALANCER

    def test_alb_with_traffic_not_flagged(self) -> None:
        lbs = [{"arn": "arn:.../foo", "name": "foo", "type": "application"}]
        util = {
            lbs[0]["arn"]: [
                _series(
                    resource_id=lbs[0]["arn"],
                    metric_name="RequestCount",
                    statistic="Sum",
                    values=[0, 1, 0],
                    namespace="AWS/ApplicationELB",
                ),
            ]
        }
        out = rule_idle_load_balancer(
            account_id=None,
            region="us-east-1",
            load_balancers=lbs,
            utilization_by_resource=util,
        )
        assert out == []

    def test_no_data_lb_not_flagged(self) -> None:
        lbs = [{"arn": "arn:.../foo", "name": "foo", "type": "application"}]
        out = rule_idle_load_balancer(
            account_id=None,
            region="us-east-1",
            load_balancers=lbs,
            utilization_by_resource={},
        )
        assert out == []

    def test_gateway_lb_not_flagged(self) -> None:
        lbs = [{"arn": "arn:.../gwy", "name": "gwy", "type": "gateway"}]
        out = rule_idle_load_balancer(
            account_id=None,
            region="us-east-1",
            load_balancers=lbs,
            utilization_by_resource={},
        )
        assert out == []


# ---------------------------------------------------------------------------
# Rule 6 — RDS Underutilization
# ---------------------------------------------------------------------------


class TestRdsUnderutilization:
    def test_low_cpu_and_connections_flagged(self) -> None:
        dbs = [{"db_instance_identifier": "db-1", "db_instance_class": "db.m5.large", "engine": "postgres"}]
        util = {
            "db-1": [
                _series(
                    resource_id="db-1",
                    metric_name="CPUUtilization",
                    statistic="Average",
                    values=[2, 3, 4],
                    namespace="AWS/RDS",
                ),
                _series(
                    resource_id="db-1",
                    metric_name="CPUUtilization",
                    statistic="Maximum",
                    values=[10, 12, 14],
                    namespace="AWS/RDS",
                ),
                _series(
                    resource_id="db-1",
                    metric_name="DatabaseConnections",
                    statistic="Average",
                    values=[1, 1, 2],
                    namespace="AWS/RDS",
                ),
            ]
        }
        out = rule_rds_underutilization(
            account_id=None,
            region="us-east-1",
            rds_instances=dbs,
            utilization_by_resource=util,
            lookback_days=30,
        )
        assert len(out) == 1
        assert out[0].action == RecommendationAction.REVIEW_LOW_UTILIZATION_RDS

    def test_high_connections_not_flagged(self) -> None:
        dbs = [{"db_instance_identifier": "db-1", "db_instance_class": "db.m5.large", "engine": "postgres"}]
        util = {
            "db-1": [
                _series(
                    resource_id="db-1",
                    metric_name="CPUUtilization",
                    statistic="Average",
                    values=[2, 3, 4],
                    namespace="AWS/RDS",
                ),
                _series(
                    resource_id="db-1",
                    metric_name="CPUUtilization",
                    statistic="Maximum",
                    values=[10, 12, 14],
                    namespace="AWS/RDS",
                ),
                _series(
                    resource_id="db-1",
                    metric_name="DatabaseConnections",
                    statistic="Average",
                    values=[20, 30, 40],  # >> 5 threshold
                    namespace="AWS/RDS",
                ),
            ]
        }
        out = rule_rds_underutilization(
            account_id=None,
            region="us-east-1",
            rds_instances=dbs,
            utilization_by_resource=util,
            lookback_days=30,
        )
        assert out == []

    def test_no_data_not_flagged(self) -> None:
        dbs = [{"db_instance_identifier": "db-1", "db_instance_class": "db.m5.large"}]
        out = rule_rds_underutilization(
            account_id=None,
            region="us-east-1",
            rds_instances=dbs,
            utilization_by_resource={},
            lookback_days=30,
        )
        assert out == []


# ---------------------------------------------------------------------------
# Deterministic ID helper
# ---------------------------------------------------------------------------


class TestDeterministicId:
    def test_id_is_stable(self) -> None:
        a = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-1", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
        )
        b = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-1", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
        )
        assert a == b

    def test_different_inputs_yield_different_ids(self) -> None:
        a = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-1", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
        )
        b = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-2", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
        )
        assert a != b

    def test_suffix_distinguishes(self) -> None:
        a = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-1", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
            suffix="0",
        )
        b = deterministic_recommendation_id(
            account_id="111122223333", region="us-east-1",
            resource_id="vol-1", action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
            suffix="1",
        )
        assert a != b
