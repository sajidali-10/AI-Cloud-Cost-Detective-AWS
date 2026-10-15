"""Phase 7C.2C — Azure adapter capability + integration tests.

* ``RESOURCES=SUPPORTED`` when runtime configured.
* Adapter methods are no longer ``NotImplementedError`` stubs.
* AWS adapter unchanged.
* Provider-neutral cache-key isolation.
"""
from __future__ import annotations

import pytest

from app.services.cloud.azure.adapter import AzureCloudProvider
from app.services.cloud.base import (
    CloudCapability,
    CloudCapabilityState,
)
from app.services.cloud.models import CloudProviderKind


@pytest.fixture
def azure_adapter():
    return AzureCloudProvider()


@pytest.fixture
def azure_runtime_configured(monkeypatch):
    from app.services.cloud.azure import credential
    monkeypatch.setattr(credential, "is_configured", lambda: True)
    yield


class TestAzureResourcesCapabilities:
    def test_resources_supported_when_configured(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.RESOURCES] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_resources_detail_carries_api_version(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        detail = status.details[CloudCapability.RESOURCES]
        assert detail["api_version"] == "2024-04-01"
        assert detail["source"] == "Microsoft Resource Graph Query API"
        assert "id" in detail["default_columns"]
        assert "tags" in detail["default_columns"]

    def test_optimization_remains_unsupported(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.OPTIMIZATION] == (
            CloudCapabilityState.UNSUPPORTED
        )

    def test_advisor_remains_unsupported(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.ADVISOR] == (
            CloudCapabilityState.UNSUPPORTED
        )

    def test_costs_still_supported(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.COSTS] == (
            CloudCapabilityState.SUPPORTED
        )


class TestAzureAdapterResourceMethods:
    def test_get_resources_no_longer_stub(self, azure_adapter):
        import inspect
        source = inspect.getsource(azure_adapter.get_resources)
        assert "NotImplementedError" not in source

    def test_get_resource_summary_no_longer_stub(self, azure_adapter):
        import inspect
        source = inspect.getsource(azure_adapter.get_resource_summary)
        assert "NotImplementedError" not in source

    def test_get_recommendations_still_stub(self, azure_adapter):
        import inspect
        source = inspect.getsource(azure_adapter.get_recommendations)
        assert "NotImplementedError" in source


class TestAwsRegression:
    def test_aws_resources_still_supported(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider
        provider = AWSCloudProvider()
        status = provider.get_capabilities()
        assert status.capabilities[CloudCapability.RESOURCES] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_aws_costs_still_supported(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider
        provider = AWSCloudProvider()
        status = provider.get_capabilities()
        assert status.capabilities[CloudCapability.COSTS] == (
            CloudCapabilityState.SUPPORTED
        )


class TestCacheKeyIsolation:
    def test_provider_segment_keeps_aws_and_azure_apart(self):
        from app.api.cloud_resources import _build_cache_key
        aws = _build_cache_key(user_id=1, provider="AWS", scope="all")
        azure = _build_cache_key(user_id=1, provider="AZURE", scope="all")
        assert aws != azure

    def test_sub_scope_distinct_from_all_scope(self):
        from app.api.cloud_resources import _build_cache_key
        all_az = _build_cache_key(user_id=1, provider="AZURE", scope="all")
        sub_az = _build_cache_key(user_id=1, provider="AZURE", scope="sub:abc")
        assert all_az != sub_az

    def test_user_isolation(self):
        from app.api.cloud_resources import _build_cache_key
        u1 = _build_cache_key(user_id=1, provider="AZURE", scope="all")
        u2 = _build_cache_key(user_id=2, provider="AZURE", scope="all")
        assert u1 != u2

    def test_cache_key_pinned_length(self):
        from app.api.cloud_resources import _build_cache_key
        k = _build_cache_key(user_id=1, provider="AZURE", scope="all")
        assert len(k) == 48
        int(k, 16)
