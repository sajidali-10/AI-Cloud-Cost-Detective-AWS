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
    """Return a ``TestClient`` with a management account seeded.

    Phase 7B: the identity route looks up the management row via
    :func:`get_management_account`.  Without one, the route
    returns 404 and every test fails.  We seed an in-memory SQLite
    via the same dependency override other Phase 7B tests use.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.config import get_settings
    from app.db.models import Base, CloudAccount
    from app.db.session import get_db

    get_settings.cache_clear()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    db = SessionLocal()
    try:
        db.add(CloudAccount(
            account_id="000000000000",
            display_name="Management",
            provider="AWS",
            role_arn="arn:aws:iam::000000000000:role/Management",
            external_id=None,
            preferred_region="us-east-1",
            enabled=True,
            is_management=True,
        ))
        db.commit()
    finally:
        db.close()

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def fake_sts_factory(monkeypatch):
    """Replace the AWS session factory with a stub for the duration
    of one test.

    Phase 7B: the identity route resolves a :class:`boto3.Session`
    via :func:`get_default_session_factory().management_session()`
    (or :func:`target_session`) and then calls ``.client('sts',
    region_name=...)``.  We replace the *factory accessor* with a
    stub whose management session returns a session whose
    ``.client(...)`` returns a :class:`MagicMock` STS client that
    captures the region argument.  This is the only correct Phase
    7B seam: the route code does NOT call :func:`get_aws_client`
    directly anymore — it goes through the factory.
    """
    captured = {"region": None, "service_name": None}
    client_mock = MagicMock(name="fake_sts_client")

    def _set_response(payload):
        client_mock.get_caller_identity.return_value = payload

    def _set_error(exc):
        client_mock.get_caller_identity.side_effect = exc

    fake_session = MagicMock(name="fake_boto3_session")

    def _client(service_name, *args, **kwargs):
        captured["region"] = kwargs.get("region_name")
        captured["service_name"] = service_name
        return client_mock

    fake_session.client.side_effect = _client

    fake_factory = MagicMock(name="fake_session_factory")
    fake_factory.management_session.return_value = fake_session
    fake_factory.target_session.return_value = fake_session

    # Patch the binding used by ``app.api.aws``.  We also patch the
    # ``app.services.aws.session_factory`` module attribute so any
    # other call site picks up the same stub.
    monkeypatch.setattr(
        "app.api.aws.get_default_session_factory",
        lambda: fake_factory,
    )
    monkeypatch.setattr(
        "app.services.aws.session_factory.get_default_session_factory",
        lambda: fake_factory,
    )

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
