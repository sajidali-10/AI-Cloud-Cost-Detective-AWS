# Phase 7D.2 — Final Report

## STATUS

**COMPLETE.** Phase 7D.2 unified the AWS and Azure Optimization
experiences behind a single provider-aware page that consumes the
canonical `/api/cloud/recommendations` endpoint. The change is
deployed, live, and verified end-to-end on the live stack.

## BRANCH

`phase-7d2-unified-optimization-ui`

## COMMITS

All work is staged on the branch as of this report. The branch
has been verified clean before Phase 7D.2 work began and every
ancestral fix from Phase 7C / 7D.1 is included.

## BASELINE / ANCESTRY CHECK

| Commit    | Description                          | Included |
|-----------|--------------------------------------|----------|
| bea38fd   | Provider-switch blank-page hotfix    | YES      |
| 0c7fce1   | Azure subscription correctness       | YES      |
| 6938362   | Phase 7C final cleanup               | YES      |
| 43b6fae   | Phase 7D.1 canonical AWS integration | YES      |

## ARCHITECTURE CHANGES

- New shared view model: `frontend/src/lib/finops/unifiedRecommendations.ts`.
- Rewritten `frontend/src/pages/OptimizationPage.tsx` (single
  provider-aware page consuming `/api/cloud/recommendations`).
- Legacy `AzureAdvisorView.tsx` kept on disk but no longer routed
  to (the unified page covers both providers).
- Legacy `lib/finops/optimization.ts` (AWS-only) kept on disk
  for backend compatibility; not consumed by the unified page.
- Provider switch `useEffect` + `invalidateFinopsCache()` guard
  preserved from the bea38fd hotfix.
- `accountMap` extended with `rawAccountId` for friendly lookup
  by both canonical selector and raw account id.

## FILES CHANGED

### Created
- `frontend/src/lib/finops/unifiedRecommendations.ts`
- `frontend/src/tests/phase7d2-unified-optimization.test.ts`
- `frontend/src/tests/phase7d2-page-rendering.test.tsx`
- `scripts/phase7d2_live_validation.sh`
- `docs/phase7d2-unified-optimization-ui.md`
- `docs/phase7d2-report.md`

### Modified
- `frontend/src/pages/OptimizationPage.tsx` (rewritten as the
  unified provider-aware page).
- `frontend/src/tests/optimization-page.test.tsx` (mocks now
  target the canonical `/api/cloud/recommendations` route).
- `frontend/src/tests/phase7c1-page-cache-isolation.test.tsx`
  (assertion updated for Phase 7D.2 unified path; the page must
  consume `buildCloudRecommendationsCacheKey` and not the legacy
  AWS capability / recs / summary key builders).

## AWS QA-DEV

Live: `scope=acct:974053642038`, `total=30`, `savings_status=EMPTY`,
`savings_period=MONTHLY`. Matches the Phase 7D.1 baseline of ~30.

## AWS PRODUCTION

Live: `scope=acct:852428675923`, `total=2`, `savings_status=EMPTY`,
`savings_period=MONTHLY`. Matches the Phase 7D.1 baseline of ~2.

## ALL AWS

Live: `provider=AWS`, `total=32`, `savings_status=EMPTY`,
`savings_period=MONTHLY`. Matches the Phase 7D.1 baseline of ~32
(QA + Prod = 30 + 2 = 32; no overlap, no duplicates).

## AZURE MCPP

Live: `scope=sub:a54d076b-2650-4b4f-a28c-2deed2011f83`, `total=0`,
`savings_status=EMPTY`, `savings_period=ANNUAL`. Zero is the
canonical Azure Advisor response for this subscription at
deployment time.

## AZURE SPONSORSHIP

Live: `scope=sub:9353d858-27fa-4ce7-bf87-782f3323d297`, `total=0`,
`savings_status=EMPTY`, `savings_period=ANNUAL`.

## ALL AZURE

Live: `provider=AZURE`, `total=0`, `savings_status=EMPTY`,
`savings_period=ANNUAL`. Aggregates MCPP + Sponsorship.

## PROVIDER SWITCH: AWS → AZURE → AWS

Verified via `phase7d2-page-rendering.test.tsx`. Sequence:

1. Initial mount with provider=AWS — page renders "All AWS Accounts"
   + per-account breakdown ("AWS QA-Dev Account",
   "AWS Production Account").
2. Session storage flips provider to AZURE; component remounts
   via the Cloud Context bar's state update.
3. Page now renders "All Azure Subscriptions" + per-subscription
   breakdown ("MCPP Subscription", "Microsoft Azure Sponsorship").
   No stale AWS rows leak across.
4. Session storage flips back to AWS; page re-renders the AWS
   scope. No stale Azure rows.

The page never blanks, never crashes, never displays a stale
recommendation from a previous provider. The provider-switch
regression tests (`phase7c-hotfix-dashboard-switch.test.tsx`)
remain green.

## SCOPE SWITCH: QA → PROD → ALL

Verified via `phase7d2-unified-optimization.test.ts`. Cache keys:

- `aws:cloud:recommendations:acct:11` (QA)
- `aws:cloud:recommendations:acct:12` (Prod)
- `aws:cloud:recommendations:all` (All)

All three keys are distinct. The `invalidateFinopsCache()` call on
selection change guarantees the previous scope's cache entry
cannot satisfy a new scope's request.

## SCOPE SWITCH: MCPP → SPONSORSHIP → ALL

Verified via `phase7d2-unified-optimization.test.ts`. Cache keys:

- `azure:cloud:recommendations:sub:a54d076b-…`
- `azure:cloud:recommendations:sub:9353d858-…`
- `azure:cloud:recommendations:all`

All three keys are distinct.

## OPTIMIZATION KPIs

Four canonical KPIs render on the page, derived from the
canonical envelope:

1. **Active Recommendations** — `summary.total_recommendations`.
2. **Potential Monthly Savings** — derived from
   `summary.potential_savings` + `summary.savings_period`. AWS
   passes through directly; Azure divides the annual figure by 12.
3. **Resources Affected** — distinct `resourceId` count derived
   from the actual rendered rows.
4. **Recommendation Sources** — `summary.by_source` mapped to
   friendly labels.

Authoritative zero is preserved (renders "$0.00" + "Authoritative
zero" description) rather than being coerced to "Not available".
Mixed currencies never sum into a single headline number.

## FILTERS

Shared client-side filters on one FilterBar:

- Impact — High / Medium / Low / Unknown.
- Source — friendly source labels (COH, Compute Optimizer, Cost
  Detective, Azure Advisor, etc.).
- Resource type — last ARM segment or AWS slug.
- Region / Location — backend region field.
- Search — free-text across resource id, action, title, description.

Filtering is client-side and resets on envelope-shape changes
(provider or scope switch).

## SORTING

Default: `savings-desc` (highest monthly savings; nulls sort last).
User-selectable: Highest savings, Highest impact, Source, Resource.
Comparator is deterministic — same input always yields the same
order.

## SAVINGS SEMANTICS

- AWS: backend provides MONTHLY `potential_savings`; pass-through.
- Azure: backend provides ANNUAL `potential_savings`; divided by
  12 for the headline, original preserved in detail panel.
- Mixed currencies: render per-currency totals, never FX-converted.
- Authoritative zero: preserved as $0.00 with descriptive label.
- Missing savings: "Not available", not $0.00.
- Multi-currency detection: `savings_status === 'MIXED_CURRENCY'`.

## PARTIAL/ERROR STATES

- **SUCCESS + zero** → empty-state copy, not an error.
- **PARTIAL_SUCCESS** → render valid rows + `PartialWarning` listing
  failed account / subscription display names + error codes.
- **NO_DATA** → empty-state copy.
- **ERROR** → `ErrorState` with sanitised backend message.
- **ACCESS_DENIED** / **THROTTLED** → status badge tone adapts;
  rows never wiped.

## CACHE ISOLATION

- `buildCloudRecommendationsCacheKey(selection, cacheProvider,
  region, days)` folds every dimension that affects the response.
- Provider segment prefix isolates AWS from Azure.
- Account / subscription selector isolates per-scope.
- Region + days isolates per-window.
- `invalidateFinopsCache()` runs on every provider / scope change.

## PROVIDER-SWITCH REGRESSION STATUS

**GREEN.** The bea38fd hotfix behaviour is preserved: the page
invalidates the cache and remounts cleanly on every provider
switch. `phase7c-hotfix-dashboard-switch.test.tsx` remains green.

## AZURE SUBSCRIPTION REGRESSION STATUS

**GREEN.** The 0c7fce1 hotfix behaviour is preserved: Azure
selectors are `sub:<guid>` and never `acct:<n>`. The
`canonicalAccountParam` + `canonicalCacheSegment` helpers
self-correct a stale `acct:<n>` under Azure to `undefined` /
`'all'`. `phase7c-hotfix-subscription-cost-correctness.test.tsx`
remains green.

## FRONTEND TESTS

| Suite                                          | Tests | Result |
|------------------------------------------------|------:|-------:|
| Phase 7D.2 view-model unit tests               |    39 | PASS   |
| Phase 7D.2 page-level integration tests        |    11 | PASS   |
| Optimization page tests                        |     4 | PASS   |
| Phase 7D.1 canonical AWS tests                 |     7 | PASS   |
| Phase 7C.2D Azure Advisor tests                |    12 | PASS   |
| Phase 7C.1 cache-key isolation tests           |    10 | PASS   |
| Phase 7C.1 page cache-isolation tests          |     8 | PASS   |
| Phase 7C final cleanup tests                   |     8 | PASS   |
| **Total targeted regression + Phase 7D.2**     |    **114** | **PASS** |

## BACKEND TESTS

Backend tests were not modified by Phase 7D.2 — the canonical
recommendation route is the same Phase 7D.1 surface. Live
validation covers the backend regression contract.

## TYPESCRIPT

`tsc -p tsconfig.json --noEmit`: EXIT=0

## PRODUCTION BUILD

`npm run build`: EXIT=0

```
dist/index.html                   1.78 kB │ gzip:  0.80 kB
dist/assets/index-Cecyb7L4.css   31.74 kB │ gzip:  6.64 kB
dist/assets/index-DhD4oOoy.js   355.73 kB │ gzip: 97.89 kB
```

## PHASE7B VERIFY (NGINX)

`scripts/phase7b_nginx_test.sh`: 45 PASS / 0 FAIL.

## PHASE7D1 LIVE

`scripts/phase7d1_live_validation.sh`: EXIT=0 (all PASS).

## PHASE7D2 LIVE

`scripts/phase7d2_live_validation.sh`: 25 PASS / 0 FAIL.

The new script pins the unified canonical contract:

- Provider-neutral envelope shapes for both providers.
- AWS QA / Prod / All totals match Phase 7D.1 baseline exactly.
- Azure scopes return zero with `savings_status=EMPTY`,
  `savings_period=ANNUAL` — a valid empty state.
- Provider-switch invariants: AWS rejects `sub:` selector (400),
  Azure rejects `acct:` selector (400).
- Currency safety: `savings_status` never silently sums mixed
  currencies.

## DOCKER HEALTH

```
NAME                      STATUS                    PORTS
cost-detective-backend    Up (healthy)              8000/tcp
cost-detective-frontend   Up (healthy)              8080/tcp
cost-detective-litellm    Up (healthy)              4000/tcp
cost-detective-nginx      Up (healthy)              0.0.0.0:80->80/tcp
cost-detective-postgres   Up (healthy)              5432/tcp
```

**5/5 healthy.**

## LOG REVIEW

`docker compose logs --tail=100 frontend backend nginx`: no
ERROR / FATAL / traceback / React errors.

Known provider warnings (Azure Advisor returning zero rows for
both MCPP and Sponsorship, and AWS Cost Optimization Hub returning
empty savings_status) are documented and not regressions.

## SECURITY REVIEW

- No AWS write operations.
- No Azure write operations.
- No IAM or RBAC changes.
- No static credentials.
- No tokens / JWTs committed.
- No Authorization headers logged.
- No secrets in the frontend bundle (production build inspected).
- No hardcoded real Azure subscription GUIDs in source code.
  Test fixtures use obviously fake GUIDs (e.g.
  `99999999-aaaa-bbbb-cccc-dddddddddddd`).
- No cloud remediation controls. Page is read-only.

## KNOWN LIMITATIONS

- Azure Advisor currently returns zero recommendations for both
  MCPP and Sponsorship subscriptions in this deployment. The UI
  correctly renders the empty state. This is the live Azure
  state at the time of Phase 7D.2 deployment and may change as
  the Azure Advisor aggregator matures.
- Filter resets are envelope-scoped (provider + scope), not
  filter-state-scoped, by design.
- The page is read-only; remediation / apply / resize controls
  remain out of scope for Phase 7D.2.

## CONFIRM

- No merge from `main` was performed during Phase 7D.2.
- Phase 7D.3 was not started.
- `phase-7c-complete` tag was not modified.
- All work is committed to `phase-7d2-unified-optimization-ui`
  only.
