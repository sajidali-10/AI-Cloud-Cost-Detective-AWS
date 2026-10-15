"""Phase 7C.1 — provider-neutral route tests.

Hits the live ``/api/cloud/*`` HTTP surface via ``TestClient`` and
asserts the provider-neutral envelope.  Tests use anonymous access
(the conftest defaults ``AUTH_ENABLED=false``) because the routes
require a valid role; the auth-disabled shortcut still routes
through ``require_role`` which short-circuits to a synthetic
viewer when auth is disabled.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_list_cloud_providers_includes_aws_and_azure(client):
    r = client.get("/cloud/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["providers"]) >= 3
    by_kind = {p["provider"]: p for p in body["providers"]}
    assert by_kind["AWS"]["is_registered"] is True
    assert by_kind["AWS"]["is_enabled"] is True
    assert "AWS_INSTANCE_PROFILE" in by_kind["AWS"]["credential_strategies"]
    # Phase 7C.2A — Azure is now a registered provider (the
    # adapter is wired in and self-registers at process boot).
    # Whether it is *enabled* in this particular deployment is
    # runtime-config dependent (the credential layer disables
    # itself when AZURE_TENANT_ID / AZURE_CLIENT_ID are missing).
    # We pin the registration invariant only; the API surface
    # reports ``is_enabled`` truthfully so the admin UI can show
    # "configured / not configured" instead of "reserved".
    assert by_kind["AZURE"]["is_registered"] is True
    assert by_kind["AZURE"]["status"] in ("OK", "NO_CAPABILITIES", "ERROR")
    # RACKSPACE remains a reserved provider (Phase 7C.3).
    assert by_kind["RACKSPACE"]["is_registered"] is False
    assert by_kind["RACKSPACE"]["status"] == "NOT_REGISTERED"


def test_list_cloud_providers_aws_capabilities_include_supported(client):
    r = client.get("/cloud/providers")
    assert r.status_code == 200
    aws = next(
        p for p in r.json()["providers"] if p["provider"] == "AWS"
    )
    states = {item["capability"]: item["state"] for item in aws["capabilities"]}
    assert states["COSTS"] == "SUPPORTED"
    assert states["RESOURCES"] == "SUPPORTED"
    assert states["OPTIMIZATION"] == "SUPPORTED"
    assert states["METRICS"] == "SUPPORTED"
    assert states["AI_EVIDENCE"] == "SUPPORTED"
    assert states["BILLING"] == "UNSUPPORTED"
    assert states["ADVISOR"] == "UNSUPPORTED"
    # AWS regions are surfaced.
    region_ids = {r["region_id"] for r in aws["supported_regions"]}
    assert "us-east-1" in region_ids


def test_list_cloud_accounts_selectable_includes_provider(client):
    r = client.get("/cloud/accounts/selectable")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "count" in body
    # The Phase 7B management row is provisioned by ``bootstrap_once``
    # during lifespan startup; Phase 7C.2A additionally inserts any
    # configured Azure subscriptions from ``AZURE_SUBSCRIPTION_<N>_*``.
    # The DB state therefore varies by deployment — we pin the
    # response SHAPE, not the row content.
    assert body["count"] == len(body["items"])
    for item in body["items"]:
        # Every item carries a stable ``provider`` field that the
        # frontend uses to route requests through the right adapter.
        assert "provider" in item
        assert item["provider"] in {"AWS", "AZURE", "RACKSPACE"}


def test_selectable_for_unknown_provider_returns_404(client):
    r = client.get("/cloud/accounts/selectable/BOGUS")
    assert r.status_code == 404
    assert r.json()["error_code"] == "UnknownProvider"


def test_selectable_for_azure_returns_200_when_adapter_registered(client):
    """Phase 7C.2A — the Azure adapter IS registered at process
    boot.  The ``/cloud/accounts/selectable/AZURE`` route therefore
    returns 200 with the canonical selector payload.  The number
    of items depends on how many Azure subscriptions the
    deployment has bootstrapped from
    ``AZURE_SUBSCRIPTION_<N>_*`` env vars — the contract under test
    here is the response shape, not the row count.
    """
    r = client.get("/cloud/accounts/selectable/AZURE")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body and "count" in body
    # Every item must be an Azure row; AWS rows MUST NOT leak into
    # the Azure selector (otherwise the frontend could route an
    # AWS request through the Azure adapter).
    for item in body["items"]:
        assert item["provider"] == "AZURE"
    # ``count`` MUST equal ``len(items)`` — the canonical contract.
    assert body["count"] == len(body["items"])


def test_selectable_for_rackspace_returns_404_until_adapter_registered(client):
    """RACKSPACE is reserved for Phase 7C.3 — the adapter is not
    wired yet.  The route must explicitly 404 (NOT_REGISTERED) so
    the frontend can render a meaningful state rather than 500.
    """
    r = client.get("/cloud/accounts/selectable/RACKSPACE")
    assert r.status_code == 404
    assert r.json()["error_code"] == "ProviderNotRegistered"


def test_selectable_for_aws_case_insensitive(client):
    r = client.get("/cloud/accounts/selectable/aws")
    assert r.status_code == 200
    assert "items" in r.json()
