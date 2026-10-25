"""Phase 9B.1 — Rackspace canonical cost normalization tests.

Covers the pure normalizer (:mod:`app.services.cloud.rackspace.costs`):

* successful current-period normalization (currency / period / total /
  service breakdown / reconciliation)
* the authoritative envelope total (``total`` field) drives the
  canonical total; un-itemized remainders are bucketed explicitly
* true-zero semantics (a valid ``$0`` is a SUCCESS, never unavailable)
* currency propagation (never hardcoded USD)
* period propagation (actual source period, verbatim)
* breakdown ↔ total reconciliation incl. mismatch detection
* uncategorized bucket for records without a product field
* malformed / missing dimension handling (INVALID_RESPONSE — never
  fabricated data)
* typed error translation from the transport layer (401 / 403 / 404 /
  429 / 5xx / timeout / not-configured)
* period-overlap rule (PERIOD_UNSUPPORTED for non-overlapping windows)

No test touches the network — the billing client is stubbed and no
real credentials are read (conftest blanks the RACKSPACE_* env vars).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

import pytest

from app.services.cloud.rackspace.costs import (
    RACKSPACE_COST_SOURCE,
    RackspaceCostError,
    RackspaceCostErrorCategory,
    assert_period_overlap,
    fetch_cost_snapshot,
    normalize_estimated_charges,
    translate_rackspace_exception,
)
from app.services.rackspace.client import RackspaceResponse
from app.services.rackspace.exceptions import (
    RackspaceAuthError,
    RackspaceConfigError,
    RackspaceNotFoundError,
    RackspacePermissionError,
    RackspaceThrottledError,
    RackspaceTransportError,
)


# ---------------------------------------------------------------------------
# Fixture-shaped helpers
# ---------------------------------------------------------------------------


def _body(**overrides: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "currency": "USD",
        "estimatedTotal": "1250.50",
        "currentBillingPeriodStartDate": "2026-10-01",
        "currentBillingPeriodEndDate": "2026-10-31",
        "estimatedCharges": [
            {"service": "Cloud Servers", "amount": "1000.00", "currency": "USD"},
            {"service": "Cloud Files", "amount": "250.50", "currency": "USD"},
        ],
    }
    body.update(overrides)
    return body


class _StubBillingClient:
    """Stubbed billing client for the canonical fetch path."""

    def __init__(
        self,
        *,
        body: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self._body = body
        self._error = error
        self.calls: list = []

    async def fetch_estimated_charges(self) -> Dict[str, Any]:
        self.calls.append({})
        if self._error is not None:
            raise self._error
        assert self._body is not None
        return self._body


# ---------------------------------------------------------------------------
# Successful normalization
# ---------------------------------------------------------------------------


class TestNormalizeSuccess:
    def test_successful_normalization(self):
        snapshot = normalize_estimated_charges(_body())
        assert snapshot.currency == "USD"
        assert snapshot.period_start == date(2026, 10, 1)
        assert snapshot.period_end == date(2026, 10, 31)
        # Canonical total is the exact Decimal record sum.
        assert snapshot.total == Decimal("1250.50")
        assert snapshot.estimated is True
        assert snapshot.record_count == 2
        assert snapshot.extensions["source"] == RACKSPACE_COST_SOURCE
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services == {
            "Cloud Servers": Decimal("1000.00"),
            "Cloud Files": Decimal("250.50"),
        }

    def test_breakdown_reconciles_exactly_with_total(self):
        snapshot = normalize_estimated_charges(_body())
        breakdown_sum = sum((s.amount for s in snapshot.by_service), Decimal("0"))
        assert breakdown_sum == snapshot.total

    def test_body_total_mismatch_is_surfaced_not_hidden(self):
        # The authoritative envelope total disagrees with the record
        # sum by more than a cent: the canonical total IS the
        # authoritative total, and the un-itemized remainder is
        # carried by an explicit bucket + surfaced as a warning —
        # never silently absorbed and never fabricated.
        body = _body(estimatedTotal="9999.99")
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("9999.99")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["Un-itemized charges"] == Decimal("8749.49")
        assert any(
            w.startswith("unitemized_charges=") for w in snapshot.warnings
        )
        # The breakdown still reconciles exactly with the total.
        assert sum((s.amount for s in snapshot.by_service), Decimal("0")) == (
            snapshot.total
        )

    def test_body_total_within_tolerance_no_warning(self):
        body = _body(estimatedTotal="1250.51")
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("1250.51")
        # A cent-level remainder is still bucketed explicitly (exact
        # reconciliation) but does not raise a warning.
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["Un-itemized charges"] == Decimal("0.01")
        assert not any(
            w.startswith("unitemized_charges=") for w in snapshot.warnings
        )

    def test_currency_propagates_not_hardcoded_usd(self):
        body = _body(
            currency="EUR",
            estimatedTotal="10.00",
            estimatedCharges=[
                {"service": "Cloud Servers", "amount": "10.00", "currency": "EUR"},
            ],
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.currency == "EUR"
        assert snapshot.total == Decimal("10.00")

    def test_currency_derived_from_records_when_body_lacks_it(self):
        body = _body(currency=None)
        snapshot = normalize_estimated_charges(body)
        assert snapshot.currency == "USD"

    def test_period_from_nested_current_period_object(self):
        body = _body(
            currentBillingPeriodStartDate=None,
            currentBillingPeriodEndDate=None,
            currentPeriod={"start": "2026-09-01", "end": "2026-09-30"},
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.period_start == date(2026, 9, 1)
        assert snapshot.period_end == date(2026, 9, 30)

    def test_period_accepts_datetime_shaped_strings(self):
        body = _body(
            currentBillingPeriodStartDate="2026-10-01T00:00:00Z",
            currentBillingPeriodEndDate="2026-10-31T23:59:59Z",
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.period_start == date(2026, 10, 1)
        assert snapshot.period_end == date(2026, 10, 31)

    def test_wrapped_envelope_shape(self):
        # The whole payload nested under a single ``estimatedCharges``
        # object key.
        body = {
            "estimatedCharges": {
                "currency": "USD",
                "currentPeriod": {"start": "2026-10-01", "end": "2026-10-31"},
                "estimatedCharges": [
                    {"offeringCode": "CLOUD_SERVERS", "amount": 42},
                ],
            }
        }
        snapshot = normalize_estimated_charges(body)
        assert snapshot.currency == "USD"
        assert snapshot.total == Decimal("42")
        assert [s.service for s in snapshot.by_service] == ["CLOUD_SERVERS"]

    def test_numeric_amounts_parsed_via_decimal_not_float(self):
        body = _body(
            estimatedTotal="0.3",
            estimatedCharges=[
                {"service": "A", "amount": 0.1},
                {"service": "B", "amount": 0.2},
            ]
        )
        snapshot = normalize_estimated_charges(body)
        # 0.1 + 0.2 must NOT produce the binary-float artefact
        # 0.30000000000000004.
        assert snapshot.total == Decimal("0.3")

    def test_records_without_product_field_bucketed_explicitly(self):
        body = _body(
            estimatedTotal="15.00",
            estimatedCharges=[
                {"amount": "10.00", "currency": "USD"},
                {"service": "Cloud Servers", "amount": "5.00", "currency": "USD"},
            ]
        )
        snapshot = normalize_estimated_charges(body)
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services == {
            "Uncategorized": Decimal("10.00"),
            "Cloud Servers": Decimal("5.00"),
        }
        assert any(
            w.startswith("uncategorized_charge_records=")
            for w in snapshot.warnings
        )
        # The uncategorized bucket still reconciles.
        assert sum((s.amount for s in snapshot.by_service), Decimal("0")) == (
            snapshot.total
        )

    def test_no_records_total_from_body_with_warning(self):
        body = _body(
            estimatedCharges=None,
            estimatedTotal="77.00",
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("77.00")
        # The whole total is un-itemized — carried by the explicit
        # bucket so the breakdown reconciles exactly.
        assert [(s.service, s.amount) for s in snapshot.by_service] == [
            ("Un-itemized charges", Decimal("77.00"))
        ]
        assert snapshot.record_count == 0
        assert "no_charge_records_total_from_body" in snapshot.warnings


# ---------------------------------------------------------------------------
# True zero semantics
# ---------------------------------------------------------------------------


class TestTrueZero:
    def test_records_totaling_zero_is_success(self):
        body = _body(
            estimatedCharges=[
                {"service": "Cloud Servers", "amount": "0.00", "currency": "USD"},
            ],
            estimatedTotal="0.00",
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0.00")
        # A true zero is a perfectly valid snapshot — nothing about the
        # shape marks it unavailable.

    def test_empty_record_list_with_zero_body_total_is_success(self):
        body = _body(estimatedCharges=[], estimatedTotal="0")
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0")
        assert snapshot.by_service == ()

    def test_zero_breakdown_reconciles(self):
        body = _body(estimatedCharges=[], estimatedTotal="0")
        snapshot = normalize_estimated_charges(body)
        assert sum((s.amount for s in snapshot.by_service), Decimal("0")) == (
            snapshot.total
        )


# ---------------------------------------------------------------------------
# Malformed / unsupported payloads — never fabricate
# ---------------------------------------------------------------------------


class TestInvalidResponses:
    def test_missing_currency_is_invalid(self):
        body = _body(currency=None, estimatedCharges=[])
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_mixed_record_currencies_is_invalid(self):
        body = _body(
            currency=None,
            estimatedCharges=[
                {"service": "A", "amount": "1.00", "currency": "EUR"},
                {"service": "B", "amount": "2.00", "currency": "USD"},
            ],
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_missing_period_is_invalid(self):
        body = _body(
            currentBillingPeriodStartDate=None,
            currentBillingPeriodEndDate=None,
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_period_end_before_start_is_invalid(self):
        body = _body(
            currentBillingPeriodStartDate="2026-10-31",
            currentBillingPeriodEndDate="2026-10-01",
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_empty_period_span_is_invalid(self):
        # Exclusive-end semantics: start == end is a zero-length
        # period and cannot carry charges.
        body = _body(
            currentBillingPeriodStartDate="2026-10-19",
            currentBillingPeriodEndDate="2026-10-19",
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_no_records_and_no_total_is_invalid(self):
        body = _body(estimatedCharges=None, estimatedTotal=None)
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_record_without_amount_is_invalid(self):
        body = _body(
            estimatedCharges=[{"service": "Cloud Servers", "currency": "USD"}]
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE

    def test_non_dict_body_is_invalid(self):
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(["not", "a", "dict"])  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.INVALID_RESPONSE


# ---------------------------------------------------------------------------
# Period overlap rule
# ---------------------------------------------------------------------------


class TestPeriodOverlap:
    def _snapshot(self):
        return normalize_estimated_charges(_body())

    def test_exact_window_supported(self):
        assert_period_overlap(
            requested_start=date(2026, 10, 1),
            requested_end=date(2026, 10, 31),
            snapshot=self._snapshot(),
        )

    def test_partial_overlap_supported(self):
        # A 30-day lookback that spans two cycles still overlaps the
        # current cycle — served with the ACTUAL source period.
        assert_period_overlap(
            requested_start=date(2026, 9, 25),
            requested_end=date(2026, 10, 25),
            snapshot=self._snapshot(),
        )

    def test_historical_window_is_unsupported(self):
        with pytest.raises(RackspaceCostError) as excinfo:
            assert_period_overlap(
                requested_start=date(2026, 1, 1),
                requested_end=date(2026, 1, 31),
                snapshot=self._snapshot(),
            )
        assert (
            excinfo.value.category == RackspaceCostErrorCategory.PERIOD_UNSUPPORTED
        )

    def test_future_window_is_unsupported(self):
        with pytest.raises(RackspaceCostError) as excinfo:
            assert_period_overlap(
                requested_start=date(2027, 1, 1),
                requested_end=date(2027, 1, 31),
                snapshot=self._snapshot(),
            )
        assert (
            excinfo.value.category == RackspaceCostErrorCategory.PERIOD_UNSUPPORTED
        )


# ---------------------------------------------------------------------------
# Typed error translation
# ---------------------------------------------------------------------------


class TestErrorTranslation:
    @pytest.mark.parametrize(
        "exc,expected_category",
        [
            (RackspaceConfigError("no ran"), RackspaceCostErrorCategory.NOT_CONFIGURED),
            (RackspaceAuthError("401"), RackspaceCostErrorCategory.AUTH_FAILED),
            (RackspacePermissionError("403"), RackspaceCostErrorCategory.ACCESS_DENIED),
            (RackspaceNotFoundError("404"), RackspaceCostErrorCategory.NOT_FOUND),
            (
                RackspaceThrottledError("429", retry_after=17.0),
                RackspaceCostErrorCategory.THROTTLED,
            ),
            (
                RackspaceTransportError("timeout"),
                RackspaceCostErrorCategory.NETWORK_ERROR,
            ),
        ],
    )
    def test_translation_matrix(self, exc, expected_category):
        translated = translate_rackspace_exception(exc)
        assert translated.category == expected_category

    def test_throttled_retry_after_propagates(self):
        translated = translate_rackspace_exception(
            RackspaceThrottledError("429", retry_after=17.0)
        )
        assert translated.retry_after == 17.0

    def test_throttled_checked_before_transport_subclass(self):
        # RackspaceThrottledError IS a RackspaceTransportError — the
        # translation must prefer the specific class.
        translated = translate_rackspace_exception(RackspaceThrottledError("429"))
        assert translated.category == RackspaceCostErrorCategory.THROTTLED

    def test_error_messages_never_carry_credentials(self):
        # The canonical translation replaces the transport-layer
        # detail with a stable, sanitized category message.
        translated = translate_rackspace_exception(
            RackspaceAuthError("Rackspace rejected the X-Auth-Token (HTTP 401) at https://x")
        )
        assert translated.category == RackspaceCostErrorCategory.AUTH_FAILED
        assert "api_key" not in translated.message.lower()
        assert "apikey" not in translated.message.lower()
        assert "token=" not in translated.message.lower()
        assert "secret" not in translated.message.lower()


# ---------------------------------------------------------------------------
# Fetch path (stubbed billing client)
# ---------------------------------------------------------------------------


class TestFetchCostSnapshot:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _StubBillingClient(body=_body())
        snapshot = await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert snapshot.total == Decimal("1250.50")
        assert client.calls

    @pytest.mark.asyncio
    async def test_not_configured(self):
        client = _StubBillingClient(error=RackspaceConfigError("no ran"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_auth_failed(self):
        client = _StubBillingClient(error=RackspaceAuthError("401"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.AUTH_FAILED

    @pytest.mark.asyncio
    async def test_permission_denied(self):
        client = _StubBillingClient(error=RackspacePermissionError("403"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.ACCESS_DENIED

    @pytest.mark.asyncio
    async def test_not_found(self):
        client = _StubBillingClient(error=RackspaceNotFoundError("404"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.NOT_FOUND

    @pytest.mark.asyncio
    async def test_throttled(self):
        client = _StubBillingClient(
            error=RackspaceThrottledError("429", retry_after=12.0)
        )
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.THROTTLED
        assert excinfo.value.retry_after == 12.0

    @pytest.mark.asyncio
    async def test_server_error(self):
        client = _StubBillingClient(error=RackspaceTransportError("HTTP 503"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.NETWORK_ERROR

    @pytest.mark.asyncio
    async def test_timeout(self):
        client = _StubBillingClient(error=RackspaceTransportError("timeout"))
        with pytest.raises(RackspaceCostError) as excinfo:
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
        assert excinfo.value.category == RackspaceCostErrorCategory.NETWORK_ERROR

    @pytest.mark.asyncio
    async def test_api_failure_never_becomes_zero(self):
        # A failing fetch raises — it can NEVER be conflated with a
        # legitimate $0 result.
        client = _StubBillingClient(error=RackspaceTransportError("down"))
        with pytest.raises(RackspaceCostError):
            await fetch_cost_snapshot(client)  # type: ignore[arg-type]
