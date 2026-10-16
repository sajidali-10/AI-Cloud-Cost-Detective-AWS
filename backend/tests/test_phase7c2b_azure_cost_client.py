"""Phase 7C.2B — Azure Cost Management client tests.

Covers the unit-level behaviour of :mod:`app.services.cloud.azure.costs`:

* period helper (current month → MonthToDate, otherwise Custom)
* dynamic column resolution (UsageDate / ServiceName /
  ResourceLocation / Currency / PreTaxCost / Cost)
* 204 → NO_DATA
* 200 → parsed numeric / grouped output
* 401 / 403 / 404 / 429 / 5xx → categorised errors
* pagination (``properties.nextLink``) with bounded loop guard
* ``Retry-After`` parsing
* ``Unassigned`` bucket for blank resource locations
* sanitisation — no Bearer token / JWT / client assertion in any
  exception message or log payload
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx
import pytest

from app.services.cloud.azure import costs as azure_costs
from app.services.cloud.azure.costs import (
    AzureCostCallStatus,
    AzureCostClient,
    AzureCostError,
    AzureCostErrorCategory,
    AzureQueryResult,
    MAX_PAGES,
    UNASSIGNED_BUCKET,
    build_daily_dataset,
    build_grouped_dataset,
    build_time_period,
    build_total_dataset,
    extract_by_dimension,
    extract_daily,
    extract_total,
    parse_retry_after_header,
)


# ---------------------------------------------------------------------------
# Period helper
# ---------------------------------------------------------------------------


class TestBuildTimePeriod:
    def test_current_month_uses_month_to_date(self):
        today = date(2026, 9, 17)
        period_start = date(2026, 9, 1)
        period_end = date(2026, 10, 1)
        timeframe, from_iso, to_iso = build_time_period(
            period_start=period_start, period_end=period_end, today=today
        )
        assert timeframe == "MonthToDate"
        assert from_iso == ""
        assert to_iso == ""

    def test_completed_month_uses_custom(self):
        today = date(2026, 9, 17)
        period_start = date(2026, 8, 1)
        period_end = date(2026, 9, 1)
        timeframe, from_iso, to_iso = build_time_period(
            period_start=period_start, period_end=period_end, today=today
        )
        assert timeframe == "Custom"
        assert from_iso == "2026-08-01T00:00:00Z"
        assert to_iso == "2026-09-01T00:00:00Z"

    def test_future_month_uses_custom(self):
        today = date(2026, 9, 17)
        period_start = date(2026, 10, 1)
        period_end = date(2026, 11, 1)
        timeframe, from_iso, to_iso = build_time_period(
            period_start=period_start, period_end=period_end, today=today
        )
        assert timeframe == "Custom"
        assert from_iso == "2026-10-01T00:00:00Z"
        assert to_iso == "2026-11-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Dataset builders
# ---------------------------------------------------------------------------


class TestDatasetBuilders:
    def test_total_dataset_has_no_granularity(self):
        ds = build_total_dataset()
        assert ds["granularity"] is None
        assert ds["aggregation"]["totalCost"]["name"] == "PreTaxCost"
        assert ds["aggregation"]["totalCost"]["function"] == "Sum"

    def test_daily_dataset_has_daily_granularity(self):
        ds = build_daily_dataset()
        assert ds["granularity"] == "Daily"
        assert ds["aggregation"]["totalCost"]["name"] == "PreTaxCost"

    def test_grouped_dataset_includes_dimension(self):
        ds = build_grouped_dataset("ServiceName")
        assert ds["granularity"] is None
        assert ds["grouping"][0]["type"] == "Dimension"
        assert ds["grouping"][0]["name"] == "ServiceName"


# ---------------------------------------------------------------------------
# Retry-After parser
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_integer_seconds(self):
        assert parse_retry_after_header("30") == 30
        assert parse_retry_after_header("0") == 0

    def test_blank_or_missing(self):
        assert parse_retry_after_header("") is None
        assert parse_retry_after_header(None) is None

    def test_negative_seconds_rejected(self):
        assert parse_retry_after_header("-5") is None

    def test_date_form_returns_none(self):
        # Date form is ambiguous in this layer — the caller can
        # decide what to do.  We surface ``None`` so the API
        # layer never forwards a misleading value.
        assert parse_retry_after_header("Wed, 21 Oct 2026 07:28:00 GMT") is None


# ---------------------------------------------------------------------------
# Dynamic column extraction
# ---------------------------------------------------------------------------


class TestExtractTotal:
    def _result(self, columns, rows):
        return AzureQueryResult(
            columns=tuple(
                azure_costs.AzureCostClient._parse_columns(columns)
            ),
            rows=tuple(tuple(r) for r in rows),
        )

    def test_simple_sum_with_currency(self):
        cols = [
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ]
        rows = [
            [10.50, "USD"],
            [5.25, "USD"],
        ]
        status, total, currency = extract_total(self._result(cols, rows))
        assert status == AzureCostCallStatus.OK
        assert total == Decimal("15.75")
        assert currency == "USD"

    def test_currency_lowercase_normalised(self):
        cols = [
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ]
        rows = [[12.34, "eur"]]
        _, _, currency = extract_total(self._result(cols, rows))
        assert currency == "EUR"

    def test_empty_rows_returns_empty_status(self):
        cols = [{"name": "PreTaxCost", "type": "Number"}]
        status, total, _ = extract_total(self._result(cols, []))
        assert status == AzureCostCallStatus.EMPTY
        assert total == Decimal("0")

    def test_missing_cost_column_returns_empty(self):
        cols = [{"name": "ServiceName", "type": "String"}]
        status, total, _ = extract_total(self._result(cols, [["foo"]]))
        assert status == AzureCostCallStatus.EMPTY

    def test_zero_total_is_ok_not_no_data(self):
        cols = [
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ]
        rows = [[0, "USD"]]
        status, total, _ = extract_total(self._result(cols, rows))
        assert status == AzureCostCallStatus.OK
        assert total == Decimal("0.00")


class TestExtractDaily:
    def _result(self, columns, rows):
        return AzureQueryResult(
            columns=tuple(
                azure_costs.AzureCostClient._parse_columns(columns)
            ),
            rows=tuple(tuple(r) for r in rows),
        )

    def test_daily_points_sorted_by_date(self):
        cols = [
            {"name": "UsageDate", "type": "DateTime"},
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ]
        rows = [
            ["2026-09-03", 5.0, "USD"],
            ["2026-09-01", 1.5, "USD"],
            ["2026-09-02", 2.5, "USD"],
        ]
        status, points, currency = extract_daily(self._result(cols, rows))
        assert status == AzureCostCallStatus.OK
        assert currency == "USD"
        assert [d for d, _, _ in points] == [
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 3),
        ]
        assert points[0][1] == Decimal("1.50")
        assert points[2][1] == Decimal("5.00")

    def test_daily_with_billing_period_fallback(self):
        cols = [
            {"name": "BillingPeriodStartDate", "type": "DateTime"},
            {"name": "PreTaxCost", "type": "Number"},
        ]
        rows = [["2026-09-01", 9.99]]
        status, points, _ = extract_daily(self._result(cols, rows))
        assert status == AzureCostCallStatus.OK
        assert points[0][0] == date(2026, 9, 1)
        assert points[0][1] == Decimal("9.99")

    def test_daily_datetime_with_z_suffix(self):
        cols = [
            {"name": "UsageDate", "type": "DateTime"},
            {"name": "PreTaxCost", "type": "Number"},
        ]
        rows = [["2026-09-01T00:00:00Z", 7.0]]
        _, points, _ = extract_daily(self._result(cols, rows))
        assert points[0][0] == date(2026, 9, 1)


class TestExtractByDimension:
    def _result(self, columns, rows):
        return AzureQueryResult(
            columns=tuple(
                azure_costs.AzureCostClient._parse_columns(columns)
            ),
            rows=tuple(tuple(r) for r in rows),
        )

    def test_service_grouping(self):
        cols = [
            {"name": "ServiceName", "type": "String"},
            {"name": "PreTaxCost", "type": "Number"},
            {"name": "Currency", "type": "String"},
        ]
        rows = [
            ["Virtual Machines", 12.0, "USD"],
            ["Storage", 4.0, "USD"],
            ["Virtual Machines", 3.0, "USD"],
        ]
        status, items, currency = extract_by_dimension(
            self._result(cols, rows), dimension_name="ServiceName"
        )
        assert status == AzureCostCallStatus.OK
        assert currency == "USD"
        names = [name for name, _, _ in items]
        assert names == ["Virtual Machines", "Storage"]
        assert items[0][1] == Decimal("15.00")
        assert items[1][1] == Decimal("4.00")

    def test_blank_resource_location_bucketed_as_unassigned(self):
        cols = [
            {"name": "ResourceLocation", "type": "String"},
            {"name": "PreTaxCost", "type": "Number"},
        ]
        rows = [
            ["eastus", 5.0],
            ["", 1.5],
            [None, 2.5],
        ]
        status, items, _ = extract_by_dimension(
            self._result(cols, rows),
            dimension_name="ResourceLocation",
        )
        assert status == AzureCostCallStatus.OK
        # eastus + Unassigned (which combines '' and None)
        names = [name for name, _, _ in items]
        assert UNASSIGNED_BUCKET in names
        assert "eastus" in names
        unassigned_amount = next(
            amt for name, amt, _ in items if name == UNASSIGNED_BUCKET
        )
        assert unassigned_amount == Decimal("4.00")

    def test_missing_dimension_returns_empty(self):
        cols = [
            {"name": "PreTaxCost", "type": "Number"},
        ]
        rows = [[1.0]]
        status, items, _ = extract_by_dimension(
            self._result(cols, rows), dimension_name="ServiceName"
        )
        assert status == AzureCostCallStatus.EMPTY
        assert items == []


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class TestStatusEnum:
    def test_status_values(self):
        assert AzureCostCallStatus.OK.value == "OK"
        assert AzureCostCallStatus.NO_DATA.value == "NO_DATA"
        assert AzureCostCallStatus.EMPTY.value == "EMPTY"
        assert AzureCostCallStatus.EMPTY_FOR_PERIOD.value == "EMPTY_FOR_PERIOD"


# ---------------------------------------------------------------------------
# Live mock — exercise the AzureCostClient against a fake httpx
# transport so we can verify the wire-level behaviour (200/204/401/
# 403/404/429/5xx, pagination, nextLink validation) without an
# actual Azure subscription.
# ---------------------------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    """httpx transport stub that records calls and returns canned responses."""

    def __init__(self):
        self.calls: list = []
        self.responses: list = []  # list of (status, json_body, headers)
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


class _MockToken:
    """Mock azure-identity AccessToken — never a real secret."""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.expires_on = 9999999999


@pytest.fixture
def patch_token(monkeypatch):
    """Stub ``acquire_access_token`` so the client can issue calls."""
    monkeypatch.setattr(
        azure_costs,
        "acquire_access_token",
        lambda **kwargs: "test-token-not-real",
    )


class TestClientStatusMapping:
    def _build_client(self, transport):
        """Build an AzureCostClient whose httpx transport is the stub.

        We attach a base_url to the httpx client so the cookie /
        urllib parser inside httpx can resolve the URL during
        response handling.  In production the base_url is the
        real ARM management endpoint; here it is the same URL so
        the behaviour is identical.
        """
        client = AzureCostClient()
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        return client

    def test_204_returns_empty_result(self, patch_token):
        transport = _StubTransport()
        transport.push(204)
        client = self._build_client(transport)
        try:
            result = client.query(
                subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                dataset=build_total_dataset(),
                timeframe="MonthToDate",
            )
            assert result.columns == ()
            assert result.rows == ()
        finally:
            client.close()

    def test_401_raises_auth_failed(self, patch_token):
        transport = _StubTransport()
        transport.push(401, {"error": {"code": "InvalidAuthenticationToken"}})
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.AUTH_FAILED
            assert exc_info.value.status_code == 401
        finally:
            client.close()

    def test_403_raises_access_denied(self, patch_token):
        transport = _StubTransport()
        transport.push(403)
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.ACCESS_DENIED
        finally:
            client.close()

    def test_404_raises_not_found(self, patch_token):
        transport = _StubTransport()
        transport.push(404)
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.NOT_FOUND
        finally:
            client.close()

    def test_429_carries_retry_after(self, patch_token):
        transport = _StubTransport()
        transport.push(429, headers={"Retry-After": "30"})
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.THROTTLED
            assert exc_info.value.retry_after == 30
        finally:
            client.close()

    def test_5xx_raises_upstream_error(self, patch_token):
        transport = _StubTransport()
        transport.push(503)
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.UPSTREAM_ERROR
            assert exc_info.value.status_code == 503
        finally:
            client.close()


class TestClientPagination:
    def _build_client(self, transport):
        client = AzureCostClient()
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        return client

    def test_follows_next_link(self, patch_token):
        transport = _StubTransport()
        # Page 1: real data + nextLink
        transport.push(
            200,
            {
                "properties": {
                    "columns": [
                        {"name": "PreTaxCost", "type": "Number"},
                        {"name": "Currency", "type": "String"},
                    ],
                    "rows": [[1.0, "USD"]],
                    "nextLink": (
                        "https://management.azure.com/subscriptions/"
                        "a54d076b-2650-4b4f-a28c-2deed2011f83/providers/"
                        "Microsoft.CostManagement/query?api-version=2026-06-01"
                        "&continuationToken=abc"
                    ),
                }
            },
        )
        # Page 2: more rows, no nextLink
        transport.push(
            200,
            {
                "properties": {
                    "columns": [
                        {"name": "PreTaxCost", "type": "Number"},
                        {"name": "Currency", "type": "String"},
                    ],
                    "rows": [[2.0, "USD"]],
                }
            },
        )
        client = self._build_client(transport)
        try:
            result = client.query(
                subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                dataset=build_total_dataset(),
                timeframe="MonthToDate",
            )
            assert len(result.rows) == 2
            assert result.find_column("PreTaxCost") == 0
        finally:
            client.close()

    def test_rejects_off_platform_next_link(self, patch_token):
        transport = _StubTransport()
        transport.push(
            200,
            {
                "properties": {
                    "columns": [{"name": "PreTaxCost", "type": "Number"}],
                    "rows": [[1.0]],
                    "nextLink": "https://attacker.example.com/query?evil=1",
                }
            },
        )
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.UPSTREAM_ERROR
            assert "not an ARM management endpoint" in (exc_info.value.message or "")
        finally:
            client.close()

    def test_loop_guard_triggers(self, monkeypatch, patch_token):
        # Reduce MAX_PAGES to a tiny number so the test runs fast.
        monkeypatch.setattr(azure_costs, "MAX_PAGES", 3)
        transport = _StubTransport()
        for _ in range(10):
            transport.push(
                200,
                {
                    "properties": {
                        "columns": [{"name": "PreTaxCost", "type": "Number"}],
                        "rows": [[1.0]],
                        "nextLink": (
                            "https://management.azure.com/subscriptions/"
                            "a54d076b/providers/Microsoft.CostManagement/query"
                            "?api-version=2026-06-01&c=1"
                        ),
                    }
                },
            )
        # Build client with the lowered max_pages so the loop
        # guard triggers as expected.  The module-level
        # monkeypatch keeps any future constructor call honest.
        client = AzureCostClient(max_pages=3)
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.UPSTREAM_ERROR
            assert "pagination exceeded" in (exc_info.value.message or "")
        finally:
            client.close()

    def test_schema_drift_between_pages_rejected(self, patch_token):
        transport = _StubTransport()
        transport.push(
            200,
            {
                "properties": {
                    "columns": [{"name": "PreTaxCost", "type": "Number"}],
                    "rows": [[1.0]],
                    "nextLink": (
                        "https://management.azure.com/subscriptions/"
                        "a54d076b/providers/Microsoft.CostManagement/query"
                        "?api-version=2026-06-01&c=1"
                    ),
                }
            },
        )
        transport.push(
            200,
            {
                "properties": {
                    "columns": [{"name": "SomethingElse", "type": "String"}],
                    "rows": [["foo"]],
                }
            },
        )
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            assert exc_info.value.category == AzureCostErrorCategory.INVALID_RESPONSE
        finally:
            client.close()


class TestClientSanitisation:
    def _build_client(self, transport):
        client = AzureCostClient()
        client._http = httpx.Client(
            transport=transport, base_url="https://management.azure.com"
        )
        return client

    def test_no_token_in_exception_message(self, patch_token):
        transport = _StubTransport()
        transport.push(401)
        client = self._build_client(transport)
        try:
            with pytest.raises(AzureCostError) as exc_info:
                client.query(
                    subscription_id="a54d076b-2650-4b4f-a28c-2deed2011f83",
                    dataset=build_total_dataset(),
                    timeframe="MonthToDate",
                )
            rendered = str(exc_info.value)
            assert "test-token" not in rendered
            assert "Bearer" not in rendered
            # Subscription id is truncated via safe_subscription_label
            assert "a54d076b" in rendered
            assert "2650-4b4f-a28c-2deed2011f83" not in rendered
        finally:
            client.close()

    def test_safe_subscription_label_used(self):
        # Independent of the client — sanity check.
        from app.services.cloud.azure.redaction import safe_subscription_label

        assert safe_subscription_label("a54d076b-2650-4b4f-a28c-2deed2011f83") == "a54d076b\u2026"
        assert safe_subscription_label(None) == "<unset>"
        assert safe_subscription_label("") == "<unset>"


# ---------------------------------------------------------------------------
# Constants pin
# ---------------------------------------------------------------------------


def test_api_version_pinned():
    assert azure_costs.COST_MGMT_API_VERSION == "2026-06-01"


def test_cost_type_pinned_to_actual():
    # The pinned cost semantic is documented in the module docstring
    # and tested in :func:`test_dataset_total_uses_pre_tax_cost`.
    # We re-check it here so any future change to a different cost
    # type triggers a test failure and an explicit decision.
    assert azure_costs.COST_TYPE_ACTUAL == "ActualCost"


def test_max_pages_bounded():
    assert azure_costs.MAX_PAGES > 0
    assert azure_costs.MAX_PAGES <= 100
