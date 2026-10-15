"""Phase 7C.2C.1 — Azure Resource Graph client tests.

Covers the corrected pagination contract:

* KQL contains NO ``| limit`` / ``| take`` / ``| sample`` operator
  (Microsoft Resource Graph treats these as final and disables
  ``$skipToken`` continuation).
* Page size is controlled via ``options.$top``.
* Truncation is detected via ``resultTruncated=true`` (the
  documented response field).
* Multi-page continuation preserves the same query + same
  subscriptions + same ``$top`` + ``$skipToken`` on every page.
* Pathological responses surface as
  :class:`AzureResourceGraphError`, never silently SUCCESS.
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
    """``$top`` must be > 0 and <= Microsoft's documented maximum (1000)."""
    assert 0 < rg_mod.PAGE_TOP <= 1000


def test_max_pages_bounded():
    assert 0 < rg_mod.MAX_PAGES <= 100


# ---------------------------------------------------------------------------
# KQL contract — the most important guard in this file.
# ---------------------------------------------------------------------------


def test_query_contains_no_limit_take_sample():
    """Microsoft Resource Graph treats ``| limit`` / ``| take`` /
    ``| sample`` as final operators that disable ``$skipToken``
    continuation.  The KQL must NOT contain them."""
    q = _build_resources_query()
    for forbidden in ("| limit ", "| take ", "| sample "):
        assert forbidden.lower() not in q.lower(), (
            f"KQL must not contain {forbidden!r} (breaks $skipToken pagination)"
        )


def test_query_is_constrained():
    q = _build_resources_query()
    # Must project only the columns the application needs.
    for col in ("id", "name", "type", "location", "resourceGroup", "subscriptionId", "tags"):
        assert col in q, q
    # Must NOT include raw properties blob.
    assert "properties" not in q.lower()
    # Must NOT be parameterisable.
    assert "{" not in q


def test_query_starts_with_resources():
    """The KQL must target the ``Resources`` table."""
    assert _build_resources_query().strip().startswith("Resources")


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


def _make_client(transport, *, page_top: int = PAGE_TOP, max_pages: int = MAX_PAGES):
    client = AzureResourceGraphClient(page_top=page_top, max_pages=max_pages)
    client._http = httpx.Client(
        transport=transport, base_url="https://management.azure.com"
    )
    return client


def _row(i, *, sub="sub-aaaa", rg="rg1", loc="eastus", rt="x"):
    return {
        "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.X/r{i}",
        "name": f"r{i}",
        "type": rt,
        "location": loc,
        "resourceGroup": rg,
        "subscriptionId": sub,
        "tags": {},
    }


def _page_body(rows, *, truncated=False, skip_token=None, total_records=None):
    """Build a Resource Graph response body.

    ``total_records`` overrides the default (``len(rows)`` when the
    response is complete, or ``len(rows) + 1000`` to simulate a
    multi-page workload).
    """
    if total_records is None:
        total_records = len(rows) if not truncated else len(rows) + 1000
    body = {
        "totalRecords": total_records,
        "count": len(rows),
        "data": rows,
    }
    if truncated:
        body["resultTruncated"] = True
    if skip_token is not None:
        body["$skipToken"] = skip_token
    return body


# ---------------------------------------------------------------------------
# Request shape — page size via $top, not KQL limit.
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_top_request_option_used(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([_row(1)]))
        client = _make_client(transport)
        try:
            client.query(subscription_ids=["sub-aaaa"])
            body = json.loads(transport.calls[0].content.decode("utf-8"))
            assert body["options"]["$top"] == PAGE_TOP
        finally:
            client.close()

    def test_top_request_option_override(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([_row(1)]))
        client = _make_client(transport, page_top=500)
        try:
            client.query(subscription_ids=["sub-aaaa"])
            body = json.loads(transport.calls[0].content.decode("utf-8"))
            assert body["options"]["$top"] == 500
        finally:
            client.close()

    def test_subscriptions_preserved_across_pages(self, patch_token):
        """Every page must carry the same ``subscriptions`` array as the
        first request — Resource Graph's skipToken continuation is
        scoped per query."""
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(
                [_row(i) for i in range(3)],
                truncated=True,
                skip_token="cursor-page-2",
            ),
        )
        transport.push(200, _page_body([_row(3), _row(4), _row(5)]))
        client = _make_client(transport)
        try:
            client.query(subscription_ids=["sub-aaaa", "sub-bbbb"])
            assert len(transport.calls) == 2
            body1 = json.loads(transport.calls[0].content.decode("utf-8"))
            body2 = json.loads(transport.calls[1].content.decode("utf-8"))
            assert body1["subscriptions"] == ["sub-aaaa", "sub-bbbb"]
            assert body2["subscriptions"] == ["sub-aaaa", "sub-bbbb"]
            # Same query — pagination must NOT mutate the query body.
            assert body1["query"] == body2["query"]
            # Same $top — pagination must NOT mutate the page size.
            assert body1["options"]["$top"] == body2["options"]["$top"]
        finally:
            client.close()

    def test_query_body_excludes_limit_take_sample(self, patch_token):
        transport = _StubTransport()
        transport.push(200, _page_body([_row(1)]))
        client = _make_client(transport)
        try:
            client.query(subscription_ids=["sub-aaaa"])
            body = json.loads(transport.calls[0].content.decode("utf-8"))
            assert "| limit " not in body["query"].lower()
            assert "| take " not in body["query"].lower()
            assert "| sample " not in body["query"].lower()
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Pagination — happy path
# ---------------------------------------------------------------------------


class TestPagination:
    def test_first_page_under_1000_no_token(self, patch_token):
        """First page <1000 rows, no truncation, no continuation token."""
        transport = _StubTransport()
        rows = [_row(i) for i in range(5)]
        transport.push(200, _page_body(rows))
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 5
            assert result.total_records == 5
            assert result.pages_consumed == 1
            assert len(transport.calls) == 1
        finally:
            client.close()

    def test_first_page_with_skip_token(self, patch_token):
        """First page truncated; client must request the next page with
        the same query + same $top + the $skipToken."""
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(
                [_row(i) for i in range(3)],
                truncated=True,
                skip_token="cursor-2",
            ),
        )
        transport.push(200, _page_body([_row(3), _row(4)]))
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 5
            assert result.pages_consumed == 2
            # Second request carries the skipToken in options.
            body2 = json.loads(transport.calls[1].content.decode("utf-8"))
            assert body2["options"]["$skipToken"] == "cursor-2"
            assert body2["options"]["$top"] == PAGE_TOP
            assert body2["query"] == _build_resources_query()
        finally:
            client.close()

    def test_multiple_pages_concatenated(self, patch_token):
        transport = _StubTransport()
        for page_idx in range(4):
            transport.push(
                200,
                _page_body(
                    [_row(page_idx * 5 + i) for i in range(5)],
                    truncated=page_idx < 3,
                    skip_token=f"cursor-{page_idx + 2}" if page_idx < 3 else None,
                    total_records=20,
                ),
            )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 20
            assert result.pages_consumed == 4
            assert result.total_records == 20
        finally:
            client.close()

    def test_resources_from_all_pages_preserved(self, patch_token):
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(
                [_row(0), _row(1)],
                truncated=True,
                skip_token="t-2",
            ),
        )
        transport.push(
            200,
            _page_body(
                [_row(2), _row(3), _row(4)],
                truncated=True,
                skip_token="t-3",
            ),
        )
        transport.push(200, _page_body([_row(5), _row(6), _row(7), _row(8), _row(9)]))
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            ids = [row["id"] for row in result.rows]
            assert ids == [f"/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.X/r{i}" for i in range(10)]
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Pagination — pathological / defensive
# ---------------------------------------------------------------------------


class TestPaginationDefensive:
    def test_truncated_without_skip_token_raises_pagination_incomplete(self, patch_token):
        """``resultTruncated=true`` without a ``$skipToken`` is
        pathological: Resource Graph told us the data is incomplete
        but did not give us a way to fetch the rest.  The client
        must raise, not silently report SUCCESS."""
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(
                [_row(0), _row(1)],
                truncated=True,
                # No $skipToken
            ),
        )
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == (
                AzureResourceGraphErrorCategory.PAGINATION_INCOMPLETE
            )
        finally:
            client.close()

    def test_repeated_skip_token_protection(self, patch_token):
        """A repeated ``$skipToken`` means the upstream is looping.
        Refuse to multiply charges."""
        transport = _StubTransport()
        transport.push(
            200,
            _page_body([_row(0)], truncated=True, skip_token="cursor-A"),
        )
        transport.push(
            200,
            _page_body([_row(1)], truncated=True, skip_token="cursor-A"),  # repeat
        )
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == (
                AzureResourceGraphErrorCategory.UPSTREAM_ERROR
            )
            assert "repeated" in (exc_info.value.message or "").lower()
        finally:
            client.close()

    def test_repeated_page_signature_protection(self, patch_token):
        """If the upstream echoes the same page back (same first 3 ids),
        refuse to loop."""
        rows = [_row(0), _row(1), _row(2), _row(3)]
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(rows, truncated=True, skip_token="cursor-X"),
        )
        transport.push(
            200,
            _page_body(rows, truncated=True, skip_token="cursor-Y"),  # same rows
        )
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == (
                AzureResourceGraphErrorCategory.UPSTREAM_ERROR
            )
        finally:
            client.close()

    def test_max_pages_guard(self, monkeypatch, patch_token):
        monkeypatch.setattr(rg_mod, "MAX_PAGES", 3)
        transport = _StubTransport()
        # Distinct skip tokens so the repeated-token guard does not
        # fire first — we want the MAX_PAGES guard to fire.
        for i in range(10):
            transport.push(
                200,
                _page_body(
                    [_row(i)],
                    truncated=True,
                    skip_token=f"cursor-{i}",
                ),
            )
        client = AzureResourceGraphClient(max_pages=3)
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == (
                AzureResourceGraphErrorCategory.UPSTREAM_ERROR
            )
            assert "pagination exceeded" in (exc_info.value.message or "")
        finally:
            client.close()

    def test_duplicate_resource_ids_dropped_within_pages(self, patch_token):
        """When Resource Graph returns the same resource id across two
        pages (echoed), the client must dedup so the aggregator sees
        a single canonical row per resource id."""
        rows = [_row(0), _row(1), _row(2)]
        transport = _StubTransport()
        transport.push(
            200,
            _page_body(rows, truncated=True, skip_token="cursor-2"),
        )
        # Second page echoes row 2 (already seen) plus a new row 3.
        transport.push(
            200,
            _page_body([_row(2), _row(3)], truncated=False),
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            ids = [row["id"] for row in result.rows]
            assert ids == [
                "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.X/r0",
                "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.X/r1",
                "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.X/r2",
                "/subscriptions/sub-aaaa/resourceGroups/rg1/providers/Microsoft.X/r3",
            ]
        finally:
            client.close()

    def test_truncated_response_with_status_204_returns_no_data(self, patch_token):
        """204 No Content is a valid Resource Graph response — treat as
        empty (NO_DATA) rather than truncated."""
        transport = _StubTransport()
        transport.push(204)
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert result.total_records == 0
            assert result.rows == ()
        finally:
            client.close()

    def test_truncated_true_but_count_equals_total_accepted_as_complete(self, patch_token):
        """Resource Graph can return ``resultTruncated=true`` even when
        the page already contains every row the upstream knows about
        (particularly in tenant-mode queries).  The client must
        accept this as the success boundary rather than raise
        ``PAGINATION_INCOMPLETE``."""
        transport = _StubTransport()
        # 7 rows, totalRecords=7, resultTruncated=true, no $skipToken.
        # The data is logically complete — accept it.
        rows = [_row(i) for i in range(7)]
        transport.push(
            200,
            {
                "totalRecords": 7,
                "count": 7,
                "data": rows,
                "resultTruncated": True,
                # No $skipToken
            },
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
            assert len(result.rows) == 7
            assert result.total_records == 7
            assert result.pages_consumed == 1
        finally:
            client.close()

    def test_truncated_true_with_partial_count_raises_pagination_incomplete(self, patch_token):
        """When ``resultTruncated=true`` AND ``count < totalRecords`` AND
        no ``$skipToken`` is present, the client MUST raise.  This is
        the genuinely pathological case."""
        transport = _StubTransport()
        # 5 rows returned, but totalRecords=100 and resultTruncated=true
        # with no $skipToken — the upstream told us there are 95 more
        # rows but gave us no way to fetch them.
        transport.push(
            200,
            {
                "totalRecords": 100,
                "count": 5,
                "data": [_row(i) for i in range(5)],
                "resultTruncated": True,
                # No $skipToken
            },
        )
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == (
                AzureResourceGraphErrorCategory.PAGINATION_INCOMPLETE
            )
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
            _page_body(
                [
                    _row(1, rt="microsoft.compute/virtualmachines"),
                    _row(2, rt="microsoft.storage/storageaccounts"),
                ]
            ),
        )
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=["sub-aaaa"])
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
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.AUTH_FAILED
        finally:
            client.close()

    def test_403_raises_access_denied(self, patch_token):
        transport = _StubTransport()
        transport.push(403)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.ACCESS_DENIED
        finally:
            client.close()

    def test_404_raises_not_found(self, patch_token):
        transport = _StubTransport()
        transport.push(404)
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.NOT_FOUND
        finally:
            client.close()

    def test_429_carries_retry_after(self, patch_token):
        transport = _StubTransport()
        transport.push(429, headers={"Retry-After": "30"})
        client = _make_client(transport)
        try:
            with pytest.raises(AzureResourceGraphError) as exc_info:
                client.query(subscription_ids=["sub-aaaa"])
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
                client.query(subscription_ids=["sub-aaaa"])
            assert exc_info.value.category == AzureResourceGraphErrorCategory.UPSTREAM_ERROR
        finally:
            client.close()


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
        transport.push(200, _page_body([]))
        client = _make_client(transport)
        try:
            result = client.query(subscription_ids=[])
            assert result.total_records == 0
            assert result.rows == ()
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
            assert "a54d076b" in rendered
            assert "2650-4b4f-a28c-2deed2011f83" not in rendered
        finally:
            client.close()
