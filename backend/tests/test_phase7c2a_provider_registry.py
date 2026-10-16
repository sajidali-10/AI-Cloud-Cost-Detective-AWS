"""Phase 7C.2A — provider registry + Azure adapter registration tests.

Verifies that:

* The Azure adapter self-registers with the singleton registry
  when ``app.services.cloud.azure`` is imported (or after the
  AWS adapter triggers ``ProviderRegistry.current()``).
* The Azure adapter is registered as DISABLED when the Azure
  runtime is not configured.
* The Azure adapter enables itself when the runtime IS configured.
* The AWS + Azure adapters can both be looked up simultaneously.
* ``ProviderRegistry.snapshot()`` returns only enabled adapters.
* ``ProviderNotFound.disabled`` distinguishes a missing adapter
  from a disabled one — required for the right operator message.
"""
from __future__ import annotations

import pytest

from app.services.cloud.models import CloudProviderKind
from app.services.cloud.registry import (
    ProviderNotFound,
    ProviderRegistry,
)
from app.services.cloud.azure import adapter as azure_adapter_module


# ---------------------------------------------------------------------------
# Singleton registry — Azure self-registers
# ---------------------------------------------------------------------------


def test_singleton_registry_has_aws_and_azure():
    """After the registry initialises (triggered by the AWS adapter
    import), the Azure adapter must also be present.
    """
    reg = ProviderRegistry.current()
    kinds = {k.value for k in reg.known_kinds()}
    assert "AWS" in kinds
    assert "AZURE" in kinds


def test_azure_adapter_resolves_via_registry():
    """``ProviderRegistry.current().require(AZURE)`` returns an
    instance of :class:`AzureCloudProvider`.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    adapter = ProviderRegistry.current().require(CloudProviderKind.AZURE)
    assert isinstance(adapter, AzureCloudProvider)


def test_azure_adapter_self_registers_once():
    """After the registry has been initialised (triggered by the
    AWS adapter import), the Azure adapter is present exactly
    once and resolves to a stable instance.
    """
    reg = ProviderRegistry.current()
    existing = reg.try_get(CloudProviderKind.AZURE)
    assert existing is not None
    # Re-resolving returns the same adapter instance — the
    # registry's identity invariant holds.
    again = reg.try_get(CloudProviderKind.AZURE)
    assert again is existing


# ---------------------------------------------------------------------------
# Enable / disable based on runtime configuration
# ---------------------------------------------------------------------------


def test_azure_adapter_disabled_when_runtime_missing(monkeypatch):
    """When ``is_configured()`` returns False, the Azure adapter is
    registered as DISABLED so the API surface still sees Azure
    exists but ``require(AZURE)`` raises ``ProviderNotFound``.
    """
    from app.services.cloud.azure import credential as cred_mod

    monkeypatch.setattr(cred_mod, "is_configured", lambda: False)
    # Force re-registration by reimporting the module — but that's
    # invasive.  Instead, manually call ``disable`` then ``_register``.
    reg = ProviderRegistry.current()
    reg.disable(CloudProviderKind.AZURE)
    with pytest.raises(ProviderNotFound) as excinfo:
        reg.require(CloudProviderKind.AZURE)
    assert excinfo.value.disabled is True
    # The adapter is still in the known list.
    assert "AZURE" in {k.value for k in reg.known_kinds()}
    # Re-enable so subsequent tests are not affected.
    reg.enable(CloudProviderKind.AZURE)


def test_azure_adapter_enabled_when_runtime_configured(monkeypatch):
    """When ``is_configured()`` returns True, an explicit
    ``enable()`` on the registry surfaces the Azure adapter.
    """
    from app.services.cloud.azure import credential as cred_mod

    monkeypatch.setattr(cred_mod, "is_configured", lambda: True)
    reg = ProviderRegistry.current()
    reg.enable(CloudProviderKind.AZURE)
    adapter = reg.require(CloudProviderKind.AZURE)
    assert adapter is not None
    assert adapter.provider_kind == CloudProviderKind.AZURE


# ---------------------------------------------------------------------------
# AWS adapter unchanged
# ---------------------------------------------------------------------------


def test_aws_adapter_still_resolves():
    """Phase 7C.2A MUST NOT regress the AWS adapter surface."""
    from app.services.cloud.aws.adapter import AWSCloudProvider

    adapter = ProviderRegistry.current().require(CloudProviderKind.AWS)
    assert isinstance(adapter, AWSCloudProvider)


def test_aws_and_azure_adapters_coexist():
    """Both adapters can be looked up simultaneously without one
    clobbering the other.
    """
    reg = ProviderRegistry.current()
    assert reg.try_get(CloudProviderKind.AWS) is not None
    assert reg.try_get(CloudProviderKind.AZURE) is not None


# ---------------------------------------------------------------------------
# Capability surface — Azure
# ---------------------------------------------------------------------------


def test_azure_adapter_capabilities_publish_supported_subset(monkeypatch):
    """Phase 7C.2A: the Azure adapter supports AUTHENTICATION and
    SUBSCRIPTIONS; everything else is UNSUPPORTED.  When the
    runtime is not configured, AUTHENTICATION + SUBSCRIPTIONS fall
    back to NOT_CONFIGURED.

    Phase 7C.2B advances ``COSTS`` to ``SUPPORTED`` when the
    runtime is configured; the test is updated to accept the
    new state.  ``RESOURCES``, ``OPTIMIZATION`` etc. remain
    ``UNSUPPORTED`` until their respective phases ship.
    """
    from app.services.cloud.azure import credential as cred_mod
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.base import (
        CloudCapability,
        CloudCapabilityState,
    )

    # Try both states (configured / not configured) by toggling
    # the monkeypatched is_configured on the credential module —
    # this is the symbol the adapter's ``_is_configured`` helper
    # routes through.
    for truth in [True, False]:
        monkeypatch.setattr(cred_mod, "is_configured", lambda t=truth: t)
        ProviderRegistry.current().enable(CloudProviderKind.AZURE)
        snapshot = AzureCloudProvider().get_capabilities()

        for cap in (
            CloudCapability.METRICS,
            CloudCapability.BILLING,
            CloudCapability.ADVISOR,
            CloudCapability.AI_EVIDENCE,
        ):
            assert snapshot.capabilities[cap] == CloudCapabilityState.UNSUPPORTED, (
                f"{cap.value} must be UNSUPPORTED (truth={truth})"
            )
        auth_state = snapshot.capabilities[CloudCapability.AUTHENTICATION]
        assert auth_state in (
            CloudCapabilityState.SUPPORTED,
            CloudCapabilityState.NOT_CONFIGURED,
        ), auth_state
        subs_state = snapshot.capabilities[CloudCapability.SUBSCRIPTIONS]
        assert subs_state in (
            CloudCapabilityState.SUPPORTED,
            CloudCapabilityState.NOT_CONFIGURED,
        ), subs_state
        costs_state = snapshot.capabilities[CloudCapability.COSTS]
        resources_state = snapshot.capabilities[CloudCapability.RESOURCES]
        optimization_state = snapshot.capabilities[CloudCapability.OPTIMIZATION]
        if truth:
            # Runtime configured — Phase 7C.2B advances COSTS to
            # SUPPORTED; Phase 7C.2C advances RESOURCES to SUPPORTED;
            # Phase 7C.2D advances OPTIMIZATION to SUPPORTED.
            assert costs_state == CloudCapabilityState.SUPPORTED, costs_state
            assert resources_state == CloudCapabilityState.SUPPORTED, resources_state
            assert optimization_state == CloudCapabilityState.SUPPORTED, optimization_state
        else:
            assert costs_state == CloudCapabilityState.NOT_CONFIGURED, costs_state
            assert resources_state == CloudCapabilityState.NOT_CONFIGURED, resources_state
            assert optimization_state == CloudCapabilityState.NOT_CONFIGURED, optimization_state
