# Phase 7D.3.1 — AWS Canonical Analytics Completeness

**Branch:** `phase-7d3-unified-executive-dashboard`
**Type:** Narrow backend completeness pass (no Azure changes; no dashboard redesign)
**Status:** Complete — release-ready

---

## 1. Purpose

Phase 7D.3 acceptance validation (de39e9c) was green, but the live canonical AWS envelopes surfaced two completeness gaps:

1. **AWS canonical cost `daily_trend`** — always empty.
2. **AWS canonical resources `by_type`** — empty even when `resources[]` contained hundreds of items.

Both gaps did not break the API contract, but they reduced dashboard quality and would have limited Phase 7D.4 AI FinOps analysis. Phase 7D.3.1 closes both without fabricating data.

---

## 2. AWS Daily Trend

### 2.1 Audit

The Cost Explorer query layer (`backend/app/services/aws/cost_aggregator.py`) already produces real per-day data. `aggregate_cost_report_for_context` returns `MultiAccountCostReport.daily` populated from `get_daily_trend(client, period, region=region)` (lines 398–434 of the aggregator).

The AWS adapter (`backend/app/services/cloud/aws/adapter.py`) already implements `get_cost_trend(...)` which returns a `CloudCostTrend` carrying the same per-day points. This is the path the legacy `/api/aws/costs` route already uses (indirectly via the legacy aggregator and cache).

The canonical `_build_aws_envelope` in `backend/app/api/cloud_costs.py` simply never plumbed this through. Before Phase 7D.3.1 it accessed `summary.daily` which doesn't exist on `CloudCostSummary`, causing an AttributeError. Phase 7D.3 (de39e9c) suppressed that error to `daily_trend=[]` as a release-safe fallback; this left `daily_trend` empty.

### 2.2 Implementation

Phase 7D.3.1 mirrors the existing Azure pattern:

* `_loader()` in `cloud_costs.py` now calls `adapter.get_cost_trend(...)` for AWS alongside `get_cost_summary(...)`, identical to how Azure already calls `aggregate_cost_trend_for_subscriptions`.
* `_build_aws_envelope(...)` accepts a `daily_trend` parameter and maps each `CloudServiceCost` to a canonical `DailyCostPoint` using the existing `_daily_date_for_aws(...)` helper that reads `extensions["date"]`.
* A trend fetch failure MUST NOT 502 the envelope: the call is wrapped in `try/except`. The failure is logged and surfaced through `account_errors` as `code="DailyTrendUnavailable"` so the dashboard-level partial-data banner picks it up.

### 2.3 Multi-account semantics

`get_cost_trend` delegates to `aggregate_cost_report_for_context` which:

* Fetches per-account daily points via `get_daily_trend`.
* Sums by date across successful accounts (`daily_accum`).
* Skips failed accounts (they land in `account_errors` instead).
* Sets `status="PARTIAL_SUCCESS"` when any account failed.

The canonical envelope faithfully surfaces this — successful days are preserved, failed accounts are surfaced as `account_errors`, `status` propagates.

### 2.4 No fabrication

The implementation does NOT:

* Divide monthly totals across days.
* Estimate missing days from the period.
* Pad missing dates with zero.

If the upstream `get_cost_trend` returns an empty list, the canonical envelope returns `daily_trend=[]` and the Dashboard renders its existing "No data" empty state. If the trend fetch itself fails, the envelope still returns the summary (with `daily_trend=[]`) and surfaces the failure as a `DailyTrendUnavailable` partial-data signal.

---

## 3. AWS Resources `by_type`

### 3.1 Audit

The AWS canonical resources envelope (`backend/app/api/cloud_resources.py::_build_aws_envelope`) trusted `extensions.get("by_type", [])` from the aggregator. The aggregator did NOT populate that key, leaving `by_type=[]` even when `resources[]` had hundreds of items.

### 3.2 Implementation

Phase 7D.3.1 adds defence-in-depth derivation:

* The route walks the canonical `resources[]` list and counts by `resource_type`, `region`, and `account_id`.
* An explicit `"Unassigned"` bucket captures resources that lack a `resource_type` rather than silently dropping them.
* Aggregator-supplied breakdowns (if any) are merged on top so we never regress on richer upstream summaries.
* The aggregator-supplied `display_name` (when present in resource metadata) is preferred over the raw account id for `by_subscription.display_name`.

### 3.3 Invariants

* `sum(by_type counts) == total_resources` when every resource has a type.
* `sum(by_type counts) == total_resources` regardless when at least one resource has no type — the `Unassigned` bucket preserves them.
* `total_resources` is derived defensively: prefer the aggregator's figure, fall back to `len(resources)`.

---

## 4. Optional Safe Summaries

Phase 7D.3.1 also derives `by_location` and `by_subscription` server-side from the canonical `resources[]` list, using the same merge-with-aggregator pattern. No new AWS API calls were introduced — these are computed from data already in flight.

* `by_location` — keyed on `resource.region`. An empty `region` is bucketed as `"Unassigned"`.
* `by_subscription` — keyed on `resource.account_id`. Display name prefers `metadata["account_display_name"]` (when the aggregator stamped it), else falls back to the account id.

---

## 5. Azure Regression

Zero changes to the Azure path:

* `cloud_costs.py` Azure branch still calls `aggregate_cost_trend_for_subscriptions` (untouched).
* `cloud_resources.py` Azure branch still calls `aggregate_resources_for_subscriptions` (untouched).
* No Azure adapter, aggregator, or model changed.
* Verified live: `/api/cloud/costs?provider=AZURE&account_id=all&month=2026-10` returns the same shape and timing as before.

---

## 6. Frontend

Zero frontend changes required. The executive Dashboard already consumes `daily_trend` (Spend Trend chart) and `by_type` (Resource Summary section) through existing components (`CostTrendChart`, `HorizontalBarChart`, `ResourceSummaryCard`). The new data flows through automatically.

`docs/phase7d3-unified-executive-dashboard.md` documents the existing rendering paths; this pass does not modify that document because the consumer side is unchanged.

---

## 7. Live Results

### 7.1 AWS canonical cost envelope (live)

`GET /api/cloud/costs?provider=AWS&account_id=all&month=2026-10`

```json
{
  "status": "SUCCESS",
  "total_cost": "3252.54",
  "daily_trend": [
    {"date": "2026-10-01", "amount": "197.27", "unit": "USD"},
    ...
    {"date": "2026-10-18", "amount": "21.85", "unit": "USD"}
  ],
  "by_service": 18 entries,
  "by_region": 1 entry,
  "account_errors": []
}
```

* 18 daily points (one per day in the period).
* Per-day values are real Cost Explorer daily points, not monthly/30 fabrications.
* 18 by_service entries (real AWS services from the accounts).
* 1 by_region entry (us-east-1).

### 7.2 AWS canonical resources envelope (live)

`GET /api/cloud/resources?provider=AWS&account_id=all`

```json
{
  "summary": {
    "status": "SUCCESS",
    "total_resources": 401,
    "by_type": [
      {"key": "ebs", "count": 130},
      {"key": "lambda", "count": 96},
      {"key": "ec2", "count": 95},
      {"key": "s3", "count": 47},
      {"key": "eip", "count": 25},
      {"key": "nat", "count": 4},
      {"key": "rds", "count": 4}
    ],
    "by_location": [
      {"key": "us-east-1", "count": 400},
      {"key": "us-west-1", "count": 1}
    ],
    "by_subscription": [
      {"display_name": "852428675923", "count": 79},
      {"display_name": "974053642038", "count": 322}
    ],
    "resources": 401 items
  }
}
```

* `sum(by_type) == 130 + 96 + 95 + 47 + 25 + 4 + 4 == 401 == total_resources` ✅

---

## 8. AWS Legacy Endpoints

Phase 7D.3.1 does NOT touch the legacy `/api/aws/*` routes.

Verified live:

* `/api/aws/costs?month=2026-10&account_id=all` → HTTP 200 ✅
* `/api/aws/identity` → HTTP 200 ✅
* `/api/aws/resources/all` → HTTP 200 ✅ (slow upstream call but reachable)

---

## 9. Tests Added

`backend/tests/test_phase7d3_aws_analytics_completeness.py` — 13 tests:

1. `test_aws_daily_trend_populated_from_real_source` — envelope surfaces real per-day points.
2. `test_aws_daily_trend_values_not_fabricated` — daily values are NOT monthly/30 — asymmetric per-day values preserved.
3. `test_aws_daily_trend_multi_account_aggregates_by_date` — multi-account aggregator output preserved verbatim.
4. `test_aws_daily_trend_partial_account_failure_preserves_successful` — PARTIAL_SUCCESS keeps successful trend + failed account error.
5. `test_aws_daily_trend_empty_source_stays_empty` — empty upstream → empty canonical.
6. `test_aws_daily_trend_failure_does_not_502_envelope` — trend fetch failure → summary still returned, surfaced via partial-data banner.
7. `test_aws_by_type_derived_from_resources` — by_type derived from resources[].
8. `test_aws_sum_by_type_equals_total_resources` — invariant.
9. `test_aws_missing_resource_type_uses_unassigned_bucket` — explicit safe bucket.
10. `test_aws_by_subscription_derived_from_resources` — per-account counts.
11. `test_aws_by_location_derived_from_resources` — per-region counts.
12. `test_aws_resources_aggregator_summary_breaks_down_zero` — empty upstream → well-formed envelope.
13. `test_azure_daily_trend_still_uses_aggregator` — Azure path untouched.

All 13 pass.
