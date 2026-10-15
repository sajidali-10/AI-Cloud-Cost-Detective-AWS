# Phase 7C.2B — Azure Cost Management Integration

This document describes the Azure Cost Management integration shipped
in Phase 7C.2B.  It covers the API architecture, period semantics,
query shapes, currency policy, pagination, cache isolation, security,
and live-validation results.

## API Architecture

Phase 7C.2B introduces a **canonical provider-neutral cost surface**
alongside the existing AWS legacy routes.

| Route | Provider | Status |
|-------|----------|--------|
| `/api/aws/costs` | AWS | Legacy (Phase 7A / 7B / 7C.1) — preserved |
| `/api/cloud/costs` | AWS / AZURE | Canonical (Phase 7C.2B) |

The canonical route is mounted on the existing `aws_router` with the
`/cloud` prefix; after Nginx strips `/api/` the public URL is
`/api/cloud/costs`.

### Provider dispatch

The canonical route resolves the requested provider via the
`ProviderRegistry`:

1. AWS → `AWSCloudProvider.get_cost_summary(...)` (delegates to the
   existing Phase 7B multi-account aggregator).
2. AZURE → `AzureCloudProvider.get_cost_summary(...)` (delegates to
   `app.services.cloud.azure.cost_aggregator`).

No raw Azure payload ever leaks past the adapter layer; the adapter
returns the provider-neutral `CloudCostSummary` shape, which the route
serialises into a `CloudCostReportResponse` envelope.

## Microsoft Cost Management API

* **Endpoint:** `POST https://management.azure.com/subscriptions/{id}/providers/Microsoft.CostManagement/query?api-version=2026-06-01`
* **API version:** `2026-06-01` (pinned in
  `app/services/cloud/azure/costs.py` — `COST_MGMT_API_VERSION`).
* **Authentication:** reused from Phase 7C.2A's workload-identity
  federation pipeline — `azure-identity` `ClientAssertionCredential`
  backed by an AWS STS `GetWebIdentityToken` call.  **No client
  secrets; no certificates.**  The Entra access token is acquired
  per call through `acquire_access_token()` and never persisted.
* **Cost type:** `ActualCost`.  Pinned in
  `COST_TYPE_ACTUAL`.  This matches the AWS dashboard's
  `UnblendedCost` semantic — the operator sees unbilled + billed
  charges as they accrue.  `AmortizedCost` is intentionally not
  used because its data window (today minus 7 days) would conflict
  with the calendar-month semantics.
* **Granularity:** `None` (for totals / service / region groupings)
  or `Daily` (for the trend query).
* **Dimensions:** `ServiceName` and `ResourceLocation`.  The dataset
  is built dynamically — see `_build_grouped_dataset`.

### Response shape

The Microsoft Cost Management Query API returns:

```json
{
  "properties": {
    "columns": [
      {"name": "PreTaxCost", "type": "Number"},
      {"name": "UsageDate", "type": "DateTime"},
      {"name": "ServiceName", "type": "String"},
      {"name": "ResourceLocation", "type": "String"},
      {"name": "Currency", "type": "String"}
    ],
    "rows": [...],
    "nextLink": "..."
  }
}
```

Columns are resolved **dynamically** by name (`UsageDate`,
`ServiceName`, `ResourceLocation`, `Currency`, `PreTaxCost`).
No positional indices are assumed.  When the API returns a different
canonical name (e.g. `BillingPeriodStartDate` instead of `UsageDate`
for invoice-aligned data), the parser falls back gracefully.

## Period semantics

The Azure portal defaults to a **billing month** window
(28th-of-prior-month through 27th-of-current-month).  This portal is
calendar-month-only — it must match the AWS dashboard's
`September 2026 = [2026-09-01, 2026-10-01]` semantics exactly.

The mapping is enforced inside `build_time_period`:

| UI selection | Timeframe | Boundaries |
|--------------|-----------|------------|
| Current month | `MonthToDate` | server picks the current calendar month |
| Any other (completed) month | `Custom` | explicit UTC boundaries matching the calendar month |

Example — September 2026 with `today=2026-10-15`:

* Current month: `MonthToDate` → server returns `[2026-10-01, 2026-10-15]`
* Prior month: `Custom` with `from=2026-09-01T00:00:00Z`, `to=2026-10-01T00:00:00Z`

## Query shapes

### Total cost (current period)

```python
AzureCostClient.query(
    subscription_id="...",
    dataset={
        "granularity": None,
        "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
    },
    timeframe="MonthToDate",  # or "Custom" with explicit boundaries
)
```

### Daily trend

```python
dataset={
    "granularity": "Daily",
    "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
}
```

### Cost by Service

```python
dataset={
    "granularity": None,
    "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
    "grouping": [{"type": "Dimension", "name": "ServiceName"}],
}
```

### Cost by Region / Location

```python
dataset={
    "granularity": None,
    "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
    "grouping": [{"type": "Dimension", "name": "ResourceLocation"}],
}
```

`ResourceLocation` returns Azure region codes (`eastus`, `westeurope`,
...) plus blank / `null` values for unassigned resources.  Blank rows
are bucketed under `"Unassigned"` so cost is never silently dropped.

## Status semantics

`AzureCostCallStatus` is the **public** contract returned by every
call:

| Status | Meaning | UI renders |
|--------|---------|------------|
| `OK` | Azure returned numeric cost (> 0 or = 0) | normal numbers |
| `NO_DATA` | Cost Management returned 204 No Content | "Connected but no cost data" |
| `EMPTY` | 200 with empty rows | empty dashboard with neutral state |
| `EMPTY_FOR_PERIOD` | 200 with rows but no rows in the requested window | empty dashboard |

204 is **distinct** from a numeric `$0` — the spec requires it.  A
204 response produces `status="NO_DATA"`; an explicit zero produces
`status="OK"` with `total_cost="0.00"`.

## Error mapping

Every non-2xx response is folded into a sanitised
`AzureCostErrorCategory`:

| HTTP | Category | HTTP response | Notes |
|------|----------|---------------|-------|
| 401 | `AUTH_FAILED` | 401 | Entra token rejected |
| 403 | `ACCESS_DENIED` | 403 | RBAC missing |
| 404 | `NOT_FOUND` | 404 | Subscription not visible to the SP |
| 429 | `THROTTLED` | 429 | `Retry-After` propagated as response header |
| 5xx | `UPSTREAM_ERROR` | 502 | Upstream failure |
| network | `NETWORK_ERROR` | 502 | Connection / timeout |
| bad JSON | `INVALID_RESPONSE` | 502 | Schema drift |

`Retry-After` is parsed and surfaced both as `exc.retry_after` and
as a response `Retry-After` header so a caller can back off
deterministically.  No unbounded retry loops are constructed inside
this layer.

## Pagination

Cost Management paginates large responses via
`properties.nextLink` — an absolute URL pointing at the same ARM
management endpoint.  The client:

* follows `nextLink` verbatim (it is authoritative),
* validates the URL still points at `https://management.azure.com/...`
  before each follow (defence against an unexpected 302 chain or
  compromised response),
* bounds the loop with `MAX_PAGES = 50` so a pathological response
  cannot drive an unbounded request chain,
* refuses to concatenate rows when consecutive pages return
  different column shapes (schema-drift detection).

## Currency policy

The aggregator **refuses to silently combine different currencies**.
The rollup rule:

* every successful subscription reports the same currency → totals
  are summed normally,
* no subscription succeeded → `currency_status="EMPTY"`,
* currencies disagree → `currency_status="MIXED_CURRENCY"`,
  `total_cost="0"`, and the API route renders an explicit
  "mixed currency" UI state.

**No FX conversion is ever invented.**  The contract is documented
in `AzureCostErrorCategory` and tested in
`test_phase7c2b_azure_cost_aggregator.py`.

## Cache isolation

The canonical route's cache key includes **every** dimension that
affects the response — `user`, `provider`, `scope`, `region`,
`kind`, `signature`.  Concretely:

```
v4|<provider>|<scope>|user=<id>|<region>|<kind>|<signature>
```

* AWS and Azure keys never collide (the `provider` segment dominates).
* `all` and a specific account / subscription never collide (the
  `scope` segment dominates).
* Different users, regions, periods, and dimensions all produce
  distinct keys.

The legacy AWS route keeps its Phase 7A / 7B cache-key shape
(`v3|scope|user=N|region|kind|signature`) so existing Phase 7B
regression tests do not need to invalidate.  The Phase 7C.2B
isolation is therefore enforced via the canonical route's cache key;
the AWS legacy route remains untouched.

## Security

The Azure cost client follows the same sanitisation contract as the
Phase 7C.2A subscription validator:

* `Authorization: Bearer …` headers are never logged.
* Entra access tokens / client assertions / STS federation JWTs are
  never echoed in exception messages or response bodies.
* Subscription IDs are truncated to the first 8 hex characters via
  `safe_subscription_label` before logging.
* Response payloads are never echoed in errors — only the status
  category and the safe subscription label.
* `scrub_log_payload` is applied to every string going to the log
  stream from Azure-side code paths.

The test
`test_phase7c2b_azure_cost_client.TestClientSanitisation` enforces
this contract.

## MCPP vs Sponsorship behaviour

The spec requires the UI to **distinguish** "connected but no cost
data" from "authentication failed".  The two subscriptions
exhibit different runtime behaviour against the real Microsoft
tenant:

* **MCPP Subscription** — RBAC is in place.  Cost Management returns
  numeric cost for both current and prior periods.
* **Microsoft Azure Sponsorship** — the Sponsorship's billing
  account behaves differently; Cost Management returns 204 for
  some windows and an empty result for others.

The aggregator surfaces this distinction as:

* `status="OK"` with `total_cost="0.00"` — Cost Management returned
  rows that summed to zero (cost really is zero for this period),
* `status="NO_DATA"` — Cost Management returned 204 (no data
  available for this window),
* `account_errors[i].category="THROTTLED"` — the SP is being
  rate-limited; the per-subscription attribution is preserved.

The frontend renders these distinctly:

* numeric cost → normal dashboard,
* `NO_DATA` → "Connected but no cost data — Cost Management has no
  rows for this subscription and period",
* `THROTTLED` → "Subscription temporarily rate-limited — Retry-After
  honoured".

## AWS regression status

The AWS legacy route (`/api/aws/costs`) is untouched by Phase 7C.2B.
Live validation confirms:

* `AWS QA-Dev` account — returns numeric cost in USD via the
  existing Phase 7B aggregator.
* `AWS Production` account — returns numeric cost in USD via the
  same aggregator.
* `All AWS Accounts` — multi-account rollup with the Phase 7B
  per-account attribution preserved.
* Calendar-month semantics — `month=2026-09` returns
  `[2026-09-01, 2026-10-01]` UTC boundaries identical to the
  pre-Phase-7C.2B behaviour.
* Daily trend, cost-by-service, region filter — all unchanged.

The cost cache key for AWS is still the legacy
`v3|<scope>|user=<id>|<region>|<kind>|<signature>` shape; Phase 7C.2B
does not invalidate any cached Phase 7B responses.

## Live validation summary

| Scenario | Status | Total | Currency | Source |
|----------|--------|-------|----------|--------|
| MCPP Subscription — current month (October 2026 MTD) | SUCCESS | $6.89 | USD | AZURE_COST_MANAGEMENT |
| MCPP Subscription — previous month (September 2026) | SUCCESS | numeric | USD | AZURE_COST_MANAGEMENT |
| Sponsorship — current month | SUCCESS / EMPTY | $0.00 | USD | AZURE_COST_MANAGEMENT |
| Sponsorship — previous month | SUCCESS / EMPTY | $0.00 | USD | AZURE_COST_MANAGEMENT |
| All Azure Subscriptions — current month | SUCCESS | numeric (per-sub attribution) | USD | AZURE_COST_MANAGEMENT |
| AWS QA-Dev — current month | SUCCESS | numeric | USD | AWS_COST_EXPLORER |
| All AWS Accounts — current month | SUCCESS | $2710.05 | USD | AWS_COST_EXPLORER |

See `scripts/phase7c2b_live_validation.sh` for the executable form.

## What Phase 7C.2B does NOT ship

* Azure Resource Graph inventory — Phase 7C.2C.
* Azure Advisor / optimization — Phase 7C.2D.
* Rackspace provider — not part of 7C.2B.
* FX conversion — explicitly refused; mixed-currency state is
  surfaced instead.
* Unbounded retry loops — `MAX_PAGES=50` is a hard ceiling.
