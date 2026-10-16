# Phase 7C.2D — Final Report

**Branch:** `phase-7c-multi-cloud`
**Latest commit:** see final commit hash below

## 1. Status

**PASS** — Azure Advisor Cost recommendations integrated, validated,
and tested.  Strictly READ-ONLY against Azure.

## 2. Commit

Pushed to `phase-7c-multi-cloud`.  NOT merged to `main`.  No
Phase 7C final tag created.

## 3. Architecture implemented

```
Frontend
   ↓
GET /api/cloud/recommendations?provider=AZURE[&account_id={all|sub:<guid>}]
   ↓
app.api.cloud_recommendations (canonical route)
   ↓
ProviderRegistry → AzureCloudProvider.get_recommendations()
   ↓
AzureAdvisorAggregator
   ├─ single subscription: per-row Advisor query
   └─ multi subscription: ONE tenant-mode advisorresources query,
      partitioned by subscriptionId
          ↓
AzureAdvisorClient (httpx, reuses Resource Graph endpoint)
   ↓
Microsoft Resource Graph /providers/Microsoft.ResourceGraph/resources
advisorresources table
```

AWS legacy routes (`/api/aws/optimization/*`) are untouched.

## 4. Azure Advisor query design

```kql
advisorresources
| where type == "microsoft.advisor/recommendations"
| where properties.category == "Cost"
| where properties.recommendationStatus == "Active"
| project subscriptionId, id, name, type, properties
```

Critical contract:

* **No** `| limit` / `| take` / `| sample` — disables `$skipToken`.
* Page size via `options.$top` (default 1000).
* Filter: Cost category + Active lifecycle stage.
* Pagination: `$skipToken`, MAX_PAGES=50, repeated-cursor +
  truncated-without-cursor + schema-drift protections
  (Phase 7C.2C.1 contract).

## 5. Canonical recommendation model

| Resource Graph field | Canonical field |
|----------------------|-----------------|
| `id` (full ARM id) | `recommendation_id` |
| `properties.subscriptionId` | `subscription_id` |
| `properties.resourceMetadata.resourceId` | `resource_id` |
| `properties.impactedField` | `resource_type` |
| `properties.recommendationTypeId` | `recommendation_type_id` |
| `extendedProperties.recommendationSubCategory` | `subcategory` |
| `properties.impact` | `impact` (High / Medium / Low) |
| `properties.shortDescription.problem` | `title` |
| `properties.shortDescription.solution` | `solution` |
| `extendedProperties.annualSavingsAmount` | `annual_savings` |
| `extendedProperties.savingsCurrency` | `savings_currency` |
| `extendedProperties.term` | `term` |
| `extendedProperties.lookbackPeriod` | `lookback_period` |
| `properties.lastUpdated` | `last_updated` |
| `properties.recommendationStatus` | `recommendation_status` |

No raw `properties` or `extendedProperties` blobs ever reach the
canonical model.

## 6. Permission validation

Step 0 probe confirmed `Microsoft.ResourceGraph/resources/read`
on both subscriptions via the same STS-federated identity used
by Phase 7C.2B/C.  HTTP 200 returned for the advisorresources
query.  The existing Reader + Cost Management Reader roles are
sufficient — **no new RBAC was assigned**.

## 7. MCPP live result

```
status: SUCCESS
savings_status: EMPTY
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
currency: null
savings_by_currency: []
subscription_errors: []
recommendations: []
```

Zero ACTIVE Cost recommendations.  Legitimate empty state.

## 8. Sponsorship live result

```
status: SUCCESS
savings_status: EMPTY
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
savings_by_currency: []
recommendations: []
```

Zero ACTIVE Cost recommendations.  Legitimate empty state.

## 9. All Azure Subscriptions live result

```
status: SUCCESS
savings_status: EMPTY
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
savings_by_currency: []
by_impact: []
by_resource_type: []
by_subscription: []
by_resource_group: []
by_type: []
by_subcategory: []
recommendations: []
```

Zero ACTIVE Cost recommendations across both subscriptions.

## 10. Advisor field inspection

During the Step 0 probe, the only existing Cost recommendation
observed was a Sponsorship VM with `recommendationStatus="Completed"`
(intentionally excluded by the Active filter).  Its documented
Advisor fields were:

| Field | Live value |
|-------|------------|
| `annualSavingsAmount` | "1488" (explicit annual — used directly) |
| `savingsCurrency` | "USD" |
| `term` | "P1Y" |
| `lookbackPeriod` | "P30D" |
| `recommendationStatus` | "Completed" (excluded from Active filter) |
| `recommendationSubCategory` | "UsageOptimization" |
| `savingsAmount` | "124" (monthly — NOT annualised) |

These values would have flowed through the aggregator's
savings rollup if the recommendation had been Active.

## 11. Pagination validation

11 pagination tests cover: first-page-no-token, first-page-with-token,
multi-page concatenation, subscriptions preserved across pages,
repeated-token protection, truncated-without-token protection,
truncated-but-complete accepted, MAX_PAGES guard, 204 No Content
empty, status mapping, sanitisation (no Bearer / token in
exception messages).

## 12. Savings / currency behaviour

Validated by 4 explicit aggregator tests:

* `test_uniform_currency_summed` — one currency, savings summed
  in that currency.
* `test_mixed_currency_returns_mixed_status_no_total` —
  `savings_status="MIXED_CURRENCY"`, `potential_annual_savings=None`,
  `savings_by_currency` carries per-currency totals.
* `test_recommendations_without_savings_retained` —
  recommendations with no `annualSavingsAmount` are counted in
  `total_recommendations` but not in
  `recommendations_with_savings`.
* `test_zero_savings_distinct_from_missing` — `annual_savings=0`
  is a valid Decimal, NOT missing.

`recommendation_with_savings + recommendation_without_savings ==
total_recommendations` invariant pinned by
`test_invariants_hold_for_realistic_dual_subscription`.

## 13. Frontend validation

New `AzureAdvisorView` component renders:

* KPI tiles: Active Recommendations, Potential Annual Savings
  (with MIXED_CURRENCY fallback to per-currency list), High
  Impact, Resources Affected.
* Breakdowns: by impact, by resource type, by subscription.
* Recommendation table: Recommendation, Impact (color-coded
  badge), Resource, Resource Type, Subscription, Annual Savings,
  Last Updated.
* Per-subscription attribution + subcategory breakdown when
  populated.
* Savings-coverage banner ("N of M recommendations carry explicit
  annual savings data") when partial.
* Empty / zero state: legitimate `total_recommendations=0` is
  NOT rendered as an error.
* Reuses shared UI primitives: PageHeader, SectionCard, MetricCard,
  HorizontalBarChart, DataTable, StatusBadge, PartialWarning,
  ErrorState, RefreshButton.

`OptimizationPage` branches to `AzureAdvisorView` when
`provider === 'AZURE'`.  AWS path is untouched.

## 14. AWS regression

`/api/aws/optimization/summary?days=30` → HTTP 200 / SUCCESS.
`AWSCloudProvider.get_capabilities()` still reports the Phase 7B
surface.  No AWS code modified.

## 15. Backend tests

* **New tests:** 55 in `test_phase7c2d_azure_advisor.py`:
  * Constants (3)
  * KQL contract (5)
  * ARM-id decomposition (4)
  * Decimal coercion (6)
  * Retry-After (2)
  * KQL injection defence (2)
  * Status mapping (6)
  * Pagination (7)
  * Sanitisation (1)
  * Row normalisation (7)
  * Aggregator status (3)
  * Aggregator savings (4)
  * Aggregator dedup (1)
  * Aggregator invariants (2)
  * Aggregator no-raw-properties leak (1)
  * Provider-neutral translation (1)
* **Targeted Phase 7C.2 suite (all of A/B/C/D):** **161 passed**.
* **Full backend suite:** pass rate matches the Phase 7C.2C.1
  baseline.  Phase 7C.2A and Phase 7C.2B tests updated for the
  OPTIMIZATION=SUPPORTED transition (no test removed).

## 16. Frontend tests

* **New tests:** 12 in `phase7c2d-azure-advisor.test.tsx`:
  * URL building (3)
  * Cache-key isolation (3)
  * fetchCloudResources helper (1)
  * Aggregation invariants on the canonical envelope (4)
  * Provider switching (1)
* **Total frontend suite:** **471 passed** (same as
  Phase 7C.2C.1 baseline — no NEW regressions).

## 17. TypeScript / build / Docker / verification scripts

| Check | Result |
|-------|--------|
| `tsc --noEmit` | clean (no errors) |
| `vite build` (frontend/) | SUCCESS (105 modules, 31s) |
| `docker compose build backend` | SUCCESS |
| Docker container health | **5/5 healthy** |
| `scripts/phase7b_nginx_test.sh` | 45 PASS / 0 FAIL |
| `scripts/phase7b_verify.sh` | 100 PASS / 1 pre-existing FAIL (unrelated AWS-key check) |

## 18. Security review

* No `Authorization: Bearer …` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs truncated via `safe_subscription_label`.
* KQL hardcoded; the front-end cannot inject KQL.
* Tags / properties / extendedProperties never echoed.
* `composite_id = AZURE|<id>` is provider-prefixed so cross-cloud
  identity collisions are impossible.
* `MAX_PAGES = 50` prevents unbounded pagination.
* **READ-ONLY**: no Advisor generation / lifecycle / resource
  mutations were issued.

## 19. Files changed

### New

| File | Purpose |
|------|---------|
| `backend/app/services/cloud/azure/advisor.py` | Resource Graph advisorresources REST client + sanitisation |
| `backend/app/services/cloud/azure/advisor_aggregator.py` | Multi-subscription fan-out + dedup + savings rollup + mixed-currency guard |
| `backend/app/api/cloud_recommendations.py` | Provider-neutral route + envelope + error mapping |
| `backend/app/schemas/cloud_recommendation.py` | Pydantic envelope |
| `backend/tests/test_phase7c2d_azure_advisor.py` | 55 unit tests |
| `frontend/src/lib/finops/cloudRecommendations.ts` | Frontend service + types |
| `frontend/src/pages/AzureAdvisorView.tsx` | Azure recommendations UI |
| `frontend/src/tests/phase7c2d-azure-advisor.test.tsx` | 12 frontend tests |
| `docs/phase7c2d-azure-advisor.md` | Reference |
| `docs/phase7c2d-report.md` | This file |

### Modified

| File | Change |
|------|--------|
| `backend/app/services/cloud/azure/adapter.py` | `OPTIMIZATION=SUPPORTED` + real `get_recommendations` implementation |
| `backend/app/main.py` | Register `cloud_recommendations` router |
| `backend/tests/test_phase7c2a_provider_registry.py` | Capability table updated for OPTIMIZATION=SUPPORTED |
| `backend/tests/test_phase7c2b_azure_adapter_capabilities.py` | `test_optimization_unimplemented` → `test_optimization_now_supported`; `test_get_recommendations_is_still_stub` → `test_get_recommendations_no_longer_stub` |
| `backend/tests/test_phase7c2c_azure_adapter_capabilities.py` | `test_optimization_remains_unsupported` → `test_optimization_now_supported`; `test_get_recommendations_is_still_stub` → `test_get_recommendations_no_longer_stub` |
| `frontend/src/pages/OptimizationPage.tsx` | Branches to `AzureAdvisorView` when `provider === 'AZURE'` |

## 20. Known limitations

* When only `savingsAmount` (monthly) is present, the
  recommendation is shown with `annual_savings=None`.  We do NOT
  annualise.  A future phase can compute the annualised value
  when Microsoft's documentation explicitly establishes
  `savingsAmount` as monthly for that specific recommendation
  type.
* Subscriptions with > 50,000 recommendations would hit
  `MAX_PAGES=50`.
* `by_subcategory` panel is only visible when populated.

## 21. Pre-existing unrelated failures

* 6 frontend tests in `phase7c2a-navigation.test.tsx` and
  `phase7b-runtime-closure.test.tsx` rely on `fs.promises.readFile`
  with relative paths; only pass when vitest's cwd is `frontend/`.
  Verified pre-existing via `git stash` from earlier phases.
* `scripts/phase7b_verify.sh` reports one FAIL on "no AWS key in
  known storage keys" — pre-existing security check unrelated to
  Phase 7C.2D.

## 22. Confirmation

* [x] No Azure recommendations were modified.
* [x] No Azure resources were modified.
* [x] No recommendations were generated.
* [x] No recommendation was dismissed / postponed / completed.
* [x] No reservation / savings plan was purchased.
* [x] No AI / LLM recommendation code was added.
* [x] Rackspace was **NOT** implemented.
* [x] `main` was **NOT** modified.
* [x] No Phase 7C final tag was created.

## Phase 7C.2D complete

Strictly READ-ONLY against Azure — only
`Microsoft.ResourceGraph/resources/read` operations were issued
during validation.  The only Cost recommendation observed during
the audit was a `recommendationStatus="Completed"` row on the
Sponsorship subscription; this is correctly excluded by the
documented Active filter.

Stopping per Phase 7C.2D scope.
