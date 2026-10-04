"""Tests for the AWS identity endpoint and service.

No live AWS calls. Boto3 client construction is monkeypatched so the
tests run in CI without AWS credentials.
"""
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_sts_factory(monkeypatch):
    """Replace ``app.services.aws.identity.get_aws_client`` with a factory
    that returns a MagicMock STS client capturing the region argument.
    """
    captured = {"region": None}
    client_mock = MagicMock(name="fake_sts_client")

    def _set_response(payload):
        client_mock.get_caller_identity.return_value = payload

    def _set_error(exc):
        client_mock.get_caller_identity.side_effect = exc

    def _factory(service_name, region=None):
        captured["region"] = region
        captured["service_name"] = service_name
        return client_mock

    monkeypatch.setattr("app.services.aws.identity.get_aws_client", _factory)
    return {"captured": captured, "set_response": _set_response, "set_error": _set_error}


def test_identity_happy_path(client, fake_sts_factory):
    fake_sts_factory["set_response"](
        {
            "Account": "111122223333",
            "Arn": "arn:aws:iam::111122223333:user/dev",
            "UserId": "AIDAEXAMPLEUSERID",
        }
    )

    r = client.get("/aws/identity?region=us-east-1")

    assert r.status_code == 200
    body = r.json()
    assert body["account"] == "111122223333"
    assert body["arn"] == "arn:aws:iam::111122223333:user/dev"
    assert body["user_id"] == "AIDAEXAMPLEUSERID"
    assert body["region"] == "us-east-1"
    assert fake_sts_factory["captured"]["region"] == "us-east-1"
    assert fake_sts_factory["captured"]["service_name"] == "sts"


def test_identity_no_credentials_returns_502_sanitized(client, fake_sts_factory):
    fake_sts_factory["set_error"](NoCredentialsError())

    r = client.get("/aws/identity")

    assert r.status_code == 502
    body = r.json()
    assert body["status"] == "error"
    assert body["error_code"] == "NoCredentialsError"
    # Sanitization: no exception text or Boto3 internals leak.
    text = r.text
    assert "traceback" not in text.lower()
    assert "botocore" not in text.lower()
    # Region should reflect the settings fallback (conftest sets us-east-1).
    assert body["region"] == "us-east-1"


def test_identity_client_error_returns_502_with_code(client, fake_sts_factory):
    fake_sts_factory["set_error"](
        ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "denied"}},
            operation_name="GetCallerIdentity",
        )
    )

    r = client.get("/aws/identity?region=eu-west-1")

    assert r.status_code == 502
    body = r.json()
    assert body["error_code"] == "AccessDenied"
    assert body["region"] == "eu-west-1"
    # Boto3 ClientError internals (operation_name, Error dict, requestId) must
    # not leak. The sanitized code field is fine; the raw message body is not.
    assert "GetCallerIdentity" not in r.text
    assert "requestId" not in r.text
    assert "OperationName" not in r.text


def test_identity_region_query_param_beats_settings(client, fake_sts_factory, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    from app.core.config import get_settings
    get_settings.cache_clear()

    fake_sts_factory["set_response"](
        {"Account": "1", "Arn": "arn:x", "UserId": "u"}
    )

    r = client.get("/aws/identity?region=eu-central-1")

    assert r.status_code == 200
    # The factory must have been called with the query-param region,
    # not the settings fallback (ap-south-1).
    assert fake_sts_factory["captured"]["region"] == "eu-central-1"
    assert r.json()["region"] == "eu-central-1"

    # Cleanup so other tests aren't affected.
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    get_settings.cache_clear()


def test_identity_region_falls_back_to_settings_when_query_absent(
    client, fake_sts_factory, monkeypatch
):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    from app.core.config import get_settings
    get_settings.cache_clear()

    fake_sts_factory["set_response"](
        {"Account": "1", "Arn": "arn:x", "UserId": "u"}
    )

    r = client.get("/aws/identity")

    assert r.status_code == 200
    assert fake_sts_factory["captured"]["region"] == "ap-south-1"
    assert r.json()["region"] == "ap-south-1"

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    get_settings.cache_clear()
