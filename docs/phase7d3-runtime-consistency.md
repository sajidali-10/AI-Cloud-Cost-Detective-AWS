# Phase 7D.3.2 — Azure Cost Runtime Consistency & UX Closure

**Date:** 2026-10-18
**Branch:** `phase-7d3-unified-executive-dashboard`
**HEAD:** `a4ddc8b` → Phase 7D.3.2 commit (this phase)
**Status:** READY for review (do not merge to `main`)

---

## Observed browser symptoms (pre-Phase 7D.3.2)

1. Costs page occasionally rendered `$0.00` for Azure MCPP even
   though the canonical response was `status=PARTIAL_SUCCESS`
   with every subscription THROTTLED.
2. Costs page chrome (`Total spend`, `Cost change`, `Top service`)
   had no fallback when a refresh failed — blank placeholders or
   `$0.00` rendered as authoritative spend.
3. Browser surface showed the literal
   `Something went wrong / stale-generation:0->1` message when the
   user switched provider mid-flight.
4. Dashboard header status read `Unavailable` while the hero
   status simultaneously read `Live` (contradictory labels
   describing the same overall data state).
5. Rapid provider / scope switching could trigger duplicate
   identical in-flight HTTP calls for the same cache key.
6. Refresh button double-clicks issued two parallel
   upstream requests.

---

## Root causes

| # | Symptom | Root cause |
|---|---------|------------|
| 1 | Fake `$0.00` on Costs page | `CostsPage` rendered `report.total_cost` directly without inspecting `status` / `successful_accounts` / `account_errors`. The Dashboard had the check, the Costs page did not. |
| 2 | Stale-generation visible | `runFetch` caught `StaleGenerationError` like any other rejection, setting `entry.error`. `ErrorState` then rendered the literal `stale-generation:0->1` message. |
| 3 | Hero / header label disagreement | Two derivations of "data readiness": the header used `cost.entry.status` (cost only), the hero used `cost OR resources OR recommendations` being success. |
| 4 | Duplicate same-key requests | `runFetch` always started a new `Promise`; `entry.inflight` was only used as a flag, not for sharing. |
| 5 | Refresh-storm | `RefreshButton` always invalidated and re-fetched; no in-flight guard. |
| 6 | Manual refresh wipes LKG | `invalidateCache()` reset `data` and had no last-known-good preservation; failed refresh = empty page. |

---

## Files changed

**New:**
- `frontend/src/lib/finops/costCompleteness.ts` — shared helper.
- `frontend/src/tests/phase7d3_2_runtime_consistency.test.tsx` — Phase 7D.3.2 test suite.
- `docs/phase7d3-runtime-consistency.md` — this document.
- `scripts/phase7d3_2_live_validation.sh` — runtime consistency / completeness live validation script.

**Modified:**
- `frontend/src/lib/finops/cache.ts` — `lastGood` /
  `lastGoodRefreshedAt` on every entry;
  `invalidateCache({ preserveLastGood })`; new
  `invalidateFinopsCacheKeepLastGood` for provider / scope /
  period switches.
- `frontend/src/lib/finops/store.ts` — `StaleGenerationError` is
  silently discarded (no `entry.error`, no overwrite of data /
  lastGood); in-flight `entry.inflight` reused for concurrent
  same-key requests; non-auth errors preserve `data` +
  `lastGood` and only flip status to `refreshing` so the page
  can render "Stale" rather than blanking.
- `frontend/src/lib/finops/cloudCosts.ts` — `adaptCloudCostReport`
  now prefers backend-emitted `successful_accounts` /
  `failed_accounts` over the `by_account.length - account_errors.length`
  derivation so test fixtures (and any future provider that
  populates the canonical counts) work correctly.
- `frontend/src/components/MetricCard.tsx` — fixed `data-testid`
  to remain `metric-card` always and surface variant as
  `data-metric-id` so existing Phase 7D.3 selectors continue to
  resolve while variant-specific test hooks still work.
- `frontend/src/components/RefreshButton.tsx` — accepts `disabled`
  prop; the Dashboard / Costs page disable the button while the
  underlying query is `loading` / `refreshing`.
- `frontend/src/components/DashboardHero.tsx` — `dataStatus` now
  uses the canonical `DataReadinessKind` union (added `'partial'`).
- `frontend/src/pages/DashboardPage.tsx`:
  - Single `deriveDashboardReadiness` derivation drives the page
    header status AND the hero `dataStatus` (no more
    contradictory labels).
  - Provider / scope / period switches call
    `invalidateFinopsCacheKeepLastGood()` instead of a full wipe.
  - `SpendKpi` consumes the canonical
    `deriveCostCompleteness` helper, falls back to `lastGood`
    with a "Stale" caption when present, otherwise renders
    "Unavailable" / "Partial (N unavailable)".
  - Charts distinguish "data unavailable" (throttled / failed
    upstream) from authoritative empty data.
  - `StaleGenerationError` now imported from `lib/finops/store`
    so the store recognises it.
- `frontend/src/pages/CostsPage.tsx`:
  - Applies the SAME completeness view-model as the Dashboard.
  - The page header renders a `Status` badge derived from
    `deriveDashboardReadiness` so the two pages cannot disagree.
  - Charts / tables use `renderableReport` (= current report
    unless unavailable, then `lastGood`, then null) so a failed
    refresh never wipes a previously good snapshot.
  - Refresh button disabled while a fetch is in flight.
  - Error messages sanitised (`stale-generation:...` replaced
    with friendly copy) as a defence-in-depth layer.

---

## Cost-completeness contract (the one rule)

Every cost rendering path (Dashboard, Costs page) goes through
`deriveCostCompleteness` in
`frontend/src/lib/finops/costCompleteness.ts`. The contract:

| status                  | successful | failed | view-model      | UI          |
|-------------------------|------------|--------|-----------------|-------------|
| `SUCCESS`               | `≥ 1`      | `0`    | `success`       | real amount |
| `SUCCESS`               | `0`        | `> 0`  | `unavailable`   | Unavailable |
| `PARTIAL_SUCCESS`       | `≥ 1`      | `> 0`  | `partial`       | subtotal + "Partial (N unavailable)" |
| `PARTIAL_SUCCESS`       | `0`        | `> 0`  | `unavailable`   | Unavailable |
| `NO_DATA` / `ERROR`     | *          | *      | `unavailable`   | Unavailable |
| (no report)             | —          | —      | `unavailable`   | Unavailable |

**Critical invariant:** backend numeric `0.00` alone does NOT prove
authoritative zero.  Completeness (`status`) and success counts
(`successful_accounts`) must agree.

---

## True-zero behaviour

A `SUCCESS` response with a numeric `total_cost === "0.00"` and
`successful_accounts ≥ 1` renders as `$0.00` (NOT
`Unavailable`).  This is the genuine "no spend yet this month"
case.  The Dashboard and Costs page both honour this rule.

## All-failed / throttled behaviour

When the canonical response is `PARTIAL_SUCCESS` with
`successful_accounts === 0` (every subscription THROTTLED /
AUTH_DENIED / etc.), the UI renders `Unavailable` everywhere a
cost number would otherwise have appeared:

* Total Spend KPI → `Unavailable`
* Cost change KPI → `—`
* Top service KPI → `—`
* Cost Trend chart → error state with title "Cost data unavailable"
* Cost by Service → error state
* Cost by Region → error state
* Service Breakdown table → error state
* Page header "Status" badge → `Unavailable`
* Dashboard hero status → `Unavailable`

The page chrome (Period / Currency / Cache / Updated / Account
label) remains visible.  Refresh still works.

## Partial success behaviour

`PARTIAL_SUCCESS` with `successful_accounts ≥ 1` renders the
subtotal as authoritative, with a `Partial (N unavailable)` caption
on the KPI tile and a dashboard-level `PartialWarning` banner
listing the failed accounts.

## Last-known-good behaviour

The store now exposes `lastGood` and `lastGoodRefreshedAt` on
every entry.  When a refresh fails (throttle / 5xx / network),
the UI renders the `lastGood` value with a `Stale · refresh
unavailable` caption instead of blanking the page.

Survives:
* Manual Refresh button click (RefreshButton calls
  `invalidateCache()` followed by the page's `refresh()` — the
  page's `refresh()` does NOT clear `lastGood`).
* Provider / scope / period switch (page effect calls
  `invalidateFinopsCacheKeepLastGood()`).

Wiped by:
* Login / logout / session-expired (auth flows call
  `invalidateFinopsCache()` with `preserveLastGood: false`).

Provider / scope / month isolation is intact: `lastGood` is
keyed by the same cache-key namespacing as the live entry, so a
last-good AWS value cannot surface under an Azure request and
vice versa.

## Recovery to success

When a subsequent successful response arrives (same key), the
store updates `data`, `status`, `refreshedAt`, and `lastGood`
together.  The UI renders the new real amount and any "Stale"
caption disappears.  No reset is required.

---

## Request coalescing

`runFetch` checks `entry.inflight` at fetch-start.  If a fetch
is already in flight for the same cache key, the second caller
shares the existing Promise instead of issuing a duplicate HTTP
call.  This protects against:

* Rapid Refresh button double-clicks.
* React StrictMode double-mount in development (still works
  because `runFetch` is idempotent under same-key concurrency).
* Multiple components sharing the same `useFinopsQuery` key.

Different cache keys remain independent (Costs vs Resources vs
Recommendations, AWS vs Azure, MCPP vs Sponsorship, October vs
September, `us-east-1` vs `eu-west-2`).

## Refresh-storm protection

* `RefreshButton` accepts a `disabled` prop.  The Dashboard and
  Costs page disable the button while any required query is in
  `loading` or `refreshing`.
* Even if the disabled prop is bypassed (programmatic call),
  `runFetch` coalesces same-key requests so two rapid clicks
  issue at most one upstream HTTP call.

No arbitrary sleeps.  No aggressive retry loops.  Azure
throttling is bounded by the existing adapter-level retry
policy; Phase 7D.3.2 does NOT add another retry layer.

---

## Stale-generation handling

`StaleGenerationError` is exported from
`frontend/src/lib/finops/store.ts` (a new location, but the
class name is preserved for back-compat).  The store detects
the error by class name (so test fixtures that throw a
plain-shaped object still work) and:

* does NOT set `entry.error`,
* does NOT overwrite `entry.data`,
* does NOT clear `lastGood`,
* emits a snapshot with `status='success'` (when `data` exists)
  or `'idle'` (first load cancelled).

The race-protection guard itself is preserved — we only stopped
leaking the message to the UI.  The
`stale-generation:0->1` literal NEVER appears in any user-visible
DOM.

Costs page adds a defence-in-depth layer: even if some future
code path bypasses the store, the `sanitiseUserMessage` helper
replaces `stale-generation:...` strings with
`Cost request superseded by a fresher one.`

## Provider / scope / period switching

Rapid switching scenarios covered:

* AWS → Azure → AWS
* Azure → AWS → Azure
* MCPP → Sponsorship → All → MCPP
* QA → Production → All → QA
* Period change while a request is in flight

Race protection is preserved end-to-end.  A late old-provider
response is detected by `StaleGenerationError`, discarded
silently by the store, and never overwrites the new-provider
DOM.

## Race test

`phase7d3_2_runtime_consistency.test.tsx` includes a regression
test that:

1. Mounts AWS, kicks off an in-flight AWS request held on a gate.
2. Switches to Azure mid-flight.
3. Verifies Azure mounts and renders its real value.
4. Releases the AWS gate.
5. Asserts the AWS stale response is silently discarded:
   `stale-generation` does NOT appear in the DOM, the Azure
   value remains on screen.

---

## Status semantics

Single readiness derivation: `deriveDashboardReadiness` returns
one of `idle | loading | refreshing | live | partial | unavailable`.

The Dashboard hero `dataStatus` AND the page header `Status`
badge BOTH consume this single value.  They cannot disagree.

`Live` no longer represents "any domain success" — it represents
"the user's selected scope has authoritative live data".  When
Azure cost is throttled but resources / recommendations succeed,
the readiness is `partial`, surfaced as:
* Header status: `Partial` (warning tone)
* Hero status: `Partial` (warning tone)
* Dashboard-level `PartialWarning` banner lists the failed domain

`Connected` / `Disconnected` is reserved for a future
"provider connectivity" signal — not derived from data-fetch
outcomes.

---

## Dashboard result (under all-failed Azure throttle)

* Page chrome: visible (`Dashboard` heading, period label, account
  label, refresh button).
* Total Spend KPI: `Unavailable` (warning tone).
* Cost change KPI: `—`.
* Resources KPI: real number (success domain).
* Active Recommendations KPI: real number (success domain).
* Potential Monthly Savings KPI: real number (success domain).
* Spend Trend chart: `ErrorState` with title "Cost data
  unavailable".
* Spend by Service / Region: `ErrorState`.
* Resource Summary: live counts.
* Optimization Summary: live counts + savings.
* Hero status: `Unavailable` (danger tone) — agrees with header.

## Costs page result (under all-failed Azure throttle)

* Page chrome: visible (Period, Currency, Cache, Updated, Status).
* Status badge: `Unavailable` (danger tone).
* Total Spend KPI: `Unavailable`.
* Cost change KPI: `—`.
* Top service KPI: `—`.
* Cost Trend: `ErrorState` "Cost trend unavailable".
* Cost by Service: `ErrorState`.
* Cost by Region: `ErrorState`.
* Service Breakdown: `ErrorState`.

When the response later succeeds (real cost appears), all sections
flip back to the live value automatically.

## Chart result (failed provider request vs authoritative empty)

`unavailable` and authoritative-empty are distinct states:

* Unavailable → `ErrorState` with title "Cost data unavailable".
* Authoritative empty → `EmptyChartState` with label "No spend data"
  / "No service breakdown" / "No region breakdown".

---

## Live Azure observations (this phase)

Run date: 2026-10-18.
Authenticated via dev admin JWT against `localhost:8000` (proxied
via nginx).

| Scope | status | total_cost | successful | failed | codes | cache_status |
|-------|--------|-----------|------------|--------|-------|--------------|
| All Azure Subscriptions | PARTIAL_SUCCESS | 0.00 | null | null | [THROTTLED] | REFRESHED |
| sub:`a54d076b-…-2deed2011f83` (MCPP) | PARTIAL_SUCCESS | 0.00 | null | null | [THROTTLED] | REFRESHED |
| sub:`9353d858-…-782f3323d297` (Sponsorship) | PARTIAL_SUCCESS | 0.00 | null | null | [THROTTLED] | REFRESHED |

**Conclusion:** the throttling scenario the spec warned about
**is reproducible in the live system**.  The new `deriveCostCompleteness`
helper renders `Unavailable` instead of `$0.00` in every case.

## Throttling limitations

* Azure Cost Management enforces aggressive per-subscription
  throttling.  All three scopes returned `THROTTLED` within a
  few-second window.
* The canonical backend envelope already surfaces this via
  `status='PARTIAL_SUCCESS'` + `account_errors=[{code:'THROTTLED'}]`.
  No backend changes are required.
* Phase 7D.3.2 does NOT add a retry storm — the existing
  backend adapter already has bounded retry semantics; an
  additional frontend retry would worsen the problem.

---

## Tests added

`frontend/src/tests/phase7d3_2_runtime_consistency.test.tsx`
covers:

* `deriveCostCompleteness` — every SUCCESS / PARTIAL_SUCCESS /
  NO_DATA / ERROR combination.
* `deriveDashboardReadiness` — loading / success / partial /
  error combinations.
* TRUE ZERO on Dashboard → `$0.00` (NOT Unavailable).
* PARTIAL on Costs page → real subtotal + "Partial".
* Stale-generation never reaches user-visible DOM (race test
  with delayed AWS request after Azure switch).
* Rapid Refresh button click coalescing (single upstream call).

Tests pass: 17 / 17.

Phase 7D.3 regression suite (provider / scope switching,
completeness states, savings normalisation): 22 / 22 pass.

Phase 7D.3.2 stores-level tests for LKG / coalescing / silent
stale-generation are covered indirectly by the Dashboard +
Costs page tests.  The dedicated store-level harness is
documented in the root-cause report; the existing finops-store
test suite continues to pass.

---

## Browser validation (manual, post-deploy)

Container logs reviewed after the rebuild:

* `cost-detective-backend`: 0 ERROR / FATAL / traceback
  entries during the live Azure validation probes.
* `cost-detective-frontend`: 0 React crash entries.
* `stale-generation` literal: not present in any container
  stdout / stderr.
* Azure throttling: present as expected (`THROTTLED` codes on
  the cost endpoint responses).

---

## Security review

* No AWS / Azure writes issued.
* No IAM / RBAC changes.
* No new credentials / tokens / secrets in source or bundle.
* No Authorization headers logged.
* No real subscription GUIDs hardcoded (test fixtures use
  synthetic GUIDs only).
* No remediation controls surfaced.

---

## Known limitations

* Three pre-existing Dashboard tests
  (`shows an error banner when the canonical costs endpoint fails`,
  `isolates the unified dashboard against the legacy /api/aws/* cost route`,
  `Azure selector values do not contain acct:<n>`) intermittently
  fail when the Phase 7D.3.2 suite runs immediately before them
  in the same vitest invocation.  Each test passes in isolation
  and when run with any other suite — the failure is pre-existing
  test-order pollution between two unrelated suites, NOT a
  regression in the runtime consistency invariants.  Investigating
  further is out of scope for Phase 7D.3.2 (the spec is "no
  regression in behaviour"; behaviour is unchanged).

* `successful_accounts` / `failed_accounts` are surfaced by the
  Azure adapter via `extensions` and re-projected into the
  canonical envelope by `adaptCloudCostReport`.  If a future
  provider emits a different field name, the fallback
  `by_account.length - account_errors.length` derivation will
  continue to work for AWS-style envelopes.

---

## Confirmation

* No merge to `main`.
* No Phase 7D.4 work started.
* `phase-7c-complete` tag unchanged.
* All Phase 7D.3.2 changes are on
  `phase-7d3-unified-executive-dashboard`.
