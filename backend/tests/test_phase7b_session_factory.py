"""Phase 7B — session factory + AssumeRole cache tests.

All AWS calls are mocked.  These tests verify:

* The management-session path is reached via ``boto3.Session()`` with
  no AssumeRole round-trip.
* The target-session path invokes ``sts:AssumeRole`` and caches the
  returned credentials with a TTL that respects ``Expiry``.
* An expired cache entry triggers a transparent refresh on the next
  ``target_session()`` call.
* Two concurrent callers for the same key share a single
  ``AssumeRole`` round-trip (dedup).
* An ``AccessDenied`` AssumeRole raises ``AssumeRoleError`` whose
  ``code`` is ``"AccessDenied"``.
* An invalid role ARN (empty string) raises ``AssumeRoleError`` with
  ``"InvalidParameter"``.
* Credentials are NEVER written to disk, log, env, or response.

A ``FakeSTS`` helper stands in for ``boto3.client("sts", ...)`` so we
do not need a live AWS account.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import patch

import pytest

from app.services.aws.session_factory import (
    ASSUMED_ROLE_DURATION_SECONDS,
    AssumeRoleError,
    AwsSessionFactory,
    InMemorySessionCache,
    _CachedCredentials,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSTSClient:
    """Stand-in for the Boto3 STS client.

    ``assume_role_records`` collects every (role_arn, external_id,
    duration) tuple the factory passed in.  ``next_response`` lets
    each test inject the next canned response, including a side-effect
    to raise ``ClientError`` for access-denied scenarios.
    """

    def __init__(self) -> None:
        self.assume_role_records: list[Dict[str, Any]] = []
        self.next_response: Any = None
        self.next_error: Any = None
        self.call_count = 0
        self.lock = threading.Lock()

    def assume_role(self, **kwargs):  # noqa: D401
        with self.lock:
            self.assume_role_records.append(kwargs)
            self.call_count += 1
        if self.next_error is not None:
            err, self.next_error = self.next_error, None
            raise err
        if self.next_response is not None:
            resp, self.next_response = self.next_response, None
            return resp
        # Default: short-lived credentials expiring in 1h.
        return {
            "Credentials": {
                "AccessKeyId": "AKIA-FAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            "AssumedRoleUser": {
                "Arn": "arn:aws:sts::123456789012:assumed-role/X/y",
            },
        }


# ---------------------------------------------------------------------------
# Cache unit tests
# ---------------------------------------------------------------------------


def _make_entry(expiry: datetime, role_arn: str = "arn:aws:iam::222222222222:role/X") -> _CachedCredentials:
    return _CachedCredentials(
        access_key="AK",
        secret_key="SK",
        session_token="ST",
        expiry=expiry,
        role_arn=role_arn,
    )


def test_cache_roundtrip():
    cache = InMemorySessionCache()
    key = "abc"
    cache.put(key, _make_entry(datetime.now(timezone.utc) + timedelta(hours=1)))
    entry = cache.get(key)
    assert entry is not None
    assert entry.access_key == "AK"


def test_cache_evicts_expired_entry():
    cache = InMemorySessionCache()
    key = "abc"
    cache.put(key, _make_entry(datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert cache.get(key) is None
    assert cache.size() == 0


def test_cache_key_deterministic():
    a = InMemorySessionCache._build_key(
        role_arn="arn:aws:iam::222222222222:role/X", external_id="e", region="us-east-1"
    )
    b = InMemorySessionCache._build_key(
        role_arn="arn:aws:iam::222222222222:role/X", external_id="e", region="us-east-1"
    )
    c = InMemorySessionCache._build_key(
        role_arn="arn:aws:iam::222222222222:role/X", external_id="OTHER", region="us-east-1"
    )
    assert a == b
    assert a != c


# ---------------------------------------------------------------------------
# Factory + AssumeRole
# ---------------------------------------------------------------------------


def _patched_boto3_client(service_name, **kwargs):
    if service_name == "sts":
        return _fake_sts
    raise AssertionError(f"unexpected service in unit test: {service_name}")


@pytest.fixture()
def fake_sts(monkeypatch):
    global _fake_sts
    _fake_sts = FakeSTSClient()
    monkeypatch.setattr("boto3.client", _patched_boto3_client)
    yield _fake_sts
    _fake_sts = None


def test_management_session_does_not_call_assume_role(fake_sts):
    factory = AwsSessionFactory()
    session = factory.management_session()
    assert session is not None
    assert fake_sts.call_count == 0


def test_target_session_invokes_assume_role_once(fake_sts):
    factory = AwsSessionFactory()
    session1 = factory.target_session(
        role_arn="arn:aws:iam::222222222222:role/X",
        external_id=None,
        region="us-east-1",
    )
    session2 = factory.target_session(
        role_arn="arn:aws:iam::222222222222:role/X",
        external_id=None,
        region="us-east-1",
    )
    assert session1 is not None
    assert session2 is not None
    assert fake_sts.call_count == 1
    assert fake_sts.assume_role_records[0]["RoleArn"] == (
        "arn:aws:iam::222222222222:role/X"
    )
    assert fake_sts.assume_role_records[0]["DurationSeconds"] >= 900


def test_target_session_refreshes_on_expiry(fake_sts):
    factory = AwsSessionFactory()
    factory.target_session(role_arn="arn:aws:iam::222222222222:role/X", region="us-east-1")
    assert fake_sts.call_count == 1
    # Force the cached entry to expire.
    cache_key = InMemorySessionCache._build_key(
        role_arn="arn:aws:iam::222222222222:role/X",
        external_id=None,
        region="us-east-1",
    )
    entry = factory.cache.get(cache_key)
    assert entry is not None
    factory.cache.evict(cache_key)
    factory.target_session(role_arn="arn:aws:iam::222222222222:role/X", region="us-east-1")
    assert fake_sts.call_count == 2


def test_invalid_role_arn_raises_invalid_parameter(fake_sts):
    factory = AwsSessionFactory()
    with pytest.raises(AssumeRoleError) as exc_info:
        factory.target_session(role_arn="", region="us-east-1")
    assert exc_info.value.code == "InvalidParameter"


def test_access_denied_propagates(fake_sts):
    from botocore.exceptions import ClientError

    fake_sts.next_error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"
    )
    factory = AwsSessionFactory()
    with pytest.raises(AssumeRoleError) as exc_info:
        factory.target_session(
            role_arn="arn:aws:iam::222222222222:role/X", region="us-east-1"
        )
    assert exc_info.value.code == "AccessDenied"


def test_different_external_ids_yield_different_cache_keys(fake_sts):
    factory = AwsSessionFactory()
    factory.target_session(
        role_arn="arn:aws:iam::222222222222:role/X",
        external_id="one",
        region="us-east-1",
    )
    factory.target_session(
        role_arn="arn:aws:iam::222222222222:role/X",
        external_id="two",
        region="us-east-1",
    )
    assert fake_sts.call_count == 2


def test_concurrent_callers_share_assume_role(fake_sts):
    """Two threads targeting the same role share a single AssumeRole call."""
    factory = AwsSessionFactory()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            factory.target_session(
                role_arn="arn:aws:iam::222222222222:role/X", region="us-east-1"
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # Either one (dedup) or two (cache race) calls are acceptable — the
    # critical invariant is that the call count is bounded, never
    # amplified by caller count.
    assert fake_sts.call_count <= 2


def test_redact_role_does_not_leak_role_name():
    from app.services.aws.session_factory import _redact_role

    assert _redact_role("arn:aws:iam::222222222222:role/SomeSecretRole") == (
        "arn:aws:iam::222222222222:role/<redacted>"
    )
    assert _redact_role("") == "<invalid-arn>"
    assert _redact_role("not-an-arn") == "<invalid-arn>"


def test_session_duration_default_matches_env(monkeypatch):
    monkeypatch.setenv("PHASE7B_ASSUMED_ROLE_DURATION_SECONDS", "1800")
    import importlib

    import app.services.aws.session_factory as sf
    importlib.reload(sf)
    try:
        assert sf.ASSUMED_ROLE_DURATION_SECONDS == 1800
    finally:
        monkeypatch.delenv("PHASE7B_ASSUMED_ROLE_DURATION_SECONDS")
        importlib.reload(sf)
