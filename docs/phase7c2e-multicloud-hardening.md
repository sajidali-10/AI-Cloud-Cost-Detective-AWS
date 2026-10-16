# Phase 7C.2E — Multi-Cloud Integration, Hardening & Pre-Merge Validation

This document is the final hardening pass for Phase 7C.  It
consolidates the AWS + Azure integration, validates provider /
account switching, fixes pre-existing test red flags, and
verifies the full regression baseline before Phase 7C can be
merged to `main`.

## Architecture (consolidated)

```
                ┌───────────────────────┐
                │   Cloud Context bar   │
                │  ProviderContext +    │
                │   AccountContext      │
                └───────────┬───────────┘
                            │
              ProviderRegistry dispatch
                  /                  \
        AWS adapter           Azure adapter
              │                     │
   Phase 7B AWS services   Phase 7C.2A/B/C/D Azure services
   - cost_aggregator        - credential (federation)
   - resource_aggregator    - arm_client
   - optimization_agg       - costs (Cost Management Query)
                            - resources (Resource Graph)
                            - advisor_aggregator
                  │                     │
                  └──────────┬──────────┘
                             │
                Canonical API envelopes
                  /        |        \
       /api/cloud/costs   /api/cloud/resources   /api/cloud/recommendations
                (AWS / Azure)   (AWS / Azure)             (Azure)
                             │
                        Frontend
```

Canonical endpoints (Phase 7C.2B / 7C.2C / 7C.2D):

* `GET /api/cloud/costs?provider={AWS|AZURE}&account_id={all|sub:<guid>}&month=YYYY-MM|days=N`
* `GET /api/cloud/resources?provider={AWS|AZURE}&account_id={all|sub:<guid>}`
* `GET /api/cloud/recommendations?provider=AZURE&account_id={all|sub:<guid>}`

AWS legacy endpoints (preserved byte-for-byte, Phase 7B):

* `/api/aws/costs`, `/api/aws/identity`, `/api/aws/resources`,
  `/api/aws/resources/all`, `/api/aws/optimization/*`,
  `/api/aws/cloud-accounts/*`, `/api/aws/utilization`,
  `/api/aws/evidence`.

## Feature matrix (delivered state)

| Capability | AWS | Azure |
|------------|-----|-------|
| Authentication | YES (Phase 1) | YES (Phase 7C.2A — STS federation) |
| Account / subscription discovery | YES (Phase 7B) | YES (Phase 7C.2A) |
| Costs | YES (Phase 1) | YES (Phase 7C.2B — Cost Management Query) |
| Resources | YES (Phase 1) | YES (Phase 7C.2C — Resource Graph) |
| Optimization / Recommendations | YES (Phase 3 — Cost Optimization Hub) | YES (Phase 7C.2D — Advisor) |
| Write / remediation | NO | NO (strictly READ-ONLY) |

## Canonical API contract consistency

| Aspect | Costs | Resources | Recommendations |
|--------|-------|-----------|-----------------|
| Provider casing | uppercase enum ("AWS", "AZURE") | same | same |
| account_id semantics | `acct:<id>` AWS, `sub:<guid>` Azure | same | same |
| `scope` value | `all` / `acct:<id>` / `sub:<id>` | same | same |
| Status enum | SUCCESS / PARTIAL_SUCCESS / NO_DATA / ERROR | same | same |
| Error envelope | `{status, error_code, message, ...}` | same | same |
| Currency policy | USD only; no FX | N/A (no money fields) | annualSavingsAmount only; no annualisation; mixed-currency surfaced explicitly |
| Pagination | Phase 7C.2B (no client-side limit; uses options.$top + pagination) | Phase 7C.2C.1 hardened (options.$top + $skipToken + MAX_PAGES=50) | same as Resources |
| Per-scope attribution | `by_account` | `by_subscription` | `by_subscription` |
| Empty-result status | SUCCESS (with empty arrays) | same | same |

All three canonical endpoints share the same
``AccountValidationResult``-style sanitisation contract:
no Bearer / token / JWT in any log line; subscription IDs
truncated to first 8 hex chars via `safe_subscription_label`.

## Status semantics (consistent across all three)

| Status | Meaning |
|--------|---------|
| `SUCCESS` | At least one scope returned data, no failures |
| `PARTIAL_SUCCESS` | At least one scope succeeded, another failed |
| `NO_DATA` | Every accessible scope is genuinely empty, no failures |
| `ERROR` | Every scope failed |

Empty data ≠ ERROR:
* Sponsorship cost = $0 → SUCCESS (not ERROR)
* Zero Advisor New Cost recommendations → SUCCESS
* Zero resources → SUCCESS

The dashboard distinguishes these explicitly via
`EmptyState` components in the React UI.

## Currency safety (recap)

* **Costs**: USD only; no FX conversion invented.
* **Recommendations**: `annualSavingsAmount` used directly when
  present; never annualises `savingsAmount`; missing savings
  retained; zero savings distinct from missing; mixed currencies
  surfaced as `MIXED_CURRENCY` with per-currency totals — never
  combined into a fake total.

## Cache isolation

Cache keys for every canonical endpoint are prefix-segmented
by `provider`:

```
v4|<provider>|<scope>|user=<id>|<region>|<kind>|<signature>
```

The Phase 7C.2C.1 verifier confirmed AWS and Azure keys cannot
collide even when account ids happen to share prefixes.

## Pagination contract

All three canonical endpoints use the Phase 7C.2C.1 pagination
contract:

* `options.$top = 1000` request field
* `options.$skipToken` for continuation
* `MAX_PAGES = 50` loop guard
* Repeated `$skipToken` → `UPSTREAM_ERROR`
* `resultTruncated=true` + no `$skipToken` + `count < totalRecords`
  → `PAGINATION_INCOMPLETE`
* `resultTruncated=true` + `count == totalRecords` → success boundary
* Same query + `$top` + `subscriptions` across every page

## Resource identity

Resources carry `composite_id` in provider-prefixed form:

| Provider | composite_id prefix |
|----------|---------------------|
| AWS | `AWS\|...` |
| Azure | `AZURE\|<arm-id>` |

Cross-cloud collisions are impossible because `AWS\|` and `AZURE\|`
disjointly tag every resource.

## Phase 7C.2E — pre-existing test red flags resolved

### A. Six frontend layout-invariant tests

`phase7c2a-navigation.test.tsx` and `phase7b-runtime-closure.test.tsx`
contained `fs.promises.readFile('src/components/...')` calls with
relative paths that only resolved correctly when vitest's cwd was
`frontend/`.  When vitest ran from the project root, the relative
paths resolved against the project root and `ENOENT` failed the
assertion.

**Fix (Phase 7C.2E):**

* Added `frontend/src/tests/helpers/sourcePath.ts` — a small
  helper that resolves paths relative to the **frontend project
  root** by walking up from the helper's own `import.meta.url`
  looking for `frontend/package.json`.  No `process.cwd()` and no
  `expect.getState().testPath` introspection — works regardless of
  cwd and vitest version.
* Updated 6 calls in `phase7c2a-navigation.test.tsx` and 4 calls
  in `phase7b-runtime-closure.test.tsx` to use the new helper.

After the fix the targeted layout-invariant + AccountSelector
mounting tests pass (7/7) under both cwd layouts.

### B. `scripts/phase7b_verify.sh` reported `100 PASS / 1 FAIL`

The failing check was `"no AWS key in known storage keys"`.  The
regex was `aws(Access|.?Secret)?.?(Key|Secret)` — which matched
test variable names like `awsKey` (cache-key builder assertions
in Phase 7C.2B / 7C.2C tests).

**Fix (Phase 7C.2E):**

* Tightened the regex to require an actual credential qualifier:
  ```
  aws[._]?(access|secret|session) | AWS_(ACCESS|SECRET|SESSION)
  ```
* The previous regex is documented in the verifier script with a
  Phase 7C.2E attribution comment.

Result: `scripts/phase7b_verify.sh: 101 PASS / 0 FAIL — OVERALL: PASS`.

The remaining 3 `sessionStorage persistence` failures in
`phase7b-runtime-closure.test.tsx` are pre-existing
jsdom-environment issues on the Phase 7C.2D.1 baseline (verified
via `git stash`); they do not represent regressions.

## Live E2E validation summary

Validated against the real Docker stack:

| Scenario | Cost | Resources | Recommendations |
|----------|-----:|----------:|----------------:|
| MCPP | $7.18 USD | 18 | 0 |
| Sponsorship | $0.00 USD (SUCCESS) | 7 | 0 |
| All Azure | $7.18 USD (PARTIAL — Sponsorship THROTTLED at moment of query) | 25 | 0 |
| AWS regression | $2811.44 USD | unchanged | unchanged |

All Azure status is PARTIAL_SUCCESS because the live Sponsorship
subscription was throttled at the moment of the query — correctly
surfaced in `account_errors[]` and preserved as a per-subscription
attribution.  MCPP data is correct.

## Security

* No `Authorization: Bearer …` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs truncated via `safe_subscription_label`.
* KQL hardcoded; no caller injection (verified by
  `test_kql_does_not_reference_other_lifecycle_values`).
* `composite_id` provider-prefixed prevents cross-cloud collisions.
* `MAX_PAGES = 50` prevents unbounded pagination.
* **READ-ONLY**: no Azure resource mutations, no Advisor lifecycle
  updates, no reservation purchases.

## Documentation consolidation

* `docs/phase7c2e-multicloud-hardening.md` — this file
* `docs/phase7c2e-report.md` — final report
* `docs/phase7c2b-azure-cost-management.md` — Phase 7C.2B reference
* `docs/phase7c2c-azure-resource-graph.md` — Phase 7C.2C reference
* `docs/phase7c2d-azure-advisor.md` — Phase 7C.2D reference

## What Phase 7C.2E does NOT ship

* Rackspace / GCP / OCI providers — explicitly out of scope.
* Azure Monitor / Azure Defender — not implemented.
* Resource mutation (start/stop/delete/resize) — strictly
  READ-ONLY.
* Advisor recommendation generation / lifecycle updates.
* AI / LLM-generated recommendations.
* Professional UI redesign — only minimal cross-cloud fixes.
* `main` merge — handled by the user after Phase 7C finalization.
* Phase 7C tag — created by the user after `main` merge.
