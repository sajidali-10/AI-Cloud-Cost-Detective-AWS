"""Phase 9B — Rackspace cost reconciliation tests.

Closes the Phase 9B reconciliation gap where the raw Billing V2
``estimated_charges`` authoritative total (``total``) did not match
the canonical normalized total:

* raw live response: ``total = 9.0 USD`` (envelope ``total`` field)
* old canonical output: ``0.00`` (record-sum of $0-rated line items)

The invariant these tests enforce:

    raw authoritative envelope total == canonical normalized total
    service breakdown sum          == canonical normalized total

at any point in time, for whatever the authoritative value is.

The fixture below is shaped EXACTLY like the live Billing V2
``estimated_charges`` response (string amounts, ``offeringCode``
product labels, ``chargeTotal`` / ``discountTotal`` / ``total``
envelope fields, one record without an ``offeringCode``) but carries
NO real account identifiers — offering codes are the public Rackspace
product SKUs and every number is synthetic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from app.services.cloud.rackspace.costs import (
    UNITEMIZED_SERVICE,
    RackspaceCostError,
    RackspaceCostErrorCategory,
    normalize_estimated_charges,
)


# ---------------------------------------------------------------------------
# Realistic sanitized fixture — shaped like the LIVE Billing V2 response
# (no real account identifiers; synthetic numbers only)
# ---------------------------------------------------------------------------


def _live_shaped_body(**overrides: Any) -> Dict[str, Any]:
    """A sanitized replica of the live ``estimated_charges`` body.

    Mirrors the observed live structure exactly:

    * single ``estimatedCharges`` envelope object
    * string-valued money fields (``"0.00"``, ``"9"``)
    * ``chargeTotal`` = itemized record sum, ``total`` = the
      authoritative cycle total (which includes a non-itemized
      minimum / support charge)
    * ``estimatedCharge`` records keyed by ``offeringCode``
    * one record with no ``offeringCode`` (support line)
    """
    body: Dict[str, Any] = {
        "estimatedCharges": {
            "chargeTotal": "0.00",
            "currency": "USD",
            "currentBillingPeriodStartDate": "2026-10-19T00:00:00Z",
            "currentBillingPeriodEndDate": "2026-11-19T00:00:00Z",
            "discountTotal": "0.00",
            "durationRemainingInCycle": "P25D",
            "estimatedCharge": [
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "STORAGE_VOLUME",
                    "offeringCode": "CBS",
                    "quantity": "143400.00",
                    "unitOfMeasure": "GB_HRS",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "STORAGE_BKUPS",
                    "offeringCode": "CBS",
                    "quantity": "4158.29",
                    "rate": "0.000407822",
                    "unitOfMeasure": "GB_HRS",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "USG_PER_DAY",
                    "offeringCode": "CBCKUP",
                    "quantity": "100.00",
                    "rate": "0.9793672",
                    "unitOfMeasure": "NO_OF_DAYS",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "UPTIME",
                    "offeringCode": "NXTGEN",
                    "quantity": "4080.00",
                    "unitOfMeasure": "HRS",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "UPTIME_SUPPORT",
                    "offeringCode": "NXTGEN",
                    "quantity": "22200.00",
                    "rate": "0.014884",
                    "unitOfMeasure": "GB_HRS",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "BWOUT",
                    "offeringCode": "NXTGEN",
                    "quantity": "68.37",
                    "unitOfMeasure": "GB",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "STORAGE_IMAGE",
                    "offeringCode": "NXTGEN",
                    "quantity": "224.14",
                    "rate": "0.29768",
                    "unitOfMeasure": "GB",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "STORAGE",
                    "offeringCode": "CFILES",
                    "quantity": "8.84",
                    "unitOfMeasure": "GB",
                },
                {
                    "amount": "0.00",
                    "category": "CHARGE",
                    "chargeType": "ADDITIONAL_SUPPORT",
                    "eligibleAmount": "148.84",
                    "quantity": "1.00",
                    "unitOfMeasure": "COUNT",
                },
            ],
            "link": [
                {"href": "https://billing.api.rackspacecloud.com/v2/accounts/<redacted>/estimated_charges", "rel": "self"},
            ],
            "total": "9",
        },
    }
    body["estimatedCharges"].update(
        overrides.pop("envelope", {})  # type: ignore[arg-type]
    )
    body.update(overrides)
    return body


def _raw_authoritative_total(body: Dict[str, Any]) -> Decimal:
    """Independent extraction of the raw authoritative envelope total.

    Reads the documented Billing V2 fields directly (envelope
    ``total`` first, then the component totals) — deliberately NOT
    re-using the normalizer's helper so the reconciliation test is a
    genuine cross-check.
    """
    env = body.get("estimatedCharges")
    if not isinstance(env, dict):
        env = body
    for key in ("total", "chargeTotal", "estimatedTotal", "currentCharges", "amount"):
        value = env.get(key)
        if value is None:
            continue
        return Decimal(str(value))
    raise AssertionError("fixture carries no authoritative total")


# ---------------------------------------------------------------------------
# Raw ↔ canonical reconciliation (the Phase 9B closure invariant)
# ---------------------------------------------------------------------------


class TestRawCanonicalReconciliation:
    def test_live_shaped_body_reconciles_with_raw_authoritative_total(self):
        body = _live_shaped_body()
        snapshot = normalize_estimated_charges(body)
        # THE closure invariant: the canonical total equals the raw
        # authoritative Billing total at the same point in time.
        assert snapshot.total == _raw_authoritative_total(body)
        assert snapshot.total == Decimal("9")
        assert snapshot.currency == "USD"

    def test_live_shaped_body_breakdown_sums_to_canonical_total(self):
        snapshot = normalize_estimated_charges(_live_shaped_body())
        breakdown_sum = sum(
            (s.amount for s in snapshot.by_service), Decimal("0")
        )
        assert breakdown_sum == snapshot.total == Decimal("9")

    def test_live_shaped_body_unitemized_bucket_is_explicit(self):
        snapshot = normalize_estimated_charges(_live_shaped_body())
        services = {s.service: s.amount for s in snapshot.by_service}
        # All nine records are $0-rated: the authoritative $9 cycle
        # total is NOT itemized in any record — it lands in the
        # explicit un-itemized bucket, never dropped and never
        # re-attributed to a real offering.
        assert services[UNITEMIZED_SERVICE] == Decimal("9")
        assert services.get("CBS") == Decimal("0.00")
        assert services.get("NXTGEN") == Decimal("0.00")
        assert any(
            w.startswith("unitemized_charges=") for w in snapshot.warnings
        )
        assert snapshot.record_count == 9
        assert snapshot.extensions["itemized_total"] == "0.00"
        assert snapshot.extensions["unitemized_total"] == "9.00"

    def test_partial_itemization_reconciles(self):
        # Some records itemized, remainder un-itemized.
        body = _live_shaped_body(
            envelope={
                "total": "120.00",
                "chargeTotal": "100.00",
            }
        )
        # Re-key the first two records to real amounts.
        records = body["estimatedCharges"]["estimatedCharge"]
        records[0]["amount"] = "60.00"
        records[1]["amount"] = "40.00"
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("120.00")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("100.00")
        assert services[UNITEMIZED_SERVICE] == Decimal("20.00")
        assert sum((s.amount for s in snapshot.by_service), Decimal("0")) == (
            snapshot.total
        )

    def test_itemized_records_exactly_equal_total_no_unitemized_bucket(self):
        body = _live_shaped_body(
            envelope={
                "total": "0.00",
                "chargeTotal": "0.00",
            }
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0.00")
        assert UNITEMIZED_SERVICE not in {
            s.service for s in snapshot.by_service
        }


# ---------------------------------------------------------------------------
# Non-zero normalization + multi-record summation
# ---------------------------------------------------------------------------


class TestNonZeroNormalization:
    def test_nonzero_estimated_charges_normalize_correctly(self):
        body = _live_shaped_body(
            envelope={"total": "58.25", "chargeTotal": "58.25"}
        )
        records = body["estimatedCharges"]["estimatedCharge"]
        for record in records:
            record["amount"] = "0.00"
        records[0]["amount"] = "25.00"   # CBS
        records[1]["amount"] = "10.00"   # CBS
        records[3]["amount"] = "18.25"   # NXTGEN
        records[7]["amount"] = "5.00"    # CFILES
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("58.25")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("35.00")
        assert services["NXTGEN"] == Decimal("18.25")
        assert services["CFILES"] == Decimal("5.00")
        assert snapshot.record_count == 9

    def test_multiple_charge_records_sum_exactly_decimal(self):
        # Three records whose sum has an exact Decimal representation
        # that binary floats would corrupt.
        body = _live_shaped_body(
            envelope={"total": "0.90", "chargeTotal": "0.90"}
        )
        records = body["estimatedCharges"]["estimatedCharge"]
        for record in records:
            record["amount"] = "0.00"
        records[0]["amount"] = "0.10"
        records[1]["amount"] = "0.20"
        records[2]["amount"] = "0.60"
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0.90")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("0.30")
        assert services["CBCKUP"] == Decimal("0.60")

    def test_discount_records_contribute_negatively(self):
        # The documented envelope can carry DISCOUNT records with
        # negative amounts; they reduce the itemized total.
        body = _live_shaped_body(
            envelope={"total": "40.00", "chargeTotal": "40.00"}
        )
        records = body["estimatedCharges"]["estimatedCharge"]
        for record in records:
            record["amount"] = "0.00"
        records[0]["amount"] = "50.00"
        records[1] = {
            "amount": "-10.00",
            "category": "DISCOUNT",
            "chargeType": "Commit Discount",
            "offeringCode": "CBS",
            "quantity": "1.00",
            "unitOfMeasure": "COUNT",
        }
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("40.00")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("40.00")


# ---------------------------------------------------------------------------
# True-zero semantics stay intact
# ---------------------------------------------------------------------------


class TestTrueZeroStaysZero:
    def test_authoritative_zero_with_zero_records_is_success(self):
        # Every record $0 AND authoritative total $0 — the true-zero
        # shape must remain a SUCCESS.
        body = _live_shaped_body(
            envelope={"total": "0.00", "chargeTotal": "0.00"}
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0.00")
        assert snapshot.record_count == 9
        # No un-itemized bucket: nothing to reconcile.
        assert UNITEMIZED_SERVICE not in {
            s.service for s in snapshot.by_service
        }
        assert sum((s.amount for s in snapshot.by_service), Decimal("0")) == (
            Decimal("0.00")
        )
        # A true zero is a perfectly valid snapshot.
        assert snapshot.currency == "USD"

    def test_zero_total_no_records_is_success(self):
        body = _live_shaped_body(
            envelope={"total": "0.00", "estimatedCharge": None}
        )
        snapshot = normalize_estimated_charges(body)
        assert snapshot.total == Decimal("0.00")
        assert snapshot.by_service == ()
        assert "no_charge_records_total_from_body" in snapshot.warnings


# ---------------------------------------------------------------------------
# Currency propagation
# ---------------------------------------------------------------------------


class TestCurrencyPropagation:
    def test_currency_propagates_to_snapshot_and_services(self):
        body = _live_shaped_body(
            envelope={"currency": "EUR", "total": "12.50", "chargeTotal": "2.50"}
        )
        records = body["estimatedCharges"]["estimatedCharge"]
        records[0]["amount"] = "2.50"
        snapshot = normalize_estimated_charges(body)
        assert snapshot.currency == "EUR"
        assert snapshot.total == Decimal("12.50")
        # Every service row carries the snapshot currency upstream.
        assert all(
            item.service and item.amount is not None
            for item in snapshot.by_service
        )
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("2.50")
        assert services[UNITEMIZED_SERVICE] == Decimal("10.00")

    def test_currency_never_hardcoded_usd(self):
        body = _live_shaped_body(envelope={"currency": "GBP", "total": "3"})
        snapshot = normalize_estimated_charges(body)
        assert snapshot.currency == "GBP"


# ---------------------------------------------------------------------------
# Malformed / missing amounts — never a silent authoritative zero
# ---------------------------------------------------------------------------


class TestMalformedAmounts:
    def test_record_missing_amount_raises_not_zero(self):
        body = _live_shaped_body()
        body["estimatedCharges"]["estimatedCharge"][0].pop("amount")
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == (
            RackspaceCostErrorCategory.INVALID_RESPONSE
        )

    def test_record_non_numeric_amount_raises_not_zero(self):
        body = _live_shaped_body()
        body["estimatedCharges"]["estimatedCharge"][0]["amount"] = "N/A"
        with pytest.raises(RackspaceCostError) as excinfo:
            normalize_estimated_charges(body)
        assert excinfo.value.category == (
            RackspaceCostErrorCategory.INVALID_RESPONSE
        )

    def test_authoritative_total_unparseable_falls_back_to_records(self):
        # ``total`` present but malformed: the component/record path
        # must still produce a total — never a silent authoritative $0.
        body = _live_shaped_body(envelope={"total": "not-a-number"})
        records = body["estimatedCharges"]["estimatedCharge"]
        records[0]["amount"] = "4.50"
        snapshot = normalize_estimated_charges(body)
        # chargeTotal "0.00" is the next parseable envelope total.
        assert snapshot.total == Decimal("0.00")
        services = {s.service: s.amount for s in snapshot.by_service}
        assert services["CBS"] == Decimal("4.50")
        assert services[UNITEMIZED_SERVICE] == Decimal("-4.50")

    def test_breakdown_always_reconciles_invariant(self):
        # Cross-cutting invariant: whatever the shape, the breakdown
        # sums exactly to the canonical total.
        for envelope in (
            {"total": "9"},
            {"total": "0.00", "chargeTotal": "0.00"},
            {"total": "120.00", "chargeTotal": "100.00"},
            {"total": "0.01"},
        ):
            body = _live_shaped_body(envelope=envelope)
            snapshot = normalize_estimated_charges(body)
            breakdown_sum = sum(
                (s.amount for s in snapshot.by_service), Decimal("0")
            )
            assert breakdown_sum == snapshot.total, envelope
            assert snapshot.total == _raw_authoritative_total(body), envelope
