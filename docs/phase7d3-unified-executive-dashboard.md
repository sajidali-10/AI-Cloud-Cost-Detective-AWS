# Phase 7D.3 — Unified Multi-Cloud Executive Dashboard

**Branch:** `phase-7d3-unified-executive-dashboard`
**Ancestry:** bea38fd → 0c7fce1 → 6938362 → 43b6fae → 77f9279 → 5bea4dc → b753956 (HEAD)
**Type:** Frontend unification (no backend changes)
**Status:** Complete — release-ready

---

## 1. Goal

Replace the per-provider AWS / Azure executive dashboard implementations with one provider-aware Dashboard that renders for both clouds from the canonical Phase 7C / 7D.1 cross-cloud API surface.

The new Dashboard must:

* Render for AWS and Azure from the same component tree.
* React to Provider, Account/Subscription and Period from the existing global contexts.
* Consume exclusively `/api/cloud/costs`, `/api/cloud/resources`, `/api/cloud/recommendations`.
* Preserve all Phase 7C hotfix invariants (provider switch, subscription correctness, cost completeness, dashboard wording cleanup).
* Preserve the locked two-row navigation — Row 1 main, Row 2 Cloud Context.

---

## 2. Old architecture (pre-7D.3)

The pre-7D.3 Dashboard had two parallel implementations:

```
DashboardPage
├── AwsDashboard
│   ├── identity         → /api/aws/identity         (legacy)
│   ├── cost             → /api/aws/costs            (legacy)
│   ├── capabilities     → /api/aws/optimization/capabilities  (legacy)
│   ├── summary          → /api/aws/optimization/summary       (legacy)
│   └── resources        → /api/aws/resources       (legacy)
└── AzureDashboard
    └── cost             → /api/cloud/costs          (canonical)
    (no resources or recommendations — placeholder "—")
```

The two implementations diverged in three ways that hurt the executive experience:

1. **Stale AWS-only wording** — `AwsDashboard` carried a hardcoded `"AWS cost visibility, optimization and AI-powered FinOps analysis"` subtitle and `"AWS Spend"` KPI label even when rendered under the Azure branch in error scenarios.
2. **Duplicated layout logic** — ~600 lines of copy-pasted PageHeader + Hero + KPI grid + Trend/Service two-column + Summary two-column blocks.
3. **AWS-favoured data path** — only AWS hit the legacy Phase 3 routes (identity, capabilities, summary, resources), while Azure hit only one of the three canonical endpoints. Azure Resources KPI and Optimization KPI rendered static `"—"` placeholders.

The architecture also depended on an early `return` for the Azure branch, which threw a React "Rendered fewer hooks than expected" exception when the user toggled provider mid-session (the bug bea38fd fixed).

---

## 3. New architecture (Phase 7D.3)

```
DashboardPage
└── UnifiedDashboard                  ← single provider-aware component tree
    ├── PageHeader (provider-aware subtitle)
    ├── DashboardHero (provider-aware tagline)
    ├── 5 KPI tiles
    │     ├── Total Spend             ← /api/cloud/costs
    │     ├── Change vs Previous Period  ← /api/cloud/costs
    │     ├── Resources               ← /api/cloud/resources
    │     ├── Active Recommendations  ← /api/cloud/recommendations
    │     └── Potential Monthly Savings  ← /api/cloud/recommendations
    ├── Spend Trend                   ← /api/cloud/costs.daily_trend
    ├── Spend by Service | Spend by Region
    │     ← /api/cloud/costs.by_service / by_region
    ├── Resource Summary              ← /api/cloud/resources
    └── Optimization Summary          ← /api/cloud/recommendations
```

### 3.1 Component topology

* `DashboardPage` is a thin entry point. It reads `useProviderContext()` and dispatches to either `UnifiedDashboard` (AWS or Azure) or a friendly `DashboardUnsupported` fallback (Rackspace).
* `UnifiedDashboard` is the single provider-aware implementation. It uses `useFinopsQuery` three times — once per canonical endpoint — under a shared set of cache keys.
* The same `MetricCard`, `SectionCard`, `CostTrendChart`, `HorizontalBarChart`, `PartialWarning`, `LoadingSkeleton` and `ErrorState` components render for both providers.

### 3.2 Data flow

```
ProviderContext + AccountContext + FinopsPeriodProvider
                       │
                       ▼
                 dashboardScope.ts
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
 /api/cloud/costs   /api/cloud/resources   /api/cloud/recommendations
        │              │              │
        ▼              ▼              ▼
   UnifiedDashboard   UnifiedDashboard   buildUnifiedEnvelope
        │              │              │
        └──────────────┴──────────────┘
                       ▼
                 Provider-neutral KPIs / charts / summaries
```

---

## 4. Canonical data sources

| Domain | Endpoint | Used by |
|--------|----------|---------|
| Spend KPI, Trend, Service, Region | `/api/cloud/costs?provider=AWS|AZURE&month=…&account_id=…` | `SpendKpi`, `ChangeKpi`, `SpendTrendChart`, service/region bars |
| Resources KPI, Resource Summary | `/api/cloud/resources?provider=AWS|AZURE&account_id=…` | `ResourcesKpi`, `ResourceSummaryCard` |
| Optimization KPI, Savings, Summary | `/api/cloud/recommendations?provider=AWS|AZURE&account_id=…` | `OptimizationKpi`, `SavingsKpi`, `OptimizationSummaryCard` |

The legacy `/api/aws/*` routes remain available on the backend as compatibility surfaces but are no longer consumed by the unified Dashboard. The `dashboard.test.tsx` regression suite asserts that a transport serving only legacy `/api/aws/costs` data does NOT leak through to the unified Dashboard KPIs.

---

## 5. KPI semantics

The Dashboard exposes 5 provider-neutral KPIs. Card labelling and semantics are identical across AWS and Azure.

### 5.1 Total Spend

* Cost data absent → `"Unavailable"` (never `$0.00`).
* All subscriptions/accounts failed → `"Unavailable"`.
* Some failed → subtotal + `"Partial (N unavailable)"`.
* All succeeded → value (genuine `$0.00` is rendered as `$0.00`).

### 5.2 Change vs Previous Period

* Previous period present → `"+12.4%"` / `"−8.2%"` with tone `success` / `warning`.
* No change → `"No change"`.
* Previous period unavailable or zero → `"—"` (never a fabricated percentage).
* Direction word + absolute change in the description.

### 5.3 Resources

* Real count from `/api/cloud/resources.total_resources`.
* Description aggregates type count + location count + subscription attribution (`All Azure Subscriptions` / `<Subscription display name>` / `N subscriptions`).

### 5.4 Active Recommendations

* Count from canonical recommendations envelope.
* Description breaks the count by impact (high / medium / low).

### 5.5 Potential Monthly Savings

* AWS (`savings_period = MONTHLY`) → direct number.
* Azure (`savings_period = ANNUAL`) → annual figure ÷ 12 (display-only; original preserved in `estimatedAnnualSavings`).
* `savings_status = MIXED_CURRENCY` → `"Mixed"` + currency breakdown. Never sums across currencies.
* `savings_status = EMPTY` → `"Not available"`.
* Authoritative zero → `$0.00`.

---

## 6. Spend correctness (release-critical — Phase 7C hotfix #2 / 7D.3 invariant)

The Total Spend KPI must NEVER render `$0.00` for an aggregation in which every account / subscription failed. The Phase 7C.2B Azure fake-zero regression is preserved by:

* Reading `successful_accounts` and `failed_accounts` from the canonical cost envelope (carried in `CostReport.successful_accounts` / `failed_accounts` after `adaptCloudCostReport`).
* Rendering `Unavailable` when `failed > 0 && successful === 0`.
* Rendering the subtotal + a `Partial (N unavailable)` marker when `successful > 0 && failed > 0`.
* Rendering the value (which may legitimately be `$0.00`) when `failed === 0`.

The Azure throttling regression (every subscription THROTTLED, total_cost = 0) is locked down by `phase7d3-unified-dashboard.test.tsx > all-failed throttled Azure aggregation renders Unavailable (NEVER $0.00) — spec step 21`.

---

## 7. Partial-data handling

A canonical endpoint may return SUCCESS with one or more failed accounts/subscriptions, or PARTIAL_SUCCESS / NO_DATA / ERROR at the envelope level.

| Domain | SUCCESS | PARTIAL_SUCCESS | ERROR | NO_DATA |
|--------|---------|-----------------|-------|---------|
| Costs   | render value (incl. genuine zero) | render subtotal + Partial (N unavailable) banner | render Unavailable in Spend KPI; others stay live | render Unavailable |
| Resources | render real count | render count + partial banner | render "—" placeholder | render "—" |
| Recommendations | render KPIs + summary | render KPIs + partial banner | render "Not available" | render "—" |

Per-domain failures do not blank the dashboard. A dashboard-level `PartialWarning` renders whenever any domain surfaced a partial-data signal in its response. Individual KPI tiles and summary sections render their own per-section error indicators.

---

## 8. Provider switching

Provider switching is release-critical. The Dashboard must:

* Not blank the page.
* Not show stale KPIs from the previous provider.
* Not let an in-flight request from the previous provider overwrite fresh state.

Two safeguards protect this:

1. **Cache invalidation on provider/scope change** — `UnifiedDashboard` calls `invalidateFinopsCache()` whenever the active provider or selection changes (same invariant as bea38fd, expanded to also fire on selection change).
2. **Generation guard inside fetcher** — `useGeneration` bumps a `generation` token on every provider/scope change; the dashboard's fetcher captures the token at fetch-start and throws a `StaleGenerationError` if the response would land after the token bumped.

The `phase7d3-unified-dashboard.test.tsx > provider switching > a late AWS response after switching to Azure does NOT overwrite Azure state` test simulates this: an AWS request is held by a gated promise, the user switches to Azure, Azure renders its real value, then the late AWS response resolves with a different number and is discarded.

---

## 9. Scope switching

Same protections apply to account / subscription scope changes. The cache key segment for every Dashboard query embeds the canonical scope:

* AWS QA → `aws:acct:<qa-row-id>`
* AWS Production → `aws:acct:<prod-row-id>`
* AWS All → `aws:all`
* Azure MCPP → `azure:sub:<mcpp-guid>`
* Azure Sponsorship → `azure:sub:<sponsor-guid>`
* Azure All → `azure:all`

A stale Azure `acct:<n>` selection (from a pre-hotfix session) self-corrects to `azure:all` in the cache segment, so an authoritative-zero cache entry cannot leak across the hotfix boundary.

---

## 10. Race-condition protection

Every Dashboard fetch is wrapped in a generation guard:

```ts
const cost = useFinopsQuery<CostReportResponse>(
  costsKey,
  async () => {
    const startGen = generationRef.current
    const response = await fetchCloudCostReport(period, { provider, region, accountId: selection })
    if (startGen !== generationRef.current) {
      throw new StaleGenerationError(startGen, generationRef.current)
    }
    return adaptCloudCostReport(response)
  },
  { ttlMs: 60_000 },
)
```

The store treats `StaleGenerationError` like any other error — the entry lands in `error` state — but the message is stable and the response is guaranteed never to overwrite the fresh state. Combined with the `invalidateFinopsCache()` on provider/scope change, the dashboard cannot render stale data after a switch.

---

## 11. Charts

The Dashboard consumes the existing `CostTrendChart` and `HorizontalBarChart` components (provider-neutral, no library dependency). Service and region breakdowns use the canonical `by_service` / `by_region` arrays — never hardcoded service lists. Examples that DO render when AWS has them: EC2, RDS, S3, Lambda. Examples that DO render when Azure has them: Virtual Machines, Storage, App Service.

---

## 12. Cache isolation

Cache keys always include the provider prefix and the canonical scope segment. Verified by `phase7d3-unified-dashboard.test.tsx > cache isolation`:

* AWS costs != Azure costs
* AWS QA != AWS Production
* MCPP != Sponsorship
* Current month != previous month

The `dashboardScope.ts` module is the single source of truth for this. It also self-corrects stale Azure `acct:<n>` selections to `azure:all` (Phase 7C hotfix #2 invariant preserved).

---

## 13. Responsive layout

The KPI row uses Tailwind grid `grid-cols-1 sm:grid-cols-2 lg:grid-cols-5`. Charts use `grid-cols-1 lg:grid-cols-2`. Summary section uses `grid-cols-1 lg:grid-cols-2`. The dashboard:

* Wraps cleanly from 1024 px → 1280 px → 1440 px → 1600 px → 1920 px.
* Never overflows horizontally.
* Preserves the HipLink-inspired dark palette (Tailwind tokens: `bg-surface`, `border-border`, `text-fg-primary`, etc.).

---

## 14. Live validation

`scripts/phase7d3_live_validation.sh` exercises every canonical endpoint across every scope the Dashboard supports:

* AWS QA, Production, All
* Azure MCPP, Sponsorship, All
* Selector isolation (sub: → AWS rejected, acct: → Azure rejected)
* Provider/scope totals differ (cache isolation invariant)
* Route health (all 6 canonical routes return 200)

The script exits non-zero if any check fails, so the Phase 7D.3 regression gate is fully automated.

---

## 15. Security

The Dashboard is read-only:

* No AWS writes (no IAM, no Cost Anomaly Detection actions, no remediation).
* No Azure writes (no RBAC changes, no Advisor suppress calls, no remediation).
* No static credentials in source.
* No JWT / token leakage — auth header is the existing `Authorization: Bearer` injection in `lib/api.ts`; no new logging paths.
* No secrets in the production bundle (verified by `no-secrets.test.tsx`).

---

## 16. Known limitations

* Rackspace is explicitly out of scope for the executive Dashboard — `DashboardPage` renders a friendly `DashboardUnsupported` state for it (the locked two-row navigation remains intact).
* Cost data is period-aware via `FinopsPeriodProvider`; resources and recommendations are current-state only (their canonical endpoints are not period-bound). The Dashboard honours this distinction by re-fetching cost on period change while keeping resource and recommendation caches stable across period changes within the same provider/scope.
* The unified Dashboard intentionally hides the per-cloud identity panel and capabilities surface (Compute Optimizer / Cost Optimization Hub). Those still live on the dedicated `/optimization` page where they belong. This matches the spec's executive-KPI focus.
