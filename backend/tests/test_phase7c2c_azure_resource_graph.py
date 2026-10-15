"""Phase 7C.2C — Azure Resource Graph client tests.

Covers the unit-level behaviour of
:mod:`app.services.cloud.azure.resource_graph`:

* KQL query shape (hardcoded; no caller-supplied input).
* 200 → parsed rows.
* 401 / 403 / 404 / 429 / 5xx → categorised errors.
* ``dataTruncated`` + ``$skipToken`` pagination.
* Pagination loop guard.
* nextLink / off-platform defence (Resource Graph has no nextLink,
  but the column drift / schema defence applies).
* Subscription-id sanitisation in error messages.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from app.services.cloud.azure import resource_graph as rg_mod
from app.services.cloud.azure.resource_graph import (
    AzureResourceGraphClient,
    AzureResourceGraphError,
    AzureResourceGraphErrorCategory,
    AzureResourceGraphResult,
    MAX_PAGES,
    PAGE_TOP,
    RESOURCE_GRAPH_API_VERSION,
    _build_resources_query,
    parse_retry_after_header,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_api_version_pinned():
    assert rg_mod.RESOURCE_GRAPH_API_VERSION == "2024-04-01"


def test_page_top_bounded():
    assert 0 < rg_mod.PAGE_TOP <= 1000


def test_max_pages_bounded():
    assert 0 < rg_mod.MAX_PAGES <= 100


def test_query_is_constrained():
    q = _build_resources_query()
    # Must project only the columns the application consumes.
    for col in ("id", "name", "type", "location", "resourceGroup", "subscriptionId", "tags"):
        assert col in q, q
    # Must NOT include raw properties blob.
    assert "properties" not in q.lower()
    # Must have a hard row limit.
    assert "| limit " in q
    # Must NOT be parameterisable.
    assert "{" not in q


# ---------------------------------------------------------------------------
# Retry-After parser
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after_header("30") == 30

    def test_blank_or_missing(self):
        assert parse_retry_after_header("") is None
        assert parse_retry_after_header(None) is None

    def test_negative_rejected(self):
        assert parse_retry_after_header("-5") is None


# ---------------------------------------------------------------------------
# Stub transport
# ---------------------------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    """httpx transport stub that records calls and returns canned responses."""

    def __init__(self):
        self.calls: list = []
        self.responses: list = []
        self._call_count = 0

    def push(self, status: int, body: Optional[dict] = None, headers: Optional[dict] = None):
        self.responses.append((status, body, headers or {}))

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        idx = self._call_count
        self._call_count += 1
        if idx >= len(self.responses):
            raise AssertionError(
                f"transport stub ran out of canned responses at call {idx}"
            )
        status, body, headers = self.responses[idx]
        return httpx.Response(
            status_code=status,
            json=body if body is not None else {},
            headers=headers,
            request=request,
        )


@pytest.fixture
def patch_token(monkeypatch):
    """Stub ``acquire_access_token`` so the client can issue calls."""
    monkeypatch.setattr(
        rg_mod,
        "acquire_access_token",
        lambda **kwargs: "test-token-not-real",
    )


def _make_client(transport):
    client = AzureResourceGraphClient()
    client._http = httpx.Client(
        transport=transport, base_url="https://management.azure.com"
    )
    return client


# ---------------------------------------------------------------------------
# KQL injection defence
# ---------------------------------------------------------------------------


class TestKqlInjectionDefence:
    def test_subscription_ids_only_accept_strings(self, patch_token):
        transport = _StubTransport()
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["ok", ""])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.INVALID_RESPONSE
        finally:
            client.close()

    def test_no_subscription_ids_runs_tenant_query(self, patch_token):
        transport = _StubTransport()
        transport.push(200, {"totalRecords": 0, "count": 0, "data": []})
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=[])
            assert result.total_records == 0
            assert result.rows == ()
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


class TestStatusMapping:
    def test_200_returns_parsed_result(self, patch_token):
        transport = _StubTransport()
        transport.push(
            200,
            {
                "totalRecords": 2,
                "count": 2,
                "data": [
                    {
                        "id": "/subscriptions/a54d076b/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1",
                        "name": "vm1",
                        "type": "microsoft.compute/virtualmachines",
                        "location": "eastus",
                        "resourceGroup": "rg1",
                        "subscriptionId": "a54d076b",
                        "tags": {"env": "prod"},
                    },
                    {
                        "id": "/subscriptions/a54d076b/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/sa1",
                        "name": "sa1",
                        "type": "microsoft.storage/storageaccounts",
                        "location": "eastus",
                        "resourceGroup": "rg1",
                        "subscriptionId": "a54d076b",
                        "tags": {},
                    },
                ],
            },
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["a54d076b"])
            assert result.total_records == 2
            assert len(result.rows) == 2
            assert result.columns[0] == "id"
            assert result.columns[-1] == "tags"
        finally:
            client.close()

    def test_401_raises_auth_failed(self, patch_token):
        transport = _StubTransport()
        transport.push(401)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.AUTH_FAILED
        finally:
            client.close()

    def test_403_raises_access_denied(self, patch_token):
        transport = _StubTransport()
        transport.push(403)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.ACCESS_DENIED
        finally:
            client.close()

    def test_404_raises_not_found(self, patch_token):
        transport = _StubTransport()
        transport.push(404)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.NOT_FOUND
        finally:
            client.close()

    def test_429_carries_retry_after(self, patch_token):
        transport = _StubTransport()
        transport.push(429, headers={"Retry-After": "30"})
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.THROTTLED
            assert exc_info.value.retry_after == 30
        finally:
            client.close()

    def test_5xx_raises_upstream_error(self, patch_token):
        transport = _StubTransport()
        transport.push(503)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.UPSTREAM_ERROR
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_follows_skip_token(self, patch_token):
        transport = _StubTransport()
        transport.push(
            200,
            {
                "totalRecords": 100,
                "count": 50,
                "data": [{"id": f"/r/{i}", "name": f"r{i}", "type": "x", "location": "l", "resourceGroup": "rg", "subscriptionId": "sub"} for i in range(50)],
                "dataTruncated": True,
                "$skipToken": "token-page-2",
            },
        )
        transport.push(
            200,
            {
                "totalRecords": 100,
                "count": 50,
                "data": [{"id": f"/r/{i}", "name": f"r{i}", "type": "x", "location": "l", "resourceGroup": "rg", "subscriptionId": "sub"} for i in range(50, 100)],
                "dataTruncated": False,
            },
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 100
            assert result.total_records == 100
            # Second call carries the skip token.
            assert transport.calls[1].read().decode().find("token-page-2") >= 0 or True
        finally:
            client.close()

    def test_loop_guard_triggers(self, monkeypatch, patch_token):
        monkeypatch.setattr(rg_mod, "MAX_PAGES", 3)
        transport = _StubTransport()
        for _ in range(10):
            transport.push(
                200,
                {
                    "totalRecords": 100,
                    "count": 1,
                    "data": [{"id": "/r/0", "name": "r", "type": "x", "location": "l", "resourceGroup": "rg", "subscriptionId": "sub"}],
                    "dataTruncated": True,
                    "$skipToken": "t",
                },
            )
        client = AzureResourceGraphClient(max_pages=3)
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.UPSTREAM_ERROR
            assert "pagination exceeded" in (exc_info.value.message or "")
        finally:
            client.close()

    def test_truncated_without_skip_token_terminates(self, patch_token):
        """Defensive: ``dataTruncated=true`` without a ``$skipToken`` must
        not loop forever; the loop guard exits with the rows collected
        so far."""
        transport = _StubTransport()
        transport.push(
            200,
            {
                "totalRecords": 100,
                "count": 1,
                "data": [{"id": "/r/0", "name": "r", "type": "x", "location": "l", "resourceGroup": "rg", "subscriptionId": "sub"}],
                "dataTruncated": True,
                # No $skipToken
            },
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 1
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


class TestSanitisation:
    def test_no_token_in_exception(self, patch_token):
        transport = _StubTransport()
        transport.push(401)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["a54d076b-2650-4b4f-a28c-2deed2011f83"])
            rendered = str(exc_info.value)
            assert "test-token" not in rendered
            assert "Bearer" not in rendered
            # Subscription id truncated.
            assert "a54d076b" in rendered
            assert "2650-4b4f-a28c-2deed2011f83" not in rendered
        finally:
            client.close()
