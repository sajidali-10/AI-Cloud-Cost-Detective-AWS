# Azure Subscription / Cost Hotfix — Final Report

**Phase 7C post-release hotfix #2** — branch `hotfix/azure-subscription-cost-correctness`

---

## 1. Status ⦿ COMPLETE

All hotfix work is implemented, tested, live-validated, committed, and pushed.
No merge to `main`, no start of Phase 7D, `phase-7c-complete` tag untouched.

## 2. Branch + Commit

- **Branch**: `hotfix/azure-subscription-cost-correctness`
- **Commit**: `0c7fce1` — *"Hotfix: fix Azure subscription selection and cost completeness"*
- **Stack**: `0c7fce1` (hotfix #2) → `bea38fd` (hotfix #1, blank-dashboard) → `77fef2b` (Phase 7C merge)
- **Remote**: pushed to `origin/hotfix/azure-subscription-cost-correctness` (tracking set). **Not merged.**
- **main**: still `77fef2b`. **tag `phase-7c-complete`**: intact.

## 3. Root Cause A — why `acct:5`/`acct:6` reached Azure APIs

The `AccountSelector` built **every** selection value as `acct:${acc.id}` from the
**local database primary key**. For AWS that is correct — the Phase 7B route maps
row id → role ARN. For Azure the canonical routes (`/api/cloud/costs`,
`/api/cloud/resources`, `/api/cloud/recommendations`) resolve subscriptions through
Cost Management / Resource Graph / Advisor by **subscription GUID** and only accept
`sub:<guid>` (or `all`). A local PK (`acct:5`, `acct:6`) has no Azure meaning.
Reproduced live: `account_id=acct:5` → **HTTP 404**
`{"error_code":"AccountNotFound","message":"no AZURE account matches selection='acct:5'"}`
on all three canonical APIs.

The `by_account` rows are:
- id **5** → GUID `a54d076b-2650-4b4f-a28c-2deed2011f83` (MCPP)
- id **6** → GUID `9353d858-27fa-4ce7-bf87-782f3323d297` (Sponsorship)

## 4. Root Cause B — why failed/throttled requests could display `$0.00`

When every subscription failed / was throttled, the backend aggregator produced a
report with `status: PARTIAL_SUCCESS` and `total_cost: Decimal("0")` (the sum of the
successfully-returned snapshots, which was zero). The frontend rendered that
`total_cost` unconditionally via `formatCurrencyCompact`, so **UNKNOWN data was
displayed as `$0.00`**. A genuine $-zero (all queries OK, zero spend) was
indistinguishable from an all-failed aggregation. Live reproduction: All-Azure
returned `total_cost=0.00` with `account_errors=[THROTTLED, THROTTLED]` —
previously rendered as a bogus `$0.00`.

## 5. Subscription identity model — before / after

**Before:** `AccountSelection = 'all' | \`acct:${number}\``; the selector emitted
`acct:<local-row-id>` for both AWS and Azure; API clients forwarded it verbatim as
`account_id`.

**After:**
- `AccountSelection = 'all' | \`acct:${number}\` | \`sub:${string}\``.
- `AccountSelector` emits **`sub:<account_id>`** for Azure rows (GUID), **`acct:<row-id>`**
  unchanged for AWS rows.
- `readStoredSelection` accepts a stored `sub:<guid>` (in addition to `acct:<n>`).
- New provider-aware coercion boundary (`frontend/src/lib/finops/subscriptionSelection.ts`):
  `canonicalAccountParam` / `canonicalCacheSegment`. A stale `acct:<n>` under Azure
  **self-corrects to `all`** in both the query param and the cache key, so no
  `acct:` value can ever reach an Azure endpoint. `sub:<guid>` passes through; AWS
  passes through unchanged.
- Wired into **all three** canonical clients: `cloudCosts.ts`, `cloudResources.ts`,
  `cloudRecommendations.ts`.

**No subscription GUIDs are hardcoded in source.** The selector reads `acc.account_id`
dynamically from the selectable-accounts API.

## 6. Cost completeness behavior — before / after

`adaptCloudCostReport` now surfaces completeness from the canonical top-level fields
(`by_account` one entry per subscription incl. failed ones; `account_errors` the
throttled/failed list; `status`; `currency_status`):

- `successful_accounts = by_account.length − account_errors.length`
- `failed_accounts = account_errors.length`
- `subscription_errors` retained on the report.

`SpendKpi` rendering:
- **all-failed** (`failed>0 && successful===0`): value **"Unavailable"**, description
  `no subscription data` — **never `$0.00`**.
- **partial** (`successful>0 && failed>0`): valid subtotal + `Partial (N unavailable)`.
- **all-succeeded**: normal value, including a genuine `$0.00`.
- `account_errors`/`subscription_errors` remain in the envelope for downstream use.

## 7. Throttling / retry behavior

Already implemented in Phase 7C.2B (verified, not modified by this hotfix): a 429 →
`AzureCostError` categorised `THROTTLED` with `retry_after` parsed from the
`Retry-After` header (`costs.py`, `resource_graph.py`), propagated to
`account_errors` → `subscription_errors`. Live responses showed `code: "THROTTLED"`
surfacing correctly on MCPP, Sponsorship, and All-Azure. The frontend now renders
that state as **Unavailable / Partial** instead of a misleading zero.

## 8. Cache behavior

- The `build*CacheKey` functions now route through `canonicalCacheSegment` (provider-aware),
  so a stale pre-hotfix `acct:<n>` Azure key self-corrects to `all` — preventing a
  wrong-keyed authoritative-zero cache entry from being served as an Azure subscription.
- The backend read-through cache stores whatever the loader returns; the frontend no
  longer trusts a cached `total_cost=0` as a real zero when the report carries
  `account_errors` with zero successful subscriptions — it renders **Unavailable**.

## 9–11. MCPP / Sponsorship / All-Azure live results (Oct 2026 MTD)

All three are currently throttled by Azure, which is exactly the all-failed case the
hotfix guards — the UI now shows **Unavailable** rather than `$0.00`.

| Scenario | selector | HTTP | status | total | account_errors |
|---|---|---|---|---|---|
| MCPP | `sub:a54d076b-…` | 200 | PARTIAL_SUCCESS | 0.00 | [THROTTLED] |
| Sponsorship | `sub:9353d858-…` | 200 | PARTIAL_SUCCESS | 0.00 | [THROTTLED] |
| All Azure | `account_id=all` | 200 | PARTIAL_SUCCESS | 0.00 | [THROTTLED, THROTTLED] |
| Resources (Sponsorship) | `sub:9353d858-…` | 200 | SUCCESS | — | 7 resources |
| Recommendations (Sponsorship) | `sub:9353d858-…` | 200 | SUCCESS | — | 0 new |
| **Bug selector** costs/resources/recommendations | `acct:5` / `acct:6` | **404** | — | — | AccountNotFound |

`successful=0, failed=1` (single) and `successful=0, failed=2` (All) → SpendKpi renders
**Unavailable**.

## 12. Repeated All-Azure tests

All-Azure was validated repeatedly across the live script and manual curls; consistent
`status=PARTIAL_SUCCESS`, `total_cost=0.00`, `by_account=2`, `account_errors=2
(THROTTLED)` every time. No cache poisoning, no `$0.00` shown (handled by `Unavailable`).

## 13. True-zero validation

All-succeeded with zero spend → `successful_accounts=1, failed_accounts=0` → SpendKpi
renders **`$0.00`**. Covered by unit test
`renders genuine successful zero as $0.00 (complete data)`.

## 14. Partial-data validation

One success + N failures → `successful>0, failed>0` → subtotal + `Partial (N unavailable)`.
Covered by `renders partial subtotal with a Partial marker`; adapter counts verified by
`partial aggregation surfaces successful>0 / failed>0 with retained errors`.

## 15. All-failed / no-data validation

All subscriptions throttled → `successful=0, failed>0` → **Unavailable**, never `$0.00`.
Covered by `renders "Unavailable" when all subscriptions failed (never $0.00)` and the
adapter `all-failed aggregation surfaces successful=0 / failed>0`.

## 16. Dashboard stale-content cleanup

Removed from the Azure dashboard: the hardcoded **"AWS Spend"** label (now provider-aware
via `providerLabel="Azure Spend"`), the AWS-only subtitle conditional, and the obsolete
**"Coming in Phase 7C.2C"** / **"Coming in Phase 7C.2D"** placeholder descriptions and
"activate in Phase" capability-notices. Replaced with live messaging pointing to the
delivered Resources and Advisor pages. `AwsDashboard` keeps its AWS labels/behavior.

## 17. AWS regression

AWS selection unchanged (`acct:<row-id>`), verified by `canonicalAccountParam('acct:3','AWS')
=== 'acct:3'` and `AccountSelector still emits acct:<id> for AWS rows`. Live AWS legacy
route `/api/aws/costs` → HTTP 200. AWS SpendKpi renders `$2.81K` for the live ~$2811 MTD shape.

## 18. Provider-switch hotfix regression

The previous blank-dashboard fix (`bea38fd`) is preserved — the hotfix #2 commit sits on
top of it, and the per-provider `/` child components (`AwsDashboard`/`AzureDashboard` +
`DashboardErrorBoundary`) are untouched. The 3 hotfix-1 tests still pass
(`phase7c-hotfix-dashboard-switch.test.tsx`).

## 19. Test totals

- **Frontend**: **485 passed** (474 baseline + 11 new in
  `phase7c-hotfix-subscription-cost-correctness.test.tsx`), 0 failed, 50 files.
- **Backend** (in-container, Python 3.12): **996 passed**, 0 failed.
- Note: the local `backend/.venv` (Python 3.14) has a pre-existing SQLAlchemy/typing
  incompatibility that prevents running pytest from the host; the project runs backend
  tests inside the Docker image (Python 3.12.7), which passes. No backend source changed.

## 20. TypeScript / build

- `tsc --noEmit` in `frontend/`: clean.
- `vite build`: success — `dist/assets/index-BU_3WstS.js` (354 kB / 96 kB gzip).

## 21. Verification scripts

- `scripts/phase7b_verify.sh`: **PASS=101 / FAIL=0 / OVERALL PASS** (includes backend
  container build+up+pytest and frontend typecheck). The single diff vs the baseline
  `102` count is an environment-dependent PASS-count variance, not a failure.
- `scripts/phase7b_nginx_test.sh`: **PASS=45 / FAIL=0**.

## 22. Docker health

`docker compose ps` → **5/5 healthy** (postgres, litellm, backend, frontend, nginx).
The frontend image was rebuilt (`docker compose build frontend`) and the frontend +
nginx containers recreated. The rebuilt bundle was verified to contain the fix markers
(`sub:<guid>` selector ternary, `readStoredSelection` accepting `/^sub:…$/`,
`startsWith("sub:")` coercion, `"Unavailable"` / `"Partial ("` strings).

## 23. Files changed (commit `0c7fce1`, 9 files, +612/−19)

- `frontend/src/lib/accountContext.tsx` — `AccountSelection` union + `sub:` storage reader
- `frontend/src/components/AccountSelector.tsx` — `sub:<guid>` for Azure, label lookup
- `frontend/src/lib/finops/subscriptionSelection.ts` — **new** provider-aware coercion
- `frontend/src/lib/finops/cloudCosts.ts` — coercion in cache key + query param + adapter completeness
- `frontend/src/lib/finops/cloudResources.ts` — coercion in cache key + query param
- `frontend/src/lib/finops/cloudRecommendations.ts` — coercion in cache key + query param
- `frontend/src/types/finops.ts` — `CostReport` completeness fields
- `frontend/src/pages/DashboardPage.tsx` — provider-aware SpendKpi, Unavailable/Partial, stale-label cleanup
- `frontend/src/tests/phase7c-hotfix-subscription-cost-correctness.test.tsx` — **new** 11 regression tests

## 24. Security review

- No subscription GUIDs hardcoded in application source (selector reads from the API).
  Production test fixtures use obviously-fake GUIDs; the live-validation script's hardcoded
  GUIDs are a pre-existing test-harness artifact, not application code.
- No credentials, tokens, or secrets introduced. `apiFetch` uses the existing token mechanism.
- No `console.log`/`debugger` debug artifacts.
- No cloud resource mutations, no Azure writes; this hotfix is read-only reporting.
- The single `FAIL` in the pre-existing `phase7c2b_live_validation.sh` is its stale legacy
  `/api/cloud-accounts/selectable?provider=AZURE` URL (404); the real route
  `/api/cloud/accounts/selectable/AZURE` returns both subscriptions. Pre-existing, out of scope,
  not introduced by this hotfix.
- Git working tree clean after commit.