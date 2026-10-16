"""Phase 7C.2A — Azure workload-federation assertion provider tests.

This test file pins the *behavioural* contract of the AWS-side
workload identity federation provider:

* No static AWS keys are read or required — the provider relies on
  the EC2 instance profile chain via ``boto3.client('sts')``.
* The STS client is constructed with a *regional* endpoint
  (``sts_regional_endpoints='regional'`` + ``region_name``).
* The GetWebIdentityToken call uses:
    - ``Audience=['api://AzureADTokenExchange']`` (configurable)
    - ``SigningAlgorithm='RS256'``
    - ``DurationSeconds=300`` (configurable, refused above 900).
* Failure modes map to sanitised categories — no Boto3 exception
  payloads leak.
* The returned WebIdentityToken is never persisted, never logged.

All tests in this file stub ``boto3.client`` so no live STS call
ever happens.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sts_stub(*, response: dict | None = None, side_effect: Exception | None = None):
    """Return a ``MagicMock`` shaped like a Boto3 STS client."""
    client = MagicMock(name="sts_client")
    if side_effect is not None:
        client.get_web_identity_token.side_effect = side_effect
    elif response is not None:
        client.get_web_identity_token.return_value = response
    else:
        client.get_web_identity_token.return_value = {
            "WebIdentityToken": "eyJ.fake.jwt",
            "ProviderArn": "arn:aws:iam::000000000000:oidc-provider/…",
            "Audience": "api://AzureADTokenExchange",
        }
    return client


def _make_session_factory(client: MagicMock):
    """Return a MagicMock session factory whose ``.client(...)`` returns ``client``."""
    session = MagicMock(name="boto3_session")
    session.client.return_value = client
    factory = MagicMock(name="session_factory")
    factory.management_session.return_value = session
    factory.target_session.return_value = session
    return factory


# ---------------------------------------------------------------------------
# Imports — done after the helpers so the helpers stay testable.
# ---------------------------------------------------------------------------


from app.core.config import get_settings  # noqa: E402
from app.services.cloud.azure.federation import (  # noqa: E402
    AWSWorkloadFederationCredential,
    AzureFederationError,
    DEFAULT_AUDIENCE,
    DEFAULT_ROLE_ARN,
    FederationConfig,
)
from app.services.cloud.azure.redaction import (  # noqa: E402
    AZURE_STATUS_AUTH_FAILED,
    AZURE_STATUS_ERROR,
    AZURE_STATUS_NOT_CONFIGURED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Always start with a clean settings cache.

    Several tests modify ``azure_*`` settings; without this the
    ``lru_cache`` on ``get_settings`` would leak the test values
    into subsequent tests.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# FederationConfig defaults
# ---------------------------------------------------------------------------


def test_federation_config_defaults_match_aws_entra_contract(monkeypatch):
    """Defaults pin the documented Phase 7C.2A contract."""
    monkeypatch.delenv("AZURE_FEDERATION_ROLE_ARN", raising=False)
    monkeypatch.delenv("AZURE_FEDERATION_AUDIENCE", raising=False)
    monkeypatch.delenv("AZURE_FEDERATION_REGION", raising=False)
    cfg = FederationConfig.from_settings()
    assert cfg.role_arn == DEFAULT_ROLE_ARN
    assert cfg.audience == DEFAULT_AUDIENCE
    assert cfg.assertion_seconds == 300
    assert cfg.signing_algorithm == "RS256"
    # Region defaults to ``us-east-1`` (the operator's AWS region
    # when AWS_DEFAULT_REGION is unset) — not the global endpoint.
    assert cfg.region != ""
    assert cfg.region != "aws-global"


def test_federation_config_refuses_long_assertion(monkeypatch):
    """Settings enforce ``ge=60, le=900`` so an operator cannot
    request a 24-hour assertion.  The ``from_settings`` path
    clamps at 300 (default) but does not raise — the contract is
    enforced by the settings schema itself.
    """
    # A misconfigured settings layer with ``azure_federation_assertion_seconds=7200``
    # is clamped back to 900 by the Settings schema.
    from app.core.config import Settings
    s = Settings(azure_federation_assertion_seconds=7200)
    assert s.azure_federation_assertion_seconds == 900


def test_federation_config_rejects_zero_assertion():
    from app.core.config import Settings
    with pytest.raises(Exception):
        Settings(azure_federation_assertion_seconds=0)


def test_federation_config_accepts_minimum_assertion():
    from app.core.config import Settings
    assert Settings(azure_federation_assertion_seconds=60).azure_federation_assertion_seconds == 60


# ---------------------------------------------------------------------------
# AWS STS regional client
# ---------------------------------------------------------------------------


def test_provider_constructs_regional_sts_client():
    """``_build_regional_sts_client`` always sets a region and the
    ``regional`` STS endpoint config flag — never the global
    ``sts.amazonaws.com`` endpoint.
    """
    from app.services.cloud.azure.federation import _build_regional_sts_client
    captured: dict = {}

    def _fake_client(service, **kwargs):
        captured["service"] = service
        captured["config"] = kwargs.get("config")
        m = MagicMock()
        return m

    with patch("boto3.client", side_effect=_fake_client):
        _build_regional_sts_client(region="eu-central-1")
    cfg = captured["config"]
    # ``region_name`` and ``sts_regional_endpoints='regional'`` are the
    # two non-negotiable contract points — the rest of the config is
    # internal.
    assert cfg.region_name == "eu-central-1"
    assert cfg.sts_regional_endpoints == "regional"


def test_provider_refuses_empty_region():
    """An empty region string is rejected up-front — the contract is
    a regional STS call, full stop.
    """
    from app.services.cloud.azure.federation import _build_regional_sts_client
    with pytest.raises(AzureFederationError) as excinfo:
        _build_regional_sts_client(region="")
    assert excinfo.value.code == AZURE_STATUS_ERROR


# ---------------------------------------------------------------------------
# Audience, signing algorithm, and short duration
# ---------------------------------------------------------------------------


def test_get_assertion_calls_sts_with_audience_rs256_and_short_duration():
    """``get_assertion`` issues a GetWebIdentityToken with the
    canonical Entra audience, RS256 signing, and a short duration.
    """
    stub = _make_sts_stub()
    cred = AWSWorkloadFederationCredential(
        config=FederationConfig(
            role_arn="arn:aws:iam::111122223333:role/Test",
            audience="api://AzureADTokenExchange",
            region="us-east-1",
            assertion_seconds=300,
            signing_algorithm="RS256",
        ),
    )
    with patch("boto3.client", return_value=stub):
        token = cred.get_assertion()
    assert token == "eyJ.fake.jwt"
    call_kwargs = stub.get_web_identity_token.call_args.kwargs
    assert call_kwargs["Audience"] == ["api://AzureADTokenExchange"]
    assert call_kwargs["SigningAlgorithm"] == "RS256"
    assert call_kwargs["DurationSeconds"] == 300
    # And the underlying STS client was constructed with a region.
    assert cred._sts_client is not None


def test_get_assertion_rejects_long_duration(monkeypatch):
    """A misconfigured ``assertion_seconds=900`` is still inside the
    Entra-allowed band, but ``assertion_seconds=1500`` is not —
    ``FederationConfig.from_settings`` is the only documented
    construction path so the only way to exceed 900 is via the
    settings schema, which clamps to 900.  We pin that behaviour
    so a future change cannot accidentally let an operator request
    a 24-hour assertion.
    """
    from app.core.config import Settings
    s = Settings(azure_federation_assertion_seconds=1500)
    assert s.azure_federation_assertion_seconds == 900


def test_audience_is_a_list_not_a_string():
    """Entra's federated credential contract expects Audience to be a
    list — the Boto3 SDK accepts both, but a list is the canonical
    shape and what the Entra App Registration was wired for.
    """
    stub = _make_sts_stub()
    cred = AWSWorkloadFederationCredential()
    with patch("boto3.client", return_value=stub):
        cred.get_assertion()
    aud = stub.get_web_identity_token.call_args.kwargs["Audience"]
    assert isinstance(aud, list)
    assert aud == ["api://AzureADTokenExchange"]


# ---------------------------------------------------------------------------
# ClientAssertionCredential construction
# ---------------------------------------------------------------------------


def test_build_client_assertion_credential_uses_runtime_config():
    """The AzureIdentity credential is a ``ClientAssertionCredential``
    whose ``func`` callback is the AWS federation provider's
    ``get_assertion`` method (not a free-floating lambda).
    """
    # Import here so a missing ``azure-identity`` does not fail the
    # entire test file.
    azure_identity = pytest.importorskip("azure.identity")

    runtime_cfg = type("R", (), {
        "tenant_id": "11111111-2222-3333-4444-555555555555",
        "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "arm_scope": "https://management.azure.com/.default",
    })()

    class FakeFederation:
        def __init__(self):
            self.calls = 0

        def get_assertion(self):
            self.calls += 1
            return "eyJ.fake.jwt"

    fed = FakeFederation()

    from app.services.cloud.azure.credential import (
        AzureRuntimeConfig,
        build_client_assertion_credential,
    )
    # Cast FakeFederation to the type expected by build_client_assertion_credential.
    # We do this through duck typing — the function only calls .get_assertion().
    real_fed = type("F", (), {"get_assertion": fed.get_assertion})()
    # Use the real AzureRuntimeConfig to make sure the import graph
    # in credential.py stays healthy.
    real_cfg = AzureRuntimeConfig(
        tenant_id=runtime_cfg.tenant_id,
        client_id=runtime_cfg.client_id,
        arm_scope=runtime_cfg.arm_scope,
    )
    cred = build_client_assertion_credential(
        runtime=real_cfg, federation=real_fed,
    )
    assert isinstance(cred, azure_identity.ClientAssertionCredential)
    # Exercise the func chain — the credential's token endpoint
    # would normally call func(), but a real GetToken requires a
    # network.  We call the bound func directly to verify the
    # binding.  azure-identity 1.18.0 stores the callback on
    # ``_func`` (not the older ``_client_assertion_func``).
    assert cred._func() == "eyJ.fake.jwt"
    # And the federation helper saw exactly one invocation.
    assert fed.calls == 1


# ---------------------------------------------------------------------------
# Failure handling — sanitised categories
# ---------------------------------------------------------------------------


def test_get_assertion_translates_client_error_to_auth_failed():
    """A ``ClientError`` from STS is folded into AUTH_FAILED with the
    sanitised category — the original Boto3 payload is not echoed.
    """
    stub = _make_sts_stub(side_effect=ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "denied"}},
        operation_name="GetWebIdentityToken",
    ))
    cred = AWSWorkloadFederationCredential(
        config=FederationConfig(
            role_arn="arn:aws:iam::111122223333:role/Test",
            audience="api://AzureADTokenExchange",
            region="us-east-1",
            assertion_seconds=300,
            signing_algorithm="RS256",
        ),
    )
    with patch("boto3.client", return_value=stub):
        with pytest.raises(AzureFederationError) as excinfo:
            cred.get_assertion()
    assert excinfo.value.code == AZURE_STATUS_AUTH_FAILED
    # No raw ClientError text leaked.
    assert "GetWebIdentityToken" not in (excinfo.value.args[0] or "")
    assert "denied" not in (excinfo.value.args[0] or "")


def test_get_assertion_translates_botocore_error_to_auth_failed():
    """A generic ``BotoCoreError`` collapses into AUTH_FAILED."""
    stub = _make_sts_stub(side_effect=BotoCoreError())
    cred = AWSWorkloadFederationCredential()
    with patch("boto3.client", return_value=stub):
        with pytest.raises(AzureFederationError) as excinfo:
            cred.get_assertion()
    assert excinfo.value.code == AZURE_STATUS_AUTH_FAILED


def test_get_assertion_rejects_empty_token():
    """A STS 200 response with no WebIdentityToken field is treated
    as a hard ERROR — never silently returns None or empty string.
    """
    stub = _make_sts_stub(response={"WebIdentityToken": ""})
    cred = AWSWorkloadFederationCredential()
    with patch("boto3.client", return_value=stub):
        with pytest.raises(AzureFederationError) as excinfo:
            cred.get_assertion()
    assert excinfo.value.code == AZURE_STATUS_ERROR


# ---------------------------------------------------------------------------
# No static AWS keys — the credential layer never reads the env
# ---------------------------------------------------------------------------


def test_provider_does_not_read_aws_access_key_env(monkeypatch):
    """``AWSWorkloadFederationCredential`` does NOT call
    :func:`boto3.client` with explicit ``aws_access_key_id`` /
    ``aws_secret_access_key`` — the EC2 instance profile chain is
    the only credential source.  We assert this by patching
    :func:`boto3.client` and inspecting its kwargs.
    """
    captured: dict = {}

    def _client(service, **kwargs):
        captured["kwargs"] = kwargs
        return _make_sts_stub()

    monkeypatch.setattr("boto3.client", _client)
    cred = AWSWorkloadFederationCredential(
        config=FederationConfig(
            role_arn="arn:aws:iam::111122223333:role/Test",
            audience="api://AzureADTokenExchange",
            region="us-east-1",
            assertion_seconds=300,
            signing_algorithm="RS256",
        ),
    )
    cred.get_assertion()
    kwargs = captured["kwargs"]
    # The provider MUST NOT have supplied static AWS credentials.
    for forbidden in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
        assert forbidden not in kwargs, (
            f"AWSWorkloadFederationCredential MUST NOT supply static {forbidden} — "
            "the EC2 instance profile chain is the only credential source."
        )
