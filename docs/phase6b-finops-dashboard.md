# Phase 6B — Real AWS FinOps Dashboard

## Overview

Phase 6B converts the HipLink-style professional dashboard shell from
Phase 6A into an **operational AWS FinOps dashboard** by wiring every
surface to real Phase 1–3 backend data. No fake spend, savings,
utilization, or resource counts. The Phase 6A visual design,
navigation, theme tokens, RBAC, and centralized API client are
preserved unchanged.

This document captures the data sources, endpoint mapping, component
surface, theme behavior, and limitations of Phase 6B.

## Authoritative Data Sources

Phase 6B is a strict consumer of the existing backend. The frontend
never invents numbers that AWS did not supply.

| Concern              | Backend (Phase 1–3)        | Frontend consumer                |
| -------------------- | --------------------------- | -------------------------------- |
| AWS identity         | `/api/aws/identity`        | Page header account / region     |
| EC2 / EBS / EIP / NAT / ELBv2 / RDS / Lambda / S3 | `/api/aws/resources` | Resources page inventory + table |
| Cost total + period  | `/api/aws/costs?days=`      | Dashboard / Costs KPIs           |
| Daily cost trend     | `/api/aws/costs`            | Cost Trend chart                 |
| Cost by service      | `/api/aws/costs`            | Cost by Service bars             |
| Cost by region       | `/api/aws/costs`            | Cost by Region bars              |
| CloudWatch utilization | `/api/aws/utilization`    | Per-resource utilization panel   |
| Optimization capabilities | `/api/aws/optimization/capabilities` | Capability panel       |
| Optimization summary | `/api/aws/optimization/summary` | Dashboard optimization summary |
| Optimization recommendations | `/api/aws/optimization/recommendations` | Optimization table |

## Lookback Filter

Only the four backend-supported lookback windows are exposed: 7, 30,
60, 90 days. The selector is `7d · 30d · 60d · 90d`. The default is
30 days. The Dashboard, Costs, and Optimization pages share the same
period via the `FinopsPeriodProvider` context so a selection on one
page flows to all three.

## Region Filter

The Costs page exposes a Region selector. Options are derived from the
`by_region` data observed in the cost report plus a default "All /
account-level" entry. Entries with `region === "global"` or
`"no_region"` are relabelled to **"Global / No Region"** rather than
dropped silently — Cost Explorer emits them for global services and
they MUST be visible.

## Dashboard Header

The HipLink-style header is preserved. The placeholder values from
Phase 6A are now populated from real data:

| Field         | Source                                  | Display                |
| ------------- | --------------------------------------- | ---------------------- |
| Account       | `/api/aws/identity.account`             | `9740…2038` (masked)   |
| Region        | `/api/aws/resources.region` or identity | `us-east-1`            |
| Status        | derived from cached cost entry           | `Live` / `Loading` / `Unavailable` |
| Last updated  | `cached_at` or current fetch time       | `10:42 AM`             |

Account id is masked in the UI (`maskAccountId` in `lib/format.ts`).
The full account id is only used internally for cache key derivation.

## KPI Definitions

### KPI 1 — AWS Spend

* **Source**: `report.total_cost`, `report.currency`.
* **Display**: compact currency (`$2.44K` for 2438.08) with the
  selected period label and currency.
* **Failure mode**: backend 5xx → KPI renders `—` (placeholder) and a
  partial-success banner is shown at the top of the dashboard.

### KPI 2 — Cost Change

* **Source**: `report.previous_period_cost`, `report.change_percent`,
  `report.change_amount`.
* **Display**: `+5.0%` / `−7.2%` etc with `vs previous N days` label
  plus the absolute change (`+$138.08`).
* **Failure mode**: when `previous_period_cost === 0`, `change_percent`
  is `null` (the backend never divides by zero). The UI renders `—`
  in that case — never `0%` and never `Infinity%`.

### KPI 3 — Resources

* **Source**: `/api/aws/resources` — sum of `items.length` across
  every service whose `status` is `ok` and which returned at least
  one item (or was queried). The dashboard does NOT claim multi-account
  support; the Phase 6A placeholder text **"Tracked across accounts"**
  is replaced with **"Across K resource types"**.
* **Display**: the integer count plus the resource-type count.

### KPI 4 — Optimize

* **Primary value**: `summary.total_recommendations` — the count of
  deduplicated recommendations.
* **Secondary**:
  * If `summary.total_estimated_monthly_savings` is non-null →
    render compact currency with `known savings / mo`.
  * If null → render **`Savings not available`** (never `$0`).

## Cost Trend Chart

`CostTrendChart.tsx` is an inline SVG area+line chart driven entirely
by semantic Tailwind tokens (`stroke-border`, `stroke-primary`,
`fill-primary-soft`, `fill-fg-muted`). It accepts the `daily_trend`
array from the cost report and renders:

* X-axis: ISO date, with sparse labels for longer periods.
* Y-axis: USD amount, always starting at 0 so comparisons are honest.
* Tooltip: date + exact formatted cost, positioned over the focused
  datapoint via mouse / keyboard arrow keys.
* Screen-reader mirror: a `<table class="sr-only">` with every
  date / cost pair — see `accessibility` below.

The chart works under dark + light themes without separate palettes
because every color is read from a CSS variable at render time.

## Cost by Service / Cost by Region

`HorizontalBarChart.tsx` is a generic ranked bar list. Used twice on
the Costs page (Service + Region). Tail rows collapse into
**"Other"** when the row count exceeds `maxRows` (default 10).
Percentage share is computed against the visible total only when
the denominator is non-zero — null entries render as `—`.

## Resources Page

* Resource-type summary cards (EC2 / EBS / EIP / NAT / ELBv2 / RDS /
  Lambda / S3) with item counts and `ServiceStatus` pills.
* Operational data table with type-specific safe columns:
  * EC2 → instance type, state
  * EBS → size, attachment count, state
  * EIP → association id (or `unassociated`)
  * NAT → state
  * ELBv2 → type (ALB / NLB)
  * RDS → engine, class, status
  * Lambda → runtime
  * S3 → region
* Client-side filters: Type, Region, Search-by-name-or-id. No
  pagination for the MVP — the table caps at 200 rows with a "showing
  first 200" hint.
* A clickable row reveals CloudWatch utilization for that resource
  via `POST /api/aws/utilization`. Missing metrics render `No data` —
  never `0`.

## Utilization UX

`UtilizationPanel.tsx` groups CloudWatch metric series by metric name
and renders type-aware labels:

* EC2 → CPU avg / max, network in, network out
* RDS → CPU, connections, freeable memory
* Lambda → invocations, duration, errors, throttles
* ELBv2 → request count, processed bytes

Missing metrics render `No data` per spec. The Phase 6A spec was
emphatic that `No data` and `0` are different states; we render the
former explicitly so the user understands CloudWatch returned no
datapoints rather than the resource being perfectly idle.

## Optimization Page

* Five KPI cards: Total Recommendations, High Confidence, AWS-native
  (from Compute Optimizer + Cost Optimization Hub), Deterministic
  (UNKNOWN source from the in-house engine), Known Savings.
* Capability panel showing Compute Optimizer / Cost Optimization Hub /
  Deterministic Engine enrollment state. **InActive** / **Not
  enrolled** / **Available** are rendered as informational statuses,
  not as errors.
* Recommendation table with resource, type, region, finding, source
  badge, confidence badge, and estimated savings. Null savings render
  **`Not available`**. Authoritative savings render `$X.XX / mo`.
* Clicking a row opens a detail panel showing current / recommended
  configuration, evidence data, reason codes, and source breakdown.
  No AI explanation (that's Phase 6C).

## Savings Source Badges

Compact status badges distinguish:

* `AWS Cost Optimization Hub` (info tone)
* `AWS Compute Optimizer` (info tone)
* `Calculated` (success tone)
* `Deterministic` (ai tone — for our in-house UNKNOWN-sourced rules)
* `Unknown` (neutral)

Deterministic recommendations do NOT carry the AWS brand color; the
spec was emphatic that they must not be confused with official AWS
recommendations.

## Data States

Distinct render paths:

| State              | Where it surfaces                       |
| ------------------ | ---------------------------------------- |
| Loading            | `LoadingSkeletonRows` or KPI `—`        |
| Empty (no data)    | `EmptyState` titled "No data"           |
| Partial-success    | `PartialWarning` banner; data still rendered |
| Failed (5xx)       | `ErrorState` per panel; other panels unaffected |
| Not-enrolled       | Capability row shows `Not enrolled` as informational |

A slow CloudWatch or optimization call never blanks the Costs page;
each panel loads independently via the shared in-memory FinOps store.

## Refresh

A `RefreshButton` invalidates the in-memory store via `invalidateCache()`
and re-fetches every active query. The backend cache controls AWS API
cost — Cost Explorer responses are cached on the server (Phase 2 cost
cache) so repeated refreshes do not generate new AWS API calls within
the cache TTL.

## Data Fetching Architecture

* `frontend/src/lib/finops/{identity,costs,resources,utilization,optimization}.ts`
  — typed service modules that call `apiFetch` against the
  Phase 6A centralized client.
* `frontend/src/lib/finops/store.ts` — tiny in-memory query cache with
  deduplication per `(cacheKey, ttl)` tuple. Components subscribe via
  `useFinopsQuery(cacheKey, fetcher, options)`. When `cacheKey`
  changes (e.g. user picks a different period), the hook swaps to the
  matching entry automatically.
* `frontend/src/lib/finops/period.tsx` — `FinopsPeriodProvider`
  exposes `days` and `region` via React context. Dashboard, Costs,
  Optimization, and Resources all consume it so a single change
  updates every panel.
* Components do NOT call `fetch()` directly. The Phase 6A `apiFetch`
  client is the only place HTTP happens for browser code.

## Type Safety

`frontend/src/types/finops.ts` declares interfaces that mirror the
backend Pydantic schemas exactly: `AwsIdentity`, `CostReport` /
`CostReportResponse`, `ResourcesResponse`, `UtilizationResponse`,
`CapabilitiesResponse`, `RecommendationsResponse`,
`OptimizationSummaryResponse`. Decimal amounts arrive as strings
from the backend (precision preservation); the formatters in
`lib/format.ts` parse them via `parseDecimal`.

## Dark + Light Themes

Every Phase 6B surface uses semantic tokens (`bg-surface`,
`text-fg-primary`, `border-border`, `fill-primary`, `stroke-primary`,
etc). The Phase 6A token system handles theme switching automatically;
no component knows about colors directly. Charts, bars, axes, labels,
tooltips, table headers, table hover, filters, status badges,
recommendation confidence, and cost-change tones all flip
automatically when `data-theme` changes.

## Positive / Negative Cost Colors

* Cost decrease → `success` tone (green).
* Cost increase → `warning` tone (amber). We do NOT use `danger` for
  cost growth — the spec explicitly said never to imply every cost
  increase is inherently bad. `danger` is reserved for failed
  capabilities (`Failed` / `Access denied` / `Unavailable`).
* Copy uses neutral wording: `Increase` / `Decrease` rather than
  `Critical` / `Warning`.

## Accessibility

* Cost Trend chart exposes a visually-hidden `<table>` with every
  date / cost pair so screen readers can read the series.
* Tooltips are reachable via keyboard arrow keys (rect hit-targets
  with `tabIndex={0}` and `onFocus` handler).
* Period selector is `role="radiogroup"` with `aria-checked`.
* Region filter is a native `<select>`.
* All status / savings badges pair a color with text — color is
  never the sole state carrier.
* PartialWarning uses `role="status"`; `EmptyState` uses `role="status"`;
  `ErrorState` uses `role="alert"`.

## Responsive Layout

HipLink desktop-first layout is preserved. Charts resize via
`preserveAspectRatio="none"`. KPI cards wrap on small screens via
`sm:grid-cols-2 lg:grid-cols-4`. Tables scroll horizontally on small
screens. The mobile navigation drawer from Phase 6A is unchanged.

## Security

* AWS credentials, LiteLLM keys, JWT secrets, and any backend
  endpoint with `/api/aws/cost-cache` are NEVER referenced from
  frontend source.
* No localhost, private IP, or absolute backend host is hardcoded
  anywhere — the browser uses nginx at the current origin for `/api/`.
* Account id is masked in the UI.
* The bearer token is read by the existing Phase 5A auth provider and
  attached by `apiFetch`; Phase 6B adds no token handling.

## Limitations

* Pagination: Resource inventory caps at 200 rows (first-N). A
  future iteration can add cursor-based pagination.
* Per-resource utilization is fetched on demand when a user clicks a
  row. Pre-loading all CloudWatch metrics for every resource would be
  expensive and is deferred.
* Real AWS-native savings figures depend on Compute Optimizer /
  Cost Optimization Hub enrollment, which is **Inactive / Not
  enrolled** in the current account. The dashboard renders this as
  informational and falls back to deterministic recommendations from
  the in-house engine. Re-enrolling these services will make
  authoritative savings figures appear automatically with no
  frontend change.
* Cost Explorer daily trend for periods < 7 days is not supported
  by the backend. The PeriodSelector defaults to 30 days so this is
  not user-facing.

## Deferred to Phase 6C

* AI conversation UI / WebSocket client / conversation history panel.
  The `/analyst` route remains a Phase 6A shell.
* Streaming AI responses.

## Deferred to Phase 8

* Custom HipLink domain + TLS certificate.
* Official HipLink production logo.

## Files Added / Modified

```
frontend/src/types/finops.ts                  (NEW — typed surface)
frontend/src/lib/format.ts                   (NEW — presentational formatters)
frontend/src/lib/finops/store.ts              (NEW — in-memory query cache)
frontend/src/lib/finops/period.tsx            (NEW — shared period context)
frontend/src/lib/finops/identity.ts           (NEW — /api/aws/identity wrapper)
frontend/src/lib/finops/costs.ts              (NEW — /api/aws/costs wrapper)
frontend/src/lib/finops/resources.ts          (NEW — /api/aws/resources wrapper)
frontend/src/lib/finops/utilization.ts        (NEW — /api/aws/utilization wrapper)
frontend/src/lib/finops/optimization.ts       (NEW — /api/aws/optimization/* wrappers)
frontend/src/components/CostTrendChart.tsx    (NEW — SVG daily trend chart)
frontend/src/components/HorizontalBarChart.tsx (NEW — generic ranked bars)
frontend/src/components/PeriodSelector.tsx    (NEW — 7d/30d/60d/90d)
frontend/src/components/RegionFilter.tsx      (NEW — region select)
frontend/src/components/PartialWarning.tsx     (NEW — PARTIAL_SUCCESS banner)
frontend/src/components/RefreshButton.tsx     (NEW — cache invalidator)
frontend/src/components/UtilizationPanel.tsx  (NEW — type-aware CW metrics)
frontend/src/components/CapabilityStatusRow.tsx (NEW — CO / COH / DE row)
frontend/src/components/RecommendationSourceBadge.tsx (NEW — savings source / confidence)
frontend/src/components/DataTable.tsx         (MODIFIED — added onRowClick / isRowClickable)
frontend/src/components/FilterBar.tsx         (MODIFIED — added testId)
frontend/src/pages/DashboardPage.tsx          (REWRITE — wired to real data)
frontend/src/pages/CostsPage.tsx              (REWRITE — wired to real data)
frontend/src/pages/ResourcesPage.tsx          (REWRITE — wired to real data)
frontend/src/pages/OptimizationPage.tsx        (REWRITE — wired to real data)
frontend/src/App.tsx                          (MODIFIED — wrap with FinopsPeriodProvider)
frontend/src/tests/format.test.ts             (NEW)
frontend/src/tests/finops-store.test.tsx      (NEW)
frontend/src/tests/charts.test.tsx            (NEW)
frontend/src/tests/badges.test.tsx            (NEW)
frontend/src/tests/period-selector.test.tsx   (NEW)
frontend/src/tests/dashboard.test.tsx         (NEW)
frontend/src/tests/costs-page.test.tsx        (NEW)
frontend/src/tests/resources-page.test.tsx     (NEW)
frontend/src/tests/optimization-page.test.tsx  (NEW)
scripts/phase6b_verify.sh                     (NEW — Phase 6B verifier)
docs/phase6b-finops-dashboard.md              (NEW — this document)
docs/phase6b-report.md                         (NEW — closure report)
```

Zero backend changes. The Phase 1–3 APIs already exposed everything
Phase 6B needed.
