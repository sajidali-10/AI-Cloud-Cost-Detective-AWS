# Phase 7D.2 — Unified AWS + Azure Optimization UI

## 1. Overview

Phase 7D.2 unifies the AWS and Azure Optimization experiences
behind a single provider-aware page that consumes the canonical
`/api/cloud/recommendations` endpoint introduced in Phase 7C.2D
and expanded to AWS in Phase 7D.1.

There is no longer a separate AWS-only Optimization view and an
Azure-only Advisor view. The single page reads from the Cloud
Context (provider + account/subscription + region + days) and
renders a shared professional surface for both providers.

The page is **strictly read-only** — no remediation controls are
exposed, by design.

## 2. Pre-flight Ancestry Verification

Before any code changes, the Phase 7D.2 branch was verified to
contain every historical fix that protects Phase 7C's regression
contract:

| Commit    | Description                          | Included |
|-----------|--------------------------------------|----------|
| bea38fd   | Provider-switch blank-page hotfix    | YES      |
| 0c7fce1   | Azure subscription correctness       | YES      |
| 6938362   | Phase 7C final cleanup               | YES      |
| 43b6fae   | Phase 7D.1 canonical AWS integration | YES      |

The branch is `phase-7d2-unified-optimization-ui`. Working tree
was clean before Phase 7D.2 work began.

## 3. Old Optimization Architecture

Before Phase 7D.2, two separate pages served the two providers:

- **AWS**: `OptimizationPage.tsx` consumed the legacy
  `/api/aws/optimization/{capabilities,recommendations,summary}`
  routes. The page rendered AWS-specific KPIs (Cost Optimization
  Hub / Compute Optimizer / Deterministic), the legacy
  recommendation shape (per-row `reason_codes`, `restart_needed`,
  `rollback_possible`), and used the `Recommendation` type from
  `types/finops.ts`.

- **Azure**: `AzureAdvisorView.tsx` consumed the canonical
  `/api/cloud/recommendations` route from Phase 7C.2D. The page
  rendered Azure-specific KPIs (High/Medium/Low impact, annual
  savings) and the `CloudRecommendationItem` shape.

### Identified Duplication and Provider-Specific Assumptions

| Concern                         | AWS (legacy)              | Azure (canonical)         |
|---------------------------------|---------------------------|---------------------------|
| Savings period                  | Monthly                   | Annual                    |
| Savings display                 | `formatCurrencyDecimal`   | `formatAnnualSavings`     |
| Sources                         | COH / ComputeOptimizer / Deterministic | Azure Advisor |
| Recommendation row shape        | `Recommendation` (legacy) | `CloudRecommendationItem` |
| Filters / sort                  | None                      | None                      |
| KPI derivation                  | `OptimizationSummaryResponse` | `CloudRecommendationSummaryEnvelope` |
| Cache key builder               | `buildRecommendationsCacheKey` (period+region+account+cacheProvider) | `buildCloudRecommendationsCacheKey` (provider+account+region+days) |

### Stale-cache and Selection Hazards

- The legacy AWS cache key did not include the AWS region (Phase
  7D.1 added it).
- The legacy AWS Optimization page never reused the
  `providerVersion` counter, so a provider switch could leave a
  stale AWS response on screen.
- The Azure view was only mounted when the provider was Azure;
  switching AWS → Azure remounted the component, but the legacy
  AWS view's state lingered in the FinOps query store for any
  key that survived the change.

### Savings-Period Inconsistency

The Phase 7D.1 canonical envelope returns a provider-neutral
`potential_savings` + `savings_period` pair. The AWS path
populates it with `MONTHLY` figures; the Azure path populates it
with `ANNUAL`. The legacy AWS page read neither — it read the
legacy `OptimizationSummaryResponse` and never crossed the
boundary.

## 4. Canonical Frontend Flow

The Phase 7D.2 unified page consumes one canonical pipeline:

```
ProviderContext (active provider)
        ↓
AccountContext (canonical selector)
        ↓
FinopsPeriodContext (region + days)
        ↓
buildCloudRecommendationsCacheKey(selection, cacheProvider, region, days)
        ↓
fetchCloudRecommendations({ provider, accountId, region, days })
        ↓
/api/cloud/recommendations?provider=…&account_id=…&region=…&days=…
        ↓
CloudRecommendationListResponse
        ↓
buildUnifiedEnvelope({ response, provider, selection, accountMap, savingsPeriod })
        ↓
UnifiedEnvelope { kpis, rows, partialErrors, … }
        ↓
Shared React UI: PageHeader, KPIs, breakdown charts, filters,
                  recommendations table, detail panel
```

The legacy AWS optimization service (`lib/finops/optimization.ts`)
remains in the codebase as backend compatibility, but the
Optimization page no longer reads it as its primary source.

## 5. Provider Selection Architecture

The page reuses every Phase 7C selection invariant:

- `canonicalAccountParam(accountId, provider)` coerces a stale
  `acct:<n>` under Azure to `undefined` so the canonical Azure
  routes never receive an AWS-style selector.
- `canonicalCacheSegment(accountId, provider)` collapses a stale
  Azure `acct:<n>` to `'all'` in the cache key shape.
- The page builds its `accountMap` from `AccountContext.accounts`,
  storing both the canonical selector (`acct:<n>` or `sub:<guid>`)
  and the raw account / subscription id (e.g. `974053642038` or
  the Azure GUID). Friendly attribution looks up rows by either
  key so the canonical recommendation rows resolve to friendly
  names regardless of which identifier the backend carries.
- The `providerVersion` counter from `ProviderContext` is part of
  the page's effect deps. On every provider change the page calls
  `invalidateFinopsCache()` and re-renders the fresh canonical
  envelope.

The page never displays:

- `acct:<local-db-id>` — the local row id is a backend-only key.
- A raw `sub:<guid>` — the Azure subscription GUID is masked to
  `XXXX…YYYY` only when the row's account map lookup fails (i.e.
  a self-corrected stale selector). The display label is the
  friendly display name in every successful path.

## 6. Shared Recommendation View Model

`frontend/src/lib/finops/unifiedRecommendations.ts` is the single
source of truth for the unified recommendation shape.

### `UnifiedRecommendationRow`

Provider-neutral fields consumed by the React UI:

- `id` — stable row key (canonical recommendation_id).
- `provider` — `AWS` | `AZURE` | future.
- `source` / `sourceLabel` — `AWS_COST_OPTIMIZATION_HUB` →
  `AWS Cost Optimization Hub`, `AZURE_ADVISOR` →
  `Azure Advisor`, etc.
- `impact` — `High` | `Medium` | `Low` | `Unknown`.
- `resource` / `resourceId` / `resourceType` / `resourceTypeShort`.
- `region`.
- `action` / `title` / `description`.
- `estimatedMonthlySavings` — **always monthly**, regardless of the
  backend's `savings_period`. Azure Advisor's annual figure is
  divided by 12 here so the KPI tile, the breakdown chart, and
  the table agree on one presentation period.
- `estimatedAnnualSavings` — preserved so the detail panel can
  show "annual: …" when the row was normalised.
- `currency`.
- `attribution` — `FriendlyAttribution { label, maskedId,
  canonicalSelector }` derived from the active `AccountContext`.
- `monthlyNormalized` — `true` when the row's monthly figure
  was synthesised from an annual backend value. Test fixture
  contracts pin this.

### `UnifiedKpis`

- `activeRecommendations` — `summary.total_recommendations`.
- `potentialMonthlySavings` — derived from `summary.potential_savings`
  + `summary.savings_period`; null when `savings_status ===
  'MIXED_CURRENCY'` or `'EMPTY'`.
- `currency` — primary currency for the headline.
- `savingsStatus` — `OK` | `MIXED_CURRENCY` | `EMPTY`.
- `savingsByCurrency` — per-currency totals used when
  `savingsStatus === 'MIXED_CURRENCY'`.
- `resourcesAffected` — distinct `resourceId` count.
- `highImpact` / `mediumImpact` / `lowImpact` — counts derived
  from the actual rendered rows (so the KPI matches the table
  even if the backend summary is slightly stale).
- `recommendationSources` — provider-neutral source breakdown.
- `authoritativeZero` — `true` when the backend reports a
  zero-value `potential_savings`; the UI then renders "Authoritative zero".
- `savingsUnavailable` — `true` when no authoritative figure exists.

### `UnifiedEnvelope`

The page-level envelope passed into the React tree. The page
constructs it once via `buildUnifiedEnvelope({ response, provider,
selection, accountMap, savingsPeriod })`.

## 7. AWS / Azure Differences

| Concern                       | AWS path                                  | Azure path                                 |
|-------------------------------|-------------------------------------------|--------------------------------------------|
| Selector                      | `acct:<local-row-id>`                     | `sub:<subscription-guid>`                  |
| Savings period (backend)      | `MONTHLY`                                 | `ANNUAL`                                   |
| Monthly normalisation         | Pass-through                              | `value / 12` for the headline              |
| Sources                       | COH, Compute Optimizer, Cost Detective    | Azure Advisor                              |
| `by_account` / `by_subscription` | `by_account` populated (account ids)   | `by_account` populated (subscription ids)  |
| `account_errors`              | Per-AWS-account failures                  | Per-Azure-subscription failures            |
| Recommendation Sources KPI    | Multiple sources common                   | Single source (Azure Advisor) common       |

The page renders all of these through one shared component tree;
the only provider-specific branching is in the friendly source
labels and the monthly-normalisation step.

## 8. Savings Normalisation

AWS canonical recommendations report a MONTHLY figure; Azure
Advisor reports an ANNUAL figure. The unified view model applies
display-only normalisation:

- `savingsPeriod === 'MONTHLY'` → pass through.
- `savingsPeriod === 'ANNUAL'` → `monthly = annual / 12`.
- `null` or unknown period → both monthly and annual are null.

The normalisation is **display-only**. The original annual figure
remains available on the row (`estimatedAnnualSavings`) and in the
detail panel.

**Mixed currencies** never sum into a single headline number. When
`summary.savings_status === 'MIXED_CURRENCY'`, the page renders
"Mixed currencies" + per-currency totals, never an FX-converted
total.

**Authoritative zero** is preserved as `0` (with a labelled
description "Authoritative zero") rather than being coerced to
"Not available".

## 9. Filters

Client-side filters share one component (`FilterBar`) and one
selector (`FilterSelect`). Filter dimensions:

- **Impact** — `High` | `Medium` | `Low` | `Unknown`.
- **Source** — friendly source labels (COH, Compute Optimizer,
  Cost Detective, Azure Advisor, etc.).
- **Resource type** — last segment of the ARM-style resource type
  (e.g. `virtualMachines` for `Microsoft.Compute/virtualMachines`)
  or the AWS slug (`EBS_VOLUME` → `EBS VOLUME`).
- **Region** — backend region field, used for AWS; the facet is
  hidden when the row has no region.

A free-text search input filters by resource id, action, title,
or description. Filtering is purely client-side and resets when
the envelope shape changes (provider or scope switch).

## 10. Sorting

Default sort: `savings-desc` (highest estimated monthly savings
first; nulls sort last so a missing figure never gets fabricated
into the top position).

User-selectable sort keys:

- **Highest savings** — `savings-desc`.
- **Highest impact** — `impact-desc` (High → Medium → Low →
  Unknown).
- **Source** — alphabetical on the friendly label.
- **Resource** — alphabetical on the resource id.

Sorting is deterministic. The comparator ties on
`(resource, action, id)` so two renders with the same input
yield the same order.

## 11. Empty / Partial / Error States

The page renders distinct visual states for the canonical
envelope statuses:

- **SUCCESS + zero** — valid empty state: "No active optimization
  recommendations were returned for this scope." This is not an
  error; Azure Advisor's zero-recommendation response is
  perfectly acceptable and renders here.
- **NO_DATA** — same empty-state copy.
- **PARTIAL_SUCCESS** — recommendations render; a `PartialWarning`
  block above the KPIs lists the failed account / subscription
  display names + error codes.
- **ERROR** — `ErrorState` with a sanitised backend message.
  The page never echoes raw AWS SDK or Azure SDK exceptions.

Failed scopes never wipe valid rows. If one AWS account fails
and another succeeds, the page renders the successful rows +
labels the failure as partial.

## 12. Cache Isolation

The canonical cache key is built by
`buildCloudRecommendationsCacheKey(selection, cacheProvider,
region, days)` and folds every dimension that affects the
response:

| Dimension  | AWS                                | Azure                                |
|------------|------------------------------------|--------------------------------------|
| Provider   | `aws:`                             | `azure:`                             |
| Account    | `acct:11`, `acct:12`, or `all`     | `sub:<guid>`, or `all`               |
| Region     | `us-east-1` / `all`                | (ignored — Azure is subscription-scoped) |
| Days       | `7`, `30`, `60`, `90`              | (ignored — Azure Advisor is snapshot) |

Phase 7D.1 invariants preserved:

- AWS QA-Dev, AWS Production, All-AWS, MCPP, Sponsorship, All-Azure
  all generate distinct cache keys.
- Provider switching invalidates the active entry so a stale
  AWS response can never satisfy an Azure request.

## 13. Provider-Switch Regression Protection

`OptimizationPage` mounts two refs (`prevSelectionRef`,
`prevProviderRef`) and a `useEffect` that calls
`invalidateFinopsCache()` whenever either changes. The
`providerVersion` counter from `ProviderContext` is part of the
effect's deps.

The order of operations on a provider switch is:

```
provider changes
  ↓
ProviderContext.setProvider bumps providerVersion
  ↓
OptimizationPage effect fires
  ↓
invalidateFinopsCache() drops every prior entry
  ↓
page re-renders with the new provider
  ↓
new useFinopsQuery fetches with the new cache key
  ↓
envelope builds with the new provider's accountMap
  ↓
KPI / table update with no stale data
```

The provider-switch test
(`phase7d2-page-rendering.test.tsx > AWS → Azure → AWS does not
blank the page`) pins this sequence end-to-end.

## 14. Responsive Design

The page is responsive at every breakpoint the spec calls out:

- **1024 px** — KPI grid collapses to 1 column.
- **1280 px** — KPI grid is 2 columns.
- **1440 px / 1600 px / 1920 px** — KPI grid is 4 columns; table
  uses horizontal scroll inside its card.

The Cloud Context bar (Row 2) and primary nav (Row 1) are
untouched — Phase 7D.2 does not introduce a second
provider/account selector.

## 15. Live AWS Validation

`scripts/phase7d2_live_validation.sh` exercises the canonical
endpoint for every AWS scope:

- All AWS Accounts → `provider=AWS`, `total=32`, `savings_period=MONTHLY`.
- AWS QA-Dev → `scope=acct:974053642038`, `total=30`.
- AWS Production → `scope=acct:852428675923`, `total=2`.

The QA + Prod total matches All-AWS exactly, confirming the
canonical route never double-counts.

## 16. Live Azure Validation

The same script exercises Azure:

- All Azure Subscriptions → `provider=AZURE`, `total=0`,
  `savings_period=ANNUAL`.
- MCPP → `scope=sub:a54d076b-2650-4b4f-a28c-2deed2011f83`, `total=0`.
- Sponsorship → `scope=sub:9353d858-27fa-4ce7-bf87-782f3323d297`, `total=0`.

The zero totals are the canonical Azure Advisor response — the
empty-state path renders "No active optimization recommendations
were returned for this scope."

## 17. Test Counts

| Suite                                          | Tests | Result |
|------------------------------------------------|------:|-------:|
| `phase7d2-unified-optimization.test.ts`        |    39 | PASS   |
| `phase7d2-page-rendering.test.tsx`             |    11 | PASS   |
| Targeted regression (provider-switch, Azure, Phase 7C, Phase 7D.1) | 64 | PASS |
| **Focused Phase 7D.2 total**                   |    50 | **PASS** |

## 18. Known Limitations

- The Azure Advisor response currently returns zero recommendations
  for both MCPP and Sponsorship. This is the live Azure state at
  the time of Phase 7D.2 deployment; the UI correctly renders the
  empty state without classifying it as an error.
- Filter resets are envelope-scoped (provider + scope), not
  filter-state-scoped. Switching scope clears active filters by
  design so stale per-scope facet selections cannot leak across.
- The page is read-only; remediation / apply / resize controls
  remain out of scope for Phase 7D.2.
