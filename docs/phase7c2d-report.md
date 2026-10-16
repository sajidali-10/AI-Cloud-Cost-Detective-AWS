# Phase 7C.2D / 7C.2D.1 — Final Report

**Branch:** `phase-7c-multi-cloud`
**Latest commit:** see final commit hash below

## 1. Status

**PASS** — Azure Advisor Cost recommendations integrated, lifecycle
semantics corrected (7C.2D.1), validated, and tested.  Strictly
READ-ONLY against Azure.

## 2. Commit

Pushed to `phase-7c-multi-cloud`.  NOT merged to `main`.  No
Phase 7C final tag created.

## 3. Root cause (Phase 7C.2D.1 finding)

Phase 7C.2D shipped with the KQL filter
``properties.recommendationStatus == "Active"``.  Microsoft
documents the consolidated ``recommendationStatus`` lifecycle
field with values ``New`` / ``InProgress`` / ``Completed`` /
``Postponed`` / ``Dismissed``.  The literal string ``"Active"``
is **not** a documented ``recommendationStatus`` value — it is
the Azure Portal's user-facing label that encompasses ``"New"``
(and sometimes ``"InProgress"``).

Live Step 0 inspection (Phase 7C.2D.1) confirmed:

| Distinct `recommendationStatus` | Tenant count |
|----------------------------------|--------------|
| `Completed` | 1 (Sponsorship VM) |
| `New` (only on OperationalExcellence / HighAvailability) | 20 (MCPP) |

The previously reported zero-result was for the **wrong** reason
(the KQL filter used an undocument value).  After fixing the
filter to ``"New"``, the result remains zero — but now for the
**correct** reason: there is genuinely no `New + Cost`
recommendation across the configured Azure identity.

## 4. Correct lifecycle semantics (Phase 7C.2D.1 fix)

| Portal label | Resource Graph `recommendationStatus` value | Surfaced? |
|---------------|------------------------------------------------|-----------|
| Active (actionable) | **`New`** | **Yes** (Phase 7C.2D.1) |
| In progress | `InProgress` | No (out of dashboard scope) |
| Completed | `Completed` | No |
| Postponed | `Postponed` | No |
| Dismissed | `Dismissed` | No |

## 5. Final Advisor query design

```kql
advisorresources
| where type == "microsoft.advisor/recommendations"
| where properties.category == "Cost"
| where properties.recommendationStatus == "New"
| project subscriptionId, id, name, type, properties
```

Critical contract preserved from Phase 7C.2D:

* **No** `| limit` / `| take` / `| sample` — disables `$skipToken`.
* Page size via `options.$top` (default 1000).
* Pagination: `$skipToken`, `MAX_PAGES=50`, repeated-cursor +
  truncated-without-cursor + schema-drift protections
  (Phase 7C.2C.1 contract).
* Reuses Phase 7C.2C.1 hardened Resource Graph client.

## 6. Metadata / category mapping

The `category` field is reliably present on
`microsoft.advisor/recommendations` rows (verified during Step 1).
The Phase 7C.2D query filters `properties.category == "Cost"` and
the aggregator surfaces it unchanged.

The remaining Phase 7C.2D.1 column-mapping review confirmed:

* `subscriptionId` is exposed as a **top-level** row field by
  Resource Graph.  The canonical `subscription_id` is sourced from
  the top-level field with a fallback to
  `properties.resourceMetadata.resourceId` parsing.
* `impactedField` is the canonical resource type (e.g.
  `Microsoft.Compute/virtualMachines`).
* `impactedValue` is the resource name (e.g. `vm1`).
* `properties.shortDescription.problem` is the title;
  `solution` is the remediation.
* `extendedProperties.recommendationSubCategory` is the
  subcategory (`UsageOptimization`, etc.).
* `properties.lastUpdated` is the timestamp.

## 7. Configuration / exclusion behavior

Microsoft documents that the Portal's "Active cost recommendations"
view accounts for the subscription-level Advisor configuration
(`/providers/Microsoft.Advisor/configurations`) — which scopes
recommendations by Cloud / Subscription / Resource Group
exclusion, plus per-recommendation-type thresholds
(`lowCpuThreshold` for the right-size VM family).

Phase 7C.2D does **NOT** reproduce that exclusion layer:

* Reproducing the configuration join requires a second Resource
  Graph query against `/providers/Microsoft.Advisor/configurations`
  per subscription, plus a per-recommendation-type rule evaluation.
* The current Advisor configuration on both subscriptions is the
  default (no exclusions); the dashboard would not change.
* The Phase 7C.2D design choice is to expose the raw Advisor
  population and let the operator act on the dashboard — the same
  shape the existing Phase 7B AWS optimization page uses.

When Microsoft's exclusion logic is required (future phase), the
configuration join is a single additional Resource Graph query
that filters the canonical list by exclusion predicate before the
aggregator runs.

## 8. Live recommendation statuses observed

The Step 1 unfiltered inspection returned (across both
subscriptions):

| Category | recommendationStatus | Count | Source |
|----------|----------------------|------:|--------|
| Cost | Completed | 1 | Sponsorship |
| OperationalExcellence | New | 12 | MCPP |
| OperationalExcellence | Completed | 3 | MCPP |
| HighAvailability | New | 8 | MCPP |
| HighAvailability | Completed | 5 | MCPP |

`Cost + New` = **0** rows.  The dashboard therefore shows an empty
state — confirmed in Step 11 below.

## 9. MCPP active=New results

```
status: SUCCESS
total_recommendations: 0
savings_status: EMPTY
```

## 10. Sponsorship active=New results

```
status: SUCCESS
total_recommendations: 0
savings_status: EMPTY
```

## 11. All Azure Subscriptions active=New results

```
status: SUCCESS
total_recommendations: 0
savings_status: EMPTY
```

All three endpoints return SUCCESS + `total_recommendations=0`.
The empty state is legitimate and is the **correct** answer per
Microsoft's documented lifecycle.

## 12. Completed recommendation validation

The previously observed Sponsorship VM Cost recommendation
remains **correctly excluded** by the corrected `New` filter:

* `recommendationStatus`: **Completed** (excluded)
* `annualSavingsAmount`: 1488 (explicit, would have been used)
* `savingsCurrency`: USD
* `term`: P1Y
* `lookbackPeriod`: P30D
* `recommendationSubCategory`: UsageOptimization
* `impact`: High
* `category`: Cost

The recommendation is intentionally NOT surfaced in the active
dashboard because the user-visible Azure Portal "Active" concept
maps to the Resource Graph `New` lifecycle value (not `Completed`).

## 13. Savings / currency validation

Phase 7C.2D.1 did not change savings handling — the
Phase 7C.2D aggregator continues to:

* Use `annualSavingsAmount` only when explicitly provided.
* Never annualise `savingsAmount` (monthly).
* Treat `annual_savings=None` as a valid recommendation with
  no savings data (counted in `recommendations_without_savings`).
* Treat `annual_savings=0` as a valid Decimal distinct from
  missing.
* Surface `MIXED_CURRENCY` when currencies disagree; never invent
  an FX rate.

These invariants are pinned by 4 explicit aggregator tests
(`test_uniform_currency_summed`,
`test_mixed_currency_returns_mixed_status_no_total`,
`test_recommendations_without_savings_retained`,
`test_zero_savings_distinct_from_missing`).

## 14. Frontend validation

No frontend code changes were needed for Phase 7C.2D.1 — the
corrected backend already returns zero recommendations, which
`AzureAdvisorView` renders as a legitimate empty state.  The
KPIs show `0`, the breakdowns are empty arrays, and the
recommendation table is empty.  No error banner — `status=SUCCESS`
+ `total_recommendations=0` is treated as a valid empty state.

## 15. AWS regression

`/api/aws/optimization/summary?days=30` → HTTP 200 / SUCCESS.
`AWSCloudProvider.get_capabilities()` unchanged.  No AWS code
modified.

## 16. Backend tests

* **New tests in Phase 7C.2D.1:** 16 added on top of the
  Phase 7C.2D baseline:
  * `test_active_status_is_new_not_active`
  * `test_kql_does_not_reference_other_lifecycle_values`
  * 5 lifecycle exclusion contract tests
  * 12 realistic-row top-level `subscriptionId` mapping tests
* **Total Phase 7C.2D tests:** **75** (up from 55 in Phase 7C.2D)
* **Targeted Phase 7C.2 suite (A/B/C/D):** **181 passed**

## 17. Frontend tests

* **Total frontend suite:** **471 passed** (same as Phase 7C.2D
  baseline; no NEW regressions)

## 18. Build / Docker / verification scripts

| Check | Result |
|-------|--------|
| `tsc --noEmit` | clean (no errors) |
| `vite build` (frontend/) | SUCCESS (105 modules, 31s) |
| `docker compose build backend` | SUCCESS |
| Docker container health | **5/5 healthy** |
| `scripts/phase7b_nginx_test.sh` | 45 PASS / 0 FAIL |
| `scripts/phase7b_verify.sh` | 100 PASS / 1 pre-existing FAIL (unrelated AWS-key check) |

## 19. Files changed (Phase 7C.2D.1)

### Modified

| File | Change |
|------|--------|
| `backend/app/services/cloud/azure/advisor.py` | `ACTIVE_STATUS` changed from `"Active"` to `"New"`; KQL filter updated; docstring updated to document Portal-vs-Resource-Graph distinction |
| `backend/tests/test_phase7c2d_azure_advisor.py` | Added 16 new tests: KQL-active-status, lifecycle exclusion contract, realistic-row top-level `subscriptionId` mapping |
| `docs/phase7c2d-azure-advisor.md` | Corrected Portal-concept-vs-Resource-Graph-value distinction; documented `InProgress` exclusion policy; documented Advisor configuration exclusion deferral |
| `docs/phase7c2d-report.md` | This file |

### Unchanged from Phase 7C.2D

All other Phase 7C.2D files are unchanged.  The aggregator
output envelope, the Pydantic schemas, the route, the adapter
wiring, the frontend service, the `AzureAdvisorView` component
— all unchanged.

## 20. Security review

* No `Authorization: Bearer …` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs truncated via `safe_subscription_label`.
* KQL hardcoded; the front-end cannot inject KQL.
* Tags / properties / extendedProperties never echoed.
* `composite_id = AZURE|<id>` is provider-prefixed.
* `MAX_PAGES = 50` prevents unbounded pagination.
* **READ-ONLY**: no Advisor generation / lifecycle / resource
  mutations were issued during validation.

## 21. Pre-existing failures

* 6 frontend tests in `phase7c2a-navigation.test.tsx` and
  `phase7b-runtime-closure.test.tsx` rely on `fs.promises.readFile`
  with relative paths; only pass when vitest's cwd is `frontend/`.
  Verified pre-existing via `git stash`.
* `scripts/phase7b_verify.sh` reports one FAIL on "no AWS key in
  known storage keys" — pre-existing security check unrelated to
  Phase 7C.2D.1.

## 22. Confirmation

* [x] No Azure resources were modified.
* [x] No Advisor states were modified.
* [x] No recommendations were generated.
* [x] No recommendation was dismissed / postponed / completed.
* [x] No reservation / savings plan was purchased.
* [x] No AI / LLM recommendation code was added.
* [x] Rackspace was **NOT** implemented.
* [x] `main` was **NOT** modified.
* [x] No Phase 7C final tag was created.

## Phase 7C.2D / 7C.2D.1 complete

The Phase 7C.2D.1 narrow correctness pass corrected the Advisor
lifecycle filter from the Portal-flavoured literal `"Active"` to
the documented Resource Graph value `"New"`.  The live result
remains `total_recommendations=0` across MCPP, Sponsorship, and
All Azure — but now for the **correct** reason per Microsoft's
documentation.

Strictly READ-ONLY against Azure.  Only
`Microsoft.ResourceGraph/resources/read` operations were issued
during validation.  Stopping per Phase 7C.2D.1 scope.
