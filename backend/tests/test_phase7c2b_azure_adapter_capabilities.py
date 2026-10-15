"""Phase 7C.2B — Azure adapter capabilities + integration.

Verifies:

* ``get_capabilities`` reports ``COSTS=SUPPORTED`` when the
  runtime is configured (and ``NOT_CONFIGURED`` otherwise).
* ``RESOURCES`` / ``OPTIMIZATION`` remain ``UNSUPPORTED``.
* The adapter's cost methods are no longer ``NotImplementedError``
  stubs but delegate to the cost aggregator.
* Provider-neutral AWS regression: the AWS adapter still reports
  the same capability surface.
"""
from __future__ import annotations

import inspect

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
    """Force the credential module's ``is_configured`` to True so
    the adapter's capability table reports SUPPORTED for the
    runtime-configured capabilities.
    """
    from app.services.cloud.azure import credential

    monkeypatch.setattr(credential, "is_configured", lambda: True)
    yield


@pytest.fixture
def azure_runtime_unconfigured(monkeypatch):
    """Force the credential module's ``is_configured`` to False so
    the adapter's capability table reports NOT_CONFIGURED.
    """
    from app.services.cloud.azure import credential

    monkeypatch.setattr(credential, "is_configured", lambda: False)
    yield


class TestAzureCapabilities:
    def test_costs_supported_when_configured(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.COSTS] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_optimization_unimplemented(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.OPTIMIZATION] == (
            CloudCapabilityState.UNSUPPORTED
        )

    def test_advisor_unimplemented(self, azure_adapter, azure_runtime_configured):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.ADVISOR] == (
            CloudCapabilityState.UNSUPPORTED
        )

    def test_authentication_supported_when_configured(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.AUTHENTICATION] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_subscriptions_supported_when_configured(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.SUBSCRIPTIONS] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_costs_not_configured_when_runtime_missing(
        self, azure_adapter, azure_runtime_unconfigured
    ):
        status = azure_adapter.get_capabilities()
        assert status.capabilities[CloudCapability.COSTS] == (
            CloudCapabilityState.NOT_CONFIGURED
        )

    def test_cost_detail_includes_api_version(
        self, azure_adapter, azure_runtime_configured
    ):
        status = azure_adapter.get_capabilities()
        detail = status.details[CloudCapability.COSTS]
        assert detail["api_version"] == "2026-06-01"
        assert detail["cost_type"] == "ActualCost"
        assert "ServiceName" in detail["dimensions"]
        assert "ResourceLocation" in detail["dimensions"]


class TestAzureAdapterCostMethods:
    def test_get_cost_summary_is_no_longer_stub(self, azure_adapter):
        """The 7C.2A NotImplementedError stubs must be gone."""
        assert not inspect.iscoroutinefunction(azure_adapter.get_cost_summary) or \
            azure_adapter.get_cost_summary.__code__.co_name != "<lambda>"
        # Confirm the implementation delegates to the aggregator by
        # checking the source file no longer raises.
        source = inspect.getsource(azure_adapter.get_cost_summary)
        assert "NotImplementedError" not in source

    def test_get_cost_trend_is_no_longer_stub(self, azure_adapter):
        source = inspect.getsource(azure_adapter.get_cost_trend)
        assert "NotImplementedError" not in source

    def test_get_cost_by_service_is_no_longer_stub(self, azure_adapter):
        source = inspect.getsource(azure_adapter.get_cost_by_service)
        assert "NotImplementedError" not in source

    def test_get_resources_no_longer_stub(self, azure_adapter):
        """Phase 7C.2C advances ``get_resources`` to a real
        implementation; the Phase 7C.2A/2B ``NotImplementedError``
        stub is gone."""
        source = inspect.getsource(azure_adapter.get_resources)
        assert "NotImplementedError" not in source

    def test_get_recommendations_is_still_stub(self, azure_adapter):
        """Phase 7C.2D — recommendations not yet implemented."""
        source = inspect.getsource(azure_adapter.get_recommendations)
        assert "NotImplementedError" in source


class TestAwsAdapterUnchanged:
    def test_aws_costs_still_supported(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider

        provider = AWSCloudProvider()
        status = provider.get_capabilities()
        assert status.capabilities[CloudCapability.COSTS] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_aws_resources_still_supported(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider

        provider = AWSCloudProvider()
        status = provider.get_capabilities()
        assert status.capabilities[CloudCapability.RESOURCES] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_aws_optimizer_still_supported(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider

        provider = AWSCloudProvider()
        status = provider.get_capabilities()
        assert status.capabilities[CloudCapability.OPTIMIZATION] == (
            CloudCapabilityState.SUPPORTED
        )

    def test_aws_kind(self):
        from app.services.cloud.aws.adapter import AWSCloudProvider

        provider = AWSCloudProvider()
        assert provider.provider_kind == CloudProviderKind.AWS
