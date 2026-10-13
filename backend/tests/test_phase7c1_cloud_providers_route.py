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


def test_list_cloud_providers_includes_aws_and_reserved(client):
    r = client.get("/cloud/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["providers"]) >= 3
    by_kind = {p["provider"]: p for p in body["providers"]}
    assert by_kind["AWS"]["is_registered"] is True
    assert by_kind["AWS"]["is_enabled"] is True
    assert "AWS_INSTANCE_PROFILE" in by_kind["AWS"]["credential_strategies"]
    # Reserved providers must always be reported as not-yet-registered
    # so the frontend can render a "future release" hint.
    for reserved in ("AZURE", "RACKSPACE"):
        assert by_kind[reserved]["is_registered"] is False
        assert by_kind[reserved]["status"] == "NOT_REGISTERED"


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
    # during lifespan startup.  With an empty DB (the default test
    # fixture), no rows exist — but the response shape is stable.
    if body["items"]:
        for item in body["items"]:
            assert "provider" in item
            assert item["provider"] == "AWS"


def test_selectable_for_unknown_provider_returns_404(client):
    r = client.get("/cloud/accounts/selectable/BOGUS")
    assert r.status_code == 404
    assert r.json()["error_code"] == "UnknownProvider"


def test_selectable_for_azure_returns_404_until_adapter_registered(client):
    """Azure is reserved in the enum but not registered.  The route
    must explicitly 404 (NOT_REGISTERED) so the frontend can render
    a meaningful state rather than 500."""
    r = client.get("/cloud/accounts/selectable/AZURE")
    assert r.status_code == 404
    assert r.json()["error_code"] == "ProviderNotRegistered"


def test_selectable_for_aws_case_insensitive(client):
    r = client.get("/cloud/accounts/selectable/aws")
    assert r.status_code == 200
    assert "items" in r.json()
