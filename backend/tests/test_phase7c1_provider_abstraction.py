"""Phase 7C.1 — provider abstraction tests.

Verifies the provider-neutral layer without touching AWS:

* `CloudProviderKind` enum and provider metadata are stable.
* `CloudCapability` / `CloudCapabilityState` enums round-trip.
* `CloudCredentialStrategy` includes the reserved Azure/Rackspace
  identifiers (shapes only).
* `ProviderRegistry` registers, disables, re-enables, and refuses
  duplicate kinds on an empty registry.
* `ProviderNotFound` distinguishes "not registered" from
  "disabled".
* `AWSCloudProvider` declares its credential strategies and
  capability surface (using a fresh empty registry + the real
  adapter so we never clobber the process-wide singleton that
  other test files depend on).
* The migration path in the ORM does not require data migration.

These tests do NOT exercise the live AWS API.
"""
from __future__ import annotations

import pytest

from app.services.cloud import (
    CloudCapability,
    CloudCapabilityState,
    CloudCredentialStrategy,
    CloudProviderKind,
)
from app.services.cloud.base import CloudProvider
from app.services.cloud.registry import (
    ProviderAlreadyRegistered,
    ProviderNotFound,
    ProviderRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeAdapter(CloudProvider):
    """Minimal adapter used to exercise the registry without AWS.

    This is intentionally a real subclass of :class:`CloudProvider`
    so the registry's ``register`` method (which inspects the
    ``provider_kind`` class attribute) accepts it.  None of the
    abstract methods are exercised.
    """

    provider_kind = CloudProviderKind.AWS
    credential_strategies = ()

    def list_regions(self):  # pragma: no cover
        return []

    def validate_account(self, account):  # pragma: no cover
        raise NotImplementedError

    def get_provider_account(self, account):  # pragma: no cover
        raise NotImplementedError

    def get_capabilities(self, account=None):  # pragma: no cover
        raise NotImplementedError

    async def get_cost_summary(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def get_cost_trend(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def get_cost_by_service(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def get_resources(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def get_resource_summary(self, **_):  # pragma: no cover
        raise NotImplementedError

    async def get_recommendations(self, **_):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_provider_kind_enum_members():
    assert {k.value for k in CloudProviderKind} == {"AWS", "AZURE", "RACKSPACE"}


def test_capability_enum_members():
    capabilities = {c.value for c in CloudCapability}
    assert {"COSTS", "RESOURCES", "OPTIMIZATION", "METRICS", "AI_EVIDENCE"}.issubset(capabilities)
    assert {s.value for s in CloudCapabilityState} == {
        "SUPPORTED",
        "UNSUPPORTED",
        "UNAVAILABLE",
        "PERMISSION_DENIED",
        "NOT_CONFIGURED",
    }


def test_credential_strategies_include_azure_and_rackspace():
    aws_strats = [s for s in CloudCredentialStrategy if s.value.startswith("AWS_")]
    azure_strats = [s for s in CloudCredentialStrategy if s.value.startswith("AZURE_")]
    rack_strats = [s for s in CloudCredentialStrategy if s.value.startswith("RACKSPACE_")]
    assert {s.value for s in aws_strats} == {
        "AWS_INSTANCE_PROFILE",
        "AWS_ASSUME_ROLE",
    }
    assert {s.value for s in azure_strats} == {
        "AZURE_WORKLOAD_FEDERATION",
        "AZURE_CLIENT_SECRET",
    }
    assert {s.value for s in rack_strats} == {"RACKSPACE_API_KEY"}
    assert CloudCredentialStrategy.AWS_INSTANCE_PROFILE.provider == CloudProviderKind.AWS
    assert CloudCredentialStrategy.AZURE_WORKLOAD_FEDERATION.provider == CloudProviderKind.AZURE
    assert CloudCredentialStrategy.RACKSPACE_API_KEY.provider == CloudProviderKind.RACKSPACE


# ---------------------------------------------------------------------------
# Registry (uses .empty() so we don't clobber the singleton other
# test files rely on).
# ---------------------------------------------------------------------------


def test_registry_singleton_returns_same_instance():
    """Two calls to ``current()`` return the same instance — but
    we never reset the singleton from these tests."""
    a = ProviderRegistry.current()
    b = ProviderRegistry.current()
    assert a is b


def test_registry_empty_distinguishes_disabled_from_missing():
    reg = ProviderRegistry.empty()
    with pytest.raises(ProviderNotFound) as excinfo:
        reg.require(CloudProviderKind.AWS)
    assert excinfo.value.disabled is False
    adapter = _FakeAdapter()
    reg.register(adapter)
    reg.disable(CloudProviderKind.AWS)
    with pytest.raises(ProviderNotFound) as excinfo:
        reg.require(CloudProviderKind.AWS)
    assert excinfo.value.disabled is True
    reg.enable(CloudProviderKind.AWS)
    assert reg.try_get(CloudProviderKind.AWS) is not None


def test_registry_rejects_duplicate_registration():
    reg = ProviderRegistry.empty()
    reg.register(_FakeAdapter())
    with pytest.raises(ProviderAlreadyRegistered):
        reg.register(_FakeAdapter())


def test_registry_known_vs_available_distinguishes_disabled():
    reg = ProviderRegistry.empty()
    reg.register(_FakeAdapter())
    assert "AWS" in {k.value for k in reg.known_kinds()}
    assert "AWS" in {k.value for k in reg.available_kinds()}
    reg.disable(CloudProviderKind.AWS)
    assert "AWS" in {k.value for k in reg.known_kinds()}
    assert "AWS" not in {k.value for k in reg.available_kinds()}


# ---------------------------------------------------------------------------
# AWS adapter capability surface (uses the singleton, never resets)
# ---------------------------------------------------------------------------


def test_aws_adapter_capabilities_publish_supported_set():
    """The AWS adapter publishes a SUPPORTED set that matches the
    Phase 7B coverage: COSTS / RESOURCES / OPTIMIZATION / METRICS /
    AI_EVIDENCE.  BILLING and ADVISOR are intentionally UNSUPPORTED.
    """
    reg = ProviderRegistry.current()
    adapter = reg.try_get(CloudProviderKind.AWS)
    assert adapter is not None
    snapshot = adapter.get_capabilities()
    assert snapshot.provider == CloudProviderKind.AWS
    expected_supported = {
        CloudCapability.COSTS,
        CloudCapability.RESOURCES,
        CloudCapability.OPTIMIZATION,
        CloudCapability.METRICS,
        CloudCapability.AI_EVIDENCE,
    }
    for cap in expected_supported:
        assert snapshot.has(cap), f"expected {cap.value} to be SUPPORTED"
    assert (
        snapshot.capabilities.get(CloudCapability.BILLING)
        == CloudCapabilityState.UNSUPPORTED
    )
    assert (
        snapshot.capabilities.get(CloudCapability.ADVISOR)
        == CloudCapabilityState.UNSUPPORTED
    )


def test_aws_adapter_describe_emits_json_safe_shape():
    reg = ProviderRegistry.current()
    adapter = reg.try_get(CloudProviderKind.AWS)
    snapshot = adapter.get_capabilities()
    described = snapshot.describe()
    assert isinstance(described, list)
    assert described
    sample = described[0]
    assert set(sample.keys()) == {"capability", "state", "detail"}
    assert isinstance(sample["detail"], dict)


def test_aws_adapter_list_regions_includes_us_east_1():
    reg = ProviderRegistry.current()
    adapter = reg.try_get(CloudProviderKind.AWS)
    regions = adapter.list_regions()
    ids = {r.region_id for r in regions}
    assert "us-east-1" in ids
    assert all(r.provider == CloudProviderKind.AWS for r in regions)


def test_aws_adapter_declares_credential_strategies():
    """Phase 7C.1: the AWS adapter's credential_strategies class
    attribute is the source of truth for what the operator can
    select at the API surface."""
    reg = ProviderRegistry.current()
    adapter = reg.try_get(CloudProviderKind.AWS)
    assert any(
        s.value == "AWS_INSTANCE_PROFILE" for s in adapter.credential_strategies
    )
    assert any(
        s.value == "AWS_ASSUME_ROLE" for s in adapter.credential_strategies
    )
    # No Azure / Rackspace strategies on the AWS adapter.
    assert not any(
        s.value.startswith("AZURE_") for s in adapter.credential_strategies
    )
    assert not any(
        s.value.startswith("RACKSPACE_") for s in adapter.credential_strategies
    )


# ---------------------------------------------------------------------------
# ORM: provider_metadata default
# ---------------------------------------------------------------------------


def test_cloud_account_orm_carries_provider_metadata():
    """The Phase 7C.1 ORM has ``provider_metadata`` (JSON/JSONB) plus
    the nullable Azure-specific columns.  AWS rows store ``{}`` and
    leave the Azure columns NULL.  This test pins the column surface
    so a future migration that drops these columns will fail loudly."""
    from app.db.models import CloudAccount
    columns = {c.name for c in CloudAccount.__table__.columns}
    assert {"tenant_id", "subscription_id", "provider_metadata"}.issubset(columns)
    assert "role_arn" in columns  # backwards-compat invariant
    assert "account_id" in columns
