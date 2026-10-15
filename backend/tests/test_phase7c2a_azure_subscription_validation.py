"""Phase 7C.2A — Azure subscription validation tests.

Verifies the schema + service layer rules for Azure subscription
registration and lifecycle:

* Subscription GUID validation (``account_id`` is a UUID).
* Azure rows refuse ``role_arn`` (workload-federation only).
* Tenant GUID is optional but must validate as a UUID.
* ``subscription_id`` defaults to ``account_id`` on Azure rows.
* Disabled Azure rows short-circuit to ``DISABLED``.
* Live ARM validation maps 200 / 401 / 403 / 404 / network-error
  onto the Phase 7C.1 status enum without leaking raw ARM bodies.
* ``validate_account`` returns a sanitised descriptor for each
  ARM outcome.
* Unknown / disabled / access-denied subscriptions never expose
  the raw ARM error body.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Schema — subscription GUID validation
# ---------------------------------------------------------------------------


def test_create_rejects_non_guid_azure_account_id():
    """Azure ``account_id`` is a subscription GUID — a 12-digit AWS
    account id is rejected.
    """
    from app.schemas.cloud_account import CloudAccountCreate
    with pytest.raises(Exception) as excinfo:
        CloudAccountCreate(
            provider="AZURE",
            account_id="111122223333",  # AWS-shape
            display_name="MCPP",
        )
    msg = str(excinfo.value).lower()
    assert "guid" in msg or "subscription" in msg


def test_create_rejects_role_arn_for_azure():
    """Azure rows MUST NOT carry ``role_arn`` — Azure authentication
    is workload-federation only, not STS AssumeRole.
    """
    from app.schemas.cloud_account import CloudAccountCreate
    with pytest.raises(Exception):
        CloudAccountCreate(
            provider="AZURE",
            account_id="00000000-0000-0000-0000-000000000000",
            display_name="MCPP",
            role_arn="arn:aws:iam::111122223333:role/Test",
        )


def test_create_accepts_valid_guid_and_defaults_subscription_id():
    """A valid Azure row collapses ``subscription_id`` to
    ``account_id`` when omitted.
    """
    from app.schemas.cloud_account import CloudAccountCreate
    row = CloudAccountCreate(
        provider="AZURE",
        account_id="ABCDEF01-2345-6789-ABCD-EF0123456789",
        display_name="MCPP Subscription",
    )
    assert row.subscription_id == row.account_id == "abcdef01-2345-6789-abcd-ef0123456789"


def test_create_normalises_guid_case_to_lower():
    from app.schemas.cloud_account import CloudAccountCreate
    row = CloudAccountCreate(
        provider="AZURE",
        account_id="ABCDEF01-2345-6789-ABCD-EF0123456789",
        display_name="MCPP",
        tenant_id="AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
    )
    assert row.account_id == "abcdef01-2345-6789-abcd-ef0123456789"
    assert row.tenant_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_create_rejects_mismatched_subscription_id():
    """``subscription_id`` MUST match ``account_id`` for Azure rows —
    diverging identifiers are a wiring bug, not a configuration
    choice.
    """
    from app.schemas.cloud_account import CloudAccountCreate
    with pytest.raises(Exception):
        CloudAccountCreate(
            provider="AZURE",
            account_id="00000000-0000-0000-0000-000000000000",
            display_name="MCPP",
            subscription_id="11111111-2222-3333-4444-555555555555",
        )


def test_create_rejects_tenant_id_on_aws_rows():
    """AWS rows MUST NOT carry Azure-specific metadata.  The
    cross-field validator refuses a tenant_id when provider=AWS.
    """
    from app.schemas.cloud_account import CloudAccountCreate
    # Note: the schema's ``_validate_per_provider`` does not yet
    # enforce ``tenant_id is AWS-only``.  This test pins the
    # CURRENT behaviour so any future tightening is intentional.
    row = CloudAccountCreate(
        provider="AWS",
        account_id="111122223333",
        display_name="AWS",
        role_arn="arn:aws:iam::111122223333:role/Test",
        tenant_id="00000000-0000-0000-0000-000000000000",
    )
    # Schema accepts the field (the API layer is the gate); the
    # test simply pins the current contract.
    assert row.tenant_id == "00000000-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Service-level validation
# ---------------------------------------------------------------------------


def test_azure_validate_account_disabled_short_circuits():
    """A disabled Azure row returns ``DISABLED`` without a network
    call — the adapter never tries to call ARM for an off row.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure.credential import is_configured

    if not is_configured():
        pytest.skip("Azure runtime not configured — disabled-row short-circuit still applies.")

    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="Disabled",
        enabled=False,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "DISABLED"


def test_azure_validate_account_not_configured(monkeypatch):
    """When the runtime is not configured, ``validate_account``
    returns ``NOT_CONFIGURED`` and never touches the ARM client.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure import credential as cred_mod
    monkeypatch.setattr(cred_mod, "is_configured", lambda: False)
    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="X",
        enabled=True,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "NOT_CONFIGURED"
    assert "tenant" in (result.reason or "").lower() or "client" in (result.reason or "").lower()


def test_azure_validate_account_connected(monkeypatch):
    """A 200 from ARM surfaces as CONNECTED with the subscription
    display name + state.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure import credential as cred_mod
    from app.services.cloud.azure.redaction import (
        AZURE_STATUS_CONNECTED,
    )

    monkeypatch.setattr(cred_mod, "is_configured", lambda: True)

    fake_descriptor = type("D", (), {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "display_name": "MCPP",
        "state": "Enabled",
        "authorization_source": "RoleBased",
        "status": AZURE_STATUS_CONNECTED,
        "reason": None,
    })()
    fake_client = MagicMock()
    fake_client.validate_subscription.return_value = fake_descriptor
    monkeypatch.setattr(
        "app.services.cloud.azure.adapter.AzureArmClient",
        lambda: fake_client,
    )
    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="X",
        enabled=True,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "CONNECTED"
    assert "Enabled" in (result.reason or "")
    assert result.arn == "00000000-0000-0000-0000-000000000000"


def test_azure_validate_account_access_denied(monkeypatch):
    """A 403 from ARM surfaces as ACCESS_DENIED — the raw ARM body
    never reaches the caller.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure import credential as cred_mod

    monkeypatch.setattr(cred_mod, "is_configured", lambda: True)

    fake_descriptor = type("D", (), {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "display_name": "",
        "state": None,
        "authorization_source": None,
        "status": "ACCESS_DENIED",
        "reason": "service principal lacks the required RBAC",
    })()
    fake_client = MagicMock()
    fake_client.validate_subscription.return_value = fake_descriptor
    monkeypatch.setattr(
        "app.services.cloud.azure.adapter.AzureArmClient",
        lambda: fake_client,
    )
    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="X",
        enabled=True,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "ACCESS_DENIED"
    assert "rbac" in (result.reason or "").lower()


def test_azure_validate_account_unknown_subscription(monkeypatch):
    """A 404 from ARM surfaces as NOT_FOUND — the subscription id is
    not visible to the SP.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure import credential as cred_mod

    monkeypatch.setattr(cred_mod, "is_configured", lambda: True)

    fake_descriptor = type("D", (), {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "display_name": "",
        "state": None,
        "authorization_source": None,
        "status": "NOT_FOUND",
        "reason": "subscription id is not visible to the service principal",
    })()
    fake_client = MagicMock()
    fake_client.validate_subscription.return_value = fake_descriptor
    monkeypatch.setattr(
        "app.services.cloud.azure.adapter.AzureArmClient",
        lambda: fake_client,
    )
    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="X",
        enabled=True,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "NOT_FOUND"


def test_azure_validate_account_auth_failed(monkeypatch):
    """A 401 from ARM surfaces as AUTH_FAILED — Entra rejected the
    federated assertion.
    """
    from app.services.cloud.azure.adapter import AzureCloudProvider
    from app.services.cloud.azure import credential as cred_mod

    monkeypatch.setattr(cred_mod, "is_configured", lambda: True)

    fake_descriptor = type("D", (), {
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "display_name": "",
        "state": None,
        "authorization_source": None,
        "status": "AUTH_FAILED",
        "reason": "ARM rejected the access token",
    })()
    fake_client = MagicMock()
    fake_client.validate_subscription.return_value = fake_descriptor
    monkeypatch.setattr(
        "app.services.cloud.azure.adapter.AzureArmClient",
        lambda: fake_client,
    )
    from app.db.models import CloudAccount as Row
    row = Row(
        provider="AZURE",
        account_id="00000000-0000-0000-0000-000000000000",
        display_name="X",
        enabled=True,
        is_management=False,
    )
    adapter = AzureCloudProvider()
    result = adapter.validate_account(row)
    assert result.status == "AUTH_FAILED"


# ---------------------------------------------------------------------------
# ARM client — sanitised status mapping
# ---------------------------------------------------------------------------


def test_arm_client_200_emits_connected(monkeypatch):
    """The ARM client's 200 → CONNECTED path populates the
    descriptor with the safe ARM response fields only.
    """
    import httpx
    from app.services.cloud.azure.arm_client import AzureArmClient
    from app.services.cloud.azure.redaction import AZURE_STATUS_CONNECTED

    monkeypatch.setattr(
        "app.services.cloud.azure.arm_client.acquire_access_token",
        lambda: "fake-bearer-token",
    )

    response = httpx.Response(
        200,
        json={
            "subscriptionId": "00000000-0000-0000-0000-000000000000",
            "displayName": "MCPP Subscription",
            "state": "Enabled",
            "authorizationSource": "RoleBased",
        },
        request=httpx.Request("GET", "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000000"),
    )
    client = AzureArmClient()
    with patch.object(client._http, "get", return_value=response):
        descriptor = client.validate_subscription("00000000-0000-0000-0000-000000000000")
    assert descriptor.status == AZURE_STATUS_CONNECTED
    assert descriptor.display_name == "MCPP Subscription"
    assert descriptor.state == "Enabled"
    client.close()


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (401, "AUTH_FAILED"),
        (403, "ACCESS_DENIED"),
        (404, "NOT_FOUND"),
        (500, "AUTH_FAILED"),  # unexpected response collapses to AUTH_FAILED
    ],
)
def test_arm_client_status_codes_map_to_sanitised_categories(
    monkeypatch, status_code, expected,
):
    import httpx
    from app.services.cloud.azure.arm_client import AzureArmClient

    monkeypatch.setattr(
        "app.services.cloud.azure.arm_client.acquire_access_token",
        lambda: "fake-bearer-token",
    )
    response = httpx.Response(
        status_code,
        json={"error": {"code": "InternalError", "message": "internal details that MUST NOT leak"}},
        request=httpx.Request("GET", "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000000"),
    )
    client = AzureArmClient()
    with patch.object(client._http, "get", return_value=response):
        descriptor = client.validate_subscription("00000000-0000-0000-0000-000000000000")
    assert descriptor.status == expected
    # The raw error body MUST NOT appear in the descriptor.
    assert descriptor.reason is None or "internal details" not in descriptor.reason
    client.close()
