"""Tests for the per-service AWS resource enumerators.

No live AWS calls. Boto3 client construction is monkeypatched so the
tests run in CI without AWS credentials.
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services.aws import resources


@pytest.fixture
def client_factory(monkeypatch):
    """Patch ``get_aws_client`` to return per-service MagicMock clients.

    Returns a dict keyed by service name; accessing ``client_factory["ec2"]``
    yields a MagicMock (created on first access) that enumerators will also
    see when they call ``get_aws_client``.
    """
    from collections import defaultdict

    clients: dict = defaultdict(
        lambda: MagicMock(name="mock_client")
    )

    def _get(service_name, region=None):
        return clients[service_name]

    monkeypatch.setattr(resources, "get_aws_client", _get)
    return clients


def _deny(method_name: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "denied"}},
        operation_name=method_name,
    )


# --- Happy path: one resource per service -------------------------------


def test_ec2_happy_path(client_factory):
    client_factory["ec2"].describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-abc",
                        "State": {"Name": "running"},
                        "InstanceType": "t3.micro",
                        "Tags": [{"Key": "env", "Value": "dev"}],
                    }
                ]
            }
        ]
    }
    result = resources.list_ec2_instances("us-east-1")
    assert result.service == "ec2"
    assert result.status == "ok"
    assert result.error_code is None
    assert len(result.items) == 1
    assert result.items[0]["instance_id"] == "i-abc"
    assert result.items[0]["state"] == "running"
    assert result.items[0]["tags"] == {"env": "dev"}


def test_ebs_happy_path(client_factory):
    client_factory["ec2"].describe_volumes.return_value = {
        "Volumes": [
            {"VolumeId": "vol-1", "Size": 20, "State": "in-use", "Tags": []}
        ]
    }
    result = resources.list_ebs_volumes("us-east-1")
    assert result.service == "ebs"
    assert result.status == "ok"
    assert result.items[0]["volume_id"] == "vol-1"
    assert result.items[0]["size_gb"] == 20


def test_eip_happy_path(client_factory):
    client_factory["ec2"].describe_addresses.return_value = {
        "Addresses": [
            {"PublicIp": "1.2.3.4", "AllocationId": "eipalloc-1", "Tags": []}
        ]
    }
    result = resources.list_elastic_ips("us-east-1")
    assert result.service == "eip"
    assert result.items[0]["public_ip"] == "1.2.3.4"


def test_nat_happy_path(client_factory):
    client_factory["ec2"].describe_nat_gateways.return_value = {
        "NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]
    }
    result = resources.list_nat_gateways("us-east-1")
    assert result.items[0]["nat_gateway_id"] == "nat-1"
    assert result.items[0]["state"] == "available"


def test_elbv2_happy_path(client_factory):
    client_factory["elbv2"].describe_load_balancers.return_value = {
        "LoadBalancers": [
            {
                "LoadBalancerArn": "arn:aws:elasticloadbalancing:us-east-1:111:loadbalancer/app/x",
                "LoadBalancerName": "x",
                "Type": "application",
            }
        ]
    }
    result = resources.list_load_balancers_v2("us-east-1")
    assert result.items[0]["type"] == "application"
    assert result.items[0]["name"] == "x"


def test_rds_happy_path(client_factory):
    client_factory["rds"].describe_db_instances.return_value = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "db1",
                "DBInstanceClass": "db.t3.micro",
                "Engine": "postgres",
                "DBInstanceStatus": "available",
                "TagList": [{"Key": "team", "Value": "data"}],
            }
        ]
    }
    result = resources.list_rds_instances("us-east-1")
    assert result.items[0]["db_instance_identifier"] == "db1"
    assert result.items[0]["tags"] == {"team": "data"}


def test_lambda_happy_path(client_factory):
    client_factory["lambda"].list_functions.return_value = {
        "Functions": [
            {
                "FunctionName": "fn1",
                "FunctionArn": "arn:aws:lambda:us-east-1:111:function:fn1",
                "Runtime": "python3.12",
            }
        ]
    }
    result = resources.list_lambda_functions("us-east-1")
    assert result.items[0]["function_name"] == "fn1"
    assert result.items[0]["runtime"] == "python3.12"


def test_s3_happy_path(client_factory):
    client_factory["s3"].list_buckets.return_value = {
        "Buckets": [
            {"Name": "bkt1", "CreationDate": datetime(2024, 1, 2, 3, 4, 5)}
        ]
    }
    client_factory["s3"].get_bucket_location.return_value = {
        "LocationConstraint": "us-west-2"
    }
    result = resources.list_s3_buckets("us-east-1")
    assert result.items[0]["name"] == "bkt1"
    assert result.items[0]["region"] == "us-west-2"
    # creation_date must be ISO-8601 string (JSON-serializable).
    assert result.items[0]["creation_date"] == "2024-01-02T03:04:05"


# --- Empty results ------------------------------------------------------


def test_each_enumerator_empty_returns_ok_with_no_items(client_factory):
    # Default MagicMock returns for the relevant keys -> empty lists.
    client_factory["ec2"].describe_instances.return_value = {"Reservations": []}
    client_factory["ec2"].describe_volumes.return_value = {"Volumes": []}
    client_factory["ec2"].describe_addresses.return_value = {"Addresses": []}
    client_factory["ec2"].describe_nat_gateways.return_value = {"NatGateways": []}
    client_factory["elbv2"].describe_load_balancers.return_value = {"LoadBalancers": []}
    client_factory["rds"].describe_db_instances.return_value = {"DBInstances": []}
    client_factory["lambda"].list_functions.return_value = {"Functions": []}
    client_factory["s3"].list_buckets.return_value = {"Buckets": []}

    for fn, expected_service in [
        (resources.list_ec2_instances, "ec2"),
        (resources.list_ebs_volumes, "ebs"),
        (resources.list_elastic_ips, "eip"),
        (resources.list_nat_gateways, "nat"),
        (resources.list_load_balancers_v2, "elbv2"),
        (resources.list_rds_instances, "rds"),
        (resources.list_lambda_functions, "lambda"),
        (resources.list_s3_buckets, "s3"),
    ]:
        result = fn("us-east-1")
        assert result.service == expected_service
        assert result.status == "ok", f"{expected_service} should be ok"
        assert result.items == [], f"{expected_service} should have no items"


# --- AccessDenied per service ------------------------------------------


def test_ec2_access_denied_returns_denied_envelope(client_factory):
    client_factory["ec2"].describe_instances.side_effect = _deny("DescribeInstances")
    result = resources.list_ec2_instances("us-east-1")
    assert result.service == "ec2"
    assert result.status == "denied"
    assert result.error_code == "AccessDenied"
    assert result.items == []


def test_elbv2_access_denied_does_not_propagate(client_factory):
    client_factory["elbv2"].describe_load_balancers.side_effect = _deny(
        "DescribeLoadBalancers"
    )
    # Must NOT raise.
    result = resources.list_load_balancers_v2("us-east-1")
    assert result.status == "denied"
    assert result.error_code == "AccessDenied"


def test_other_service_unaffected_when_one_service_denied(client_factory):
    """Aggregator test: one service's AccessDenied does not break the others."""
    # EC2 denies, all others ok.
    client_factory["ec2"].describe_instances.side_effect = _deny("DescribeInstances")
    client_factory["ec2"].describe_volumes.return_value = {"Volumes": []}
    client_factory["ec2"].describe_addresses.return_value = {"Addresses": []}
    client_factory["ec2"].describe_nat_gateways.return_value = {"NatGateways": []}
    client_factory["elbv2"].describe_load_balancers.return_value = {"LoadBalancers": []}
    client_factory["rds"].describe_db_instances.return_value = {"DBInstances": []}
    client_factory["lambda"].list_functions.return_value = {"Functions": []}
    client_factory["s3"].list_buckets.return_value = {"Buckets": []}

    results = resources.enumerate_all_services("us-east-1")

    assert results["ec2"].status == "denied"
    assert results["ec2"].error_code == "AccessDenied"
    # Every other service is unaffected.
    for svc in ("ebs", "eip", "nat", "elbv2", "rds", "lambda", "s3"):
        assert results[svc].status == "ok", f"{svc} should be ok"


def test_non_access_denied_error_returns_error_status(client_factory):
    """A non-AccessDenied ClientError becomes status=error (not denied)."""
    client_factory["rds"].describe_db_instances.side_effect = ClientError(
        error_response={"Error": {"Code": "Throttling", "Message": "rate"}},
        operation_name="DescribeDBInstances",
    )
    result = resources.list_rds_instances("us-east-1")
    assert result.status == "error"
    assert result.error_code == "Throttling"
    assert result.items == []
