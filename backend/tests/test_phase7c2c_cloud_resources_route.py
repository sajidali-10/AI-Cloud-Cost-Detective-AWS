"""Phase 7C.2C — provider-neutral Cloud resource route tests.

Exercises the canonical ``/api/cloud/resources`` endpoint via
FastAPI's TestClient.  Stubs the provider adapter so no real
network call is made; assertions focus on envelope shape,
selection, and error mapping.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, List
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db.models import AppUser
from app.main import app
from app.services.cloud import ProviderRegistry
from app.services.cloud.models import (
    CloudProviderKind,
    CloudResource,
    CloudResourceSummary,
)


def _summary(
    *,
    status: str = "SUCCESS",
    total_resources: int = 2,
    by_type: List[dict] = None,
    by_location: List[dict] = None,
    by_resource_group: List[dict] = None,
    by_subscription: List[dict] = None,
    subscription_errors: List[dict] = None,
):
    return CloudResourceSummary(
        provider=CloudProviderKind.AZURE,
        account_id=None,
        scope="all",
        region=None,
        services={},
        totals_by_account=[],
        extensions={
            "status": status,
            "total_resources": total_resources,
            "by_type": by_type or [],
            "by_location": by_location or [],
            "by_resource_group": by_resource_group or [],
            "by_subscription": by_subscription or [],
            "subscription_errors": subscription_errors or [],
        },
    )


def _resource(resource_id: str, *, sub: str = "sub-aaaa", region: str = "eastus", rt: str = "x", rg: str = "rg1"):
    return CloudResource(
        provider=CloudProviderKind.AZURE,
        account_id=sub,
        region=region,
        resource_type=rt,
        resource_id=resource_id,
        service=rt,
        name=resource_id.rsplit("/", 1)[-1],
        state=None,
        composite_id=f"AZURE|{resource_id}",
        metadata={"resource_group": rg},
    )


@pytest.fixture
def auth_user(monkeypatch):
    user = SimpleNamespace(id=1, email="alice@example.com", role="ADMIN")

    async def _stub():
        return user

    from app.api import deps
    monkeypatch.setattr(deps, "get_current_user", _stub)
    return user


@pytest.fixture
def azure_adapter_stub(monkeypatch):
    """Stub the Azure resource aggregator the route uses directly.

    The canonical /api/cloud/resources route imports
    ``aggregate_resources_for_subscriptions`` directly for AZURE
    (not via the adapter) so we stub at the route's module
    namespace — the function is bound there via
    ``from ... import aggregate_resources_for_subscriptions``.
    """
    from app.api import cloud_resources as cloud_resources_module

    original = cloud_resources_module.aggregate_resources_for_subscriptions
    summary_log: list = []
    failures_log: list = []

    async def fake_aggregate(rows, **_kwargs):
        summary_log.append(rows)
        return _summary(), [_resource("/r/1"), _resource("/r/2")], []

    cloud_resources_module.aggregate_resources_for_subscriptions = fake_aggregate

    yield {
        "summary_log": summary_log,
        "failures_log": failures_log,
    }

    cloud_resources_module.aggregate_resources_for_subscriptions = original


@pytest.fixture
def cloud_rows(monkeypatch):
    from app.api import cloud_resources as cloud_resources_module

    class _Row:
        def __init__(self, account_id, display_name):
            self.account_id = account_id
            self.display_name = display_name
            self.provider = "AZURE"
            self.enabled = True

    rows = [
        _Row("a54d076b-2650-4b4f-a28c-2deed2011f83", "MCPP Subscription"),
        _Row("9353d858-27fa-4ce7-bf87-782f3323d297", "Microsoft Azure Sponsorship"),
    ]

    monkeypatch.setattr(
        cloud_resources_module, "iter_enabled_accounts",
        lambda db, provider="AWS": rows,
    )
    return rows


@pytest.fixture
def client():
    return TestClient(app)


class TestCloudResourcesRoute:
    def test_azure_all_subscriptions(
        self, client, auth_user, azure_adapter_stub, cloud_rows
    ):
        response = client.get(
            "/cloud/resources",
            params={"provider": "AZURE"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary"]["provider"] == "AZURE"
        assert body["summary"]["scope"] == "all"
        assert body["summary"]["scope_kind"] == "all"
        assert body["summary"]["status"] == "SUCCESS"
        assert body["summary"]["total_resources"] == 2
        assert len(body["resources"]) == 2

    def test_unknown_provider_returns_400(self, client, auth_user):
        response = client.get(
            "/cloud/resources",
            params={"provider": "NOPE"},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "UnknownProvider"

    def test_invalid_subscription_returns_404(self, client, auth_user, azure_adapter_stub, cloud_rows):
        response = client.get(
            "/cloud/resources",
            params={"provider": "AZURE", "account_id": "sub:nonexistent"},
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "AccountNotFound"

    def test_specific_subscription_uses_sub_scope(
        self, client, auth_user, azure_adapter_stub, cloud_rows, monkeypatch
    ):
        from app.api import cloud_resources as cloud_resources_module
        row = cloud_rows[0]

        # The route resolves a specific subscription by GUID directly.
        response = client.get(
            "/cloud/resources",
            params={
                "provider": "AZURE",
                "account_id": "sub:" + row.account_id,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["summary"]["scope_kind"] == "single"
        assert body["summary"]["scope"] == f"sub:{row.account_id}"

    def test_auth_failed_maps_to_401(
        self, client, auth_user, azure_adapter_stub, cloud_rows, monkeypatch
    ):
        from app.api import cloud_resources as cloud_resources_module
        from app.services.cloud.azure.resource_graph import (
            AzureResourceGraphError,
            AzureResourceGraphErrorCategory,
        )

        async def raise_auth(rows, **_kwargs):
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.AUTH_FAILED,
                subscription_id="sub1",
                status_code=401,
            )
        monkeypatch.setattr(
            cloud_resources_module, "aggregate_resources_for_subscriptions", raise_auth
        )

        response = client.get(
            "/cloud/resources",
            params={"provider": "AZURE"},
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTH_FAILED"

    def test_throttled_maps_to_429_with_retry_after(
        self, client, auth_user, azure_adapter_stub, cloud_rows, monkeypatch
    ):
        from app.api import cloud_resources as cloud_resources_module
        from app.services.cloud.azure.resource_graph import (
            AzureResourceGraphError,
            AzureResourceGraphErrorCategory,
        )

        async def raise_throttled(rows, **_kwargs):
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.THROTTLED,
                subscription_id="sub1",
                status_code=429,
                retry_after=15,
            )
        monkeypatch.setattr(
            cloud_resources_module, "aggregate_resources_for_subscriptions", raise_throttled
        )

        response = client.get(
            "/cloud/resources",
            params={"provider": "AZURE"},
        )
        assert response.status_code == 429
        assert response.headers.get("Retry-After") == "15"

    def test_upstream_error_maps_to_502(
        self, client, auth_user, azure_adapter_stub, cloud_rows, monkeypatch
    ):
        from app.api import cloud_resources as cloud_resources_module
        from app.services.cloud.azure.resource_graph import (
            AzureResourceGraphError,
            AzureResourceGraphErrorCategory,
        )

        async def raise_upstream(rows, **_kwargs):
            raise AzureResourceGraphError(
                category=AzureResourceGraphErrorCategory.UPSTREAM_ERROR,
                subscription_id="sub1",
                status_code=503,
            )
        monkeypatch.setattr(
            cloud_resources_module, "aggregate_resources_for_subscriptions", raise_upstream
        )

        response = client.get(
            "/cloud/resources",
            params={"provider": "AZURE"},
        )
        assert response.status_code == 502


class TestAwsRegression:
    """Confirm Phase 7C.2C did NOT touch the AWS legacy routes."""

    def test_aws_resources_endpoint_still_resolves(self):
        from app.api import aws
        # The Phase 7B /api/aws/resources and /api/aws/resources/all
        # routes are unchanged.
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/aws/resources" in paths
        assert "/aws/resources/all" in paths
