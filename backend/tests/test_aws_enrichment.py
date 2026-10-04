"""Tests for the optional enrichment layers and the aggregator route.

No live AWS calls. Boto3 client construction is monkeypatched so the
tests run in CI without AWS credentials.
"""
from collections import defaultdict
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi.testclient import TestClient

from app.main import app
from app.services.aws import enrichment


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def aws_clients(monkeypatch):
    """Patch ``get_aws_client`` across enrichment + resources + identity.

    Returns a dict keyed by service name. Accessing any key creates a
    MagicMock on demand so enumerators and enrichers both see the same
    set of mocks.
    """
    clients = defaultdict(lambda: MagicMock(name="mock_client"))

    def _get(service_name, region=None):
        return clients[service_name]

    # Patch in both modules - enrichment imports get_aws_client directly,
    # the aggregator in the route imports it via app.services.aws.resources.
    monkeypatch.setattr(enrichment, "get_aws_client", _get)
    monkeypatch.setattr("app.services.aws.resources.get_aws_client", _get)
    return clients


def _deny(method: str) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "denied"}},
        operation_name=method,
    )


# --- Resource Explorer: index missing -> degraded ----------------------


def test_resource_explorer_index_missing_returns_degraded(aws_clients):
    aws_clients["resource-explorer-2"].search.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": "Index not found",
            }
        },
        operation_name="Search",
    )
    result = enrichment.search_resource_explorer(region="us-east-1", query="*")
    assert result.available is False
    assert result.error_code == "ResourceNotFoundException"
    assert result.results == []
    assert result.query == "*"


# --- Resource Explorer: AccessDenied -> degraded -----------------------


def test_resource_explorer_access_denied_returns_degraded(aws_clients):
    aws_clients["resource-explorer-2"].search.side_effect = _deny("Search")
    result = enrichment.search_resource_explorer(region="us-east-1", query="")
    assert result.available is False
    assert result.error_code == "AccessDenied"
    assert result.results == []


# --- Resource Explorer: happy path --------------------------------------


def test_resource_explorer_happy_path_returns_results(aws_clients):
    aws_clients["resource-explorer-2"].search.return_value = {
        "Resources": [
            {
                "Arn": "arn:aws:ec2:us-east-1:111:instance/i-abc",
                "Service": "ec2",
                "ResourceType": "AWS::EC2::Instance",
                "Region": "us-east-1",
            },
            {
                "Arn": "arn:aws:s3:::bkt1",
                "Service": "s3",
                "ResourceType": "AWS::S3::Bucket",
                "Region": "us-east-1",
            },
        ]
    }
    result = enrichment.search_resource_explorer(region="us-east-1", query="")
    assert result.available is True
    assert result.error_code is None
    assert len(result.results) == 2
    assert result.results[0]["arn"] == "arn:aws:ec2:us-east-1:111:instance/i-abc"
    assert result.results[0]["service"] == "ec2"


def test_resource_explorer_no_credentials_does_not_propagate(aws_clients):
    aws_clients["resource-explorer-2"].search.side_effect = NoCredentialsError()
    result = enrichment.search_resource_explorer(region="us-east-1", query="")
    assert result.available is False
    assert result.error_code == "NoCredentialsError"


# --- Tagging API: empty ARNs -> available=True, empty dict --------------


def test_tagging_api_empty_arns_returns_empty_available(aws_clients):
    result = enrichment.get_tags_from_tagging_api(region="us-east-1", arns=None)
    assert result.available is True
    assert result.tags_by_arn == {}
    # Must NOT have called get_resources when there are no ARNs.
    aws_clients["resourcegroupstaggingapi"].get_resources.assert_not_called()


def test_tagging_api_access_denied_returns_degraded(aws_clients):
    aws_clients["resourcegroupstaggingapi"].get_resources.side_effect = _deny(
        "GetResources"
    )
    result = enrichment.get_tags_from_tagging_api(
        region="us-east-1", arns=["arn:aws:ec2:us-east-1:111:instance/i-abc"]
    )
    assert result.available is False
    assert result.error_code == "AccessDenied"
    assert result.tags_by_arn == {}


def test_tagging_api_happy_path_merges_tags_by_arn(aws_clients):
    aws_clients["resourcegroupstaggingapi"].get_resources.return_value = {
        "ResourceTagMappingList": [
            {
                "ResourceARN": "arn:aws:ec2:us-east-1:111:instance/i-abc",
                "Tags": [
                    {"Key": "env", "Value": "dev"},
                    {"Key": "team", "Value": "data"},
                ],
            },
            {
                "ResourceARN": "arn:aws:s3:::bkt1",
                "Tags": [{"Key": "env", "Value": "prod"}],
            },
        ],
        "PaginationToken": "",  # falsy -> loop ends after one call
    }
    result = enrichment.get_tags_from_tagging_api(
        region="us-east-1",
        arns=[
            "arn:aws:ec2:us-east-1:111:instance/i-abc",
            "arn:aws:s3:::bkt1",
        ],
    )
    assert result.available is True
    assert result.error_code is None
    assert result.tags_by_arn == {
        "arn:aws:ec2:us-east-1:111:instance/i-abc": {"env": "dev", "team": "data"},
        "arn:aws:s3:::bkt1": {"env": "prod"},
    }


# --- Aggregator route (GET /api/aws/resources) --------------------------


def _empty_enumerator(aws_clients):
    """Configure all per-service enumerators to return empty ok results."""
    aws_clients["ec2"].describe_instances.return_value = {"Reservations": []}
    aws_clients["ec2"].describe_volumes.return_value = {"Volumes": []}
    aws_clients["ec2"].describe_addresses.return_value = {"Addresses": []}
    aws_clients["ec2"].describe_nat_gateways.return_value = {"NatGateways": []}
    aws_clients["elbv2"].describe_load_balancers.return_value = {"LoadBalancers": []}
    aws_clients["rds"].describe_db_instances.return_value = {"DBInstances": []}
    aws_clients["lambda"].list_functions.return_value = {"Functions": []}
    aws_clients["s3"].list_buckets.return_value = {"Buckets": []}


def test_resources_aggregator_returns_region_and_envelopes(client, aws_clients):
    _empty_enumerator(aws_clients)
    aws_clients["resource-explorer-2"].search.side_effect = ClientError(
        error_response={"Error": {"Code": "ResourceNotFoundException", "Message": "x"}},
        operation_name="Search",
    )

    r = client.get("/aws/resources?region=us-east-1")

    assert r.status_code == 200
    body = r.json()
    assert body["region"] == "us-east-1"
    assert set(body["services"].keys()) == {
        "ec2", "ebs", "eip", "nat", "elbv2", "rds", "lambda", "s3"
    }
    # All services ok (empty results).
    for svc, envelope in body["services"].items():
        assert envelope["status"] == "ok", f"{svc} should be ok"
        assert envelope["items"] == []
    # Enrichment: RE degraded (index missing), tagging available=True empty.
    assert body["enrichment"]["resource_explorer"]["available"] is False
    assert body["enrichment"]["resource_explorer"]["error_code"] == "ResourceNotFoundException"
    assert body["enrichment"]["tagging"]["available"] is True
    assert body["enrichment"]["tagging"]["tags_by_arn"] == {}


def test_resources_aggregator_region_falls_back_to_settings(
    client, aws_clients, monkeypatch
):
    _empty_enumerator(aws_clients)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    from app.core.config import get_settings
    get_settings.cache_clear()

    r = client.get("/aws/resources")  # no ?region=

    assert r.status_code == 200
    assert r.json()["region"] == "ap-south-1"

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    get_settings.cache_clear()


def test_resources_aggregator_does_not_propagate_service_denied(
    client, aws_clients
):
    """Per-service AccessDenied must NOT bubble up as 500."""
    aws_clients["ec2"].describe_instances.side_effect = _deny("DescribeInstances")
    _empty_enumerator(aws_clients)
    # Override the empty setup for the one we want denied.
    aws_clients["ec2"].describe_instances.side_effect = _deny("DescribeInstances")

    r = client.get("/aws/resources?region=us-east-1")

    assert r.status_code == 200
    body = r.json()
    # EC2 is denied; everything else still ok.
    assert body["services"]["ec2"]["status"] == "denied"
    assert body["services"]["ec2"]["error_code"] == "AccessDenied"
    for svc in ("ebs", "eip", "nat", "elbv2", "rds", "lambda", "s3"):
        assert body["services"][svc]["status"] == "ok", f"{svc} should be ok"
