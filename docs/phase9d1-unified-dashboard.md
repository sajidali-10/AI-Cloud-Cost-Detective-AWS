# Phase 9D.1 — Unified Multi-Provider Dashboard (AWS / Azure / Rackspace)

Phase 9D.1 completes the unified dashboard: the Rackspace provider
became a first-class, user-selectable surface in the existing
provider-neutral frontend. Costs, Resources and Recommendations for
all three providers (AWS, Azure, Rackspace) render from the same
canonical `/api/cloud/*` envelopes through the same shared
components, with per-provider capability discovery, honest
unavailability handling, and cross-provider stale-data protection.

Batch A added the Rackspace provider + Costs UI (commit `28a049a`);
Batch B added the Rackspace Resources + Recommendations views
(commit `77db3bb`); Batch C (this document) finalized validation,
the phase validator, deployment and documentation.

## Architecture

The frontend remains a single provider-neutral pipeline. Rackspace
adds no new data path — it plugs into the canonical flow every
provider already shares:

```text
Browser (React SPA, static bundle served by nginx)
   ↓  HTTPS, JWT (Authorization: Bearer …) — never a cloud credential
nginx (TLS terminate, SPA, /api/* proxy)
   ↓
AI Cloud Cost Detective backend (FastAPI, canonical routes)
   ↓  authenticated service calls (server-side credentials only)
canonical cloud provider layer
   ├── AWS          (Cost Explorer / Resource Explorer / CloudWatch …)
   ├── AZURE        (Cost Management / Resource Graph / Advisor)
   └── RACKSPACE    (Identity + Billing V2 / Cloud Servers API, Phase 9A–9C)
```

Key frontend modules:

* `frontend/src/lib/providerContext.tsx` — provider selection state
  (`'AWS' | 'AZURE' | 'RACKSPACE'`), capability descriptors,
  `providerVersion` invalidation counter, sessionStorage persistence.
* `frontend/src/components/ProviderSelector.tsx` — top-bar selector;
  lists only registered + enabled providers.
* `frontend/src/lib/finops/cloudCosts.ts`,
  `cloudResources.ts`, `cloudRecommendations.ts` — canonical fetchers;
  every call is `/api/cloud/<surface>?provider=<KIND>`.
* `frontend/src/pages/CostsPage.tsx` — shared Costs page; RACKSPACE
  routes through the canonical branch (`useCanonical`).
* `frontend/src/pages/RackspaceResourcesView.tsx` — Rackspace
  resources view (Phase 9D.1, Batch B).
* `frontend/src/pages/OptimizationPage.tsx` — shared Recommendations
  page; RACKSPACE renders through the canonical advisory pipeline.

No frontend module calls a cloud vendor API directly. All browser
traffic terminates on the application backend.

## Provider selection behavior

* The selector renders only providers that are registered **and**
  enabled (`is_registered && is_enabled`). With the Rackspace
  adapter registered (Phase 9B) and its credentials configured,
  **Rackspace appears as a third selectable option** labelled
  "Rackspace" alongside AWS and Azure.
* Selection persists in `sessionStorage`
  (`cd.provider.selection.v1`) and resets to AWS on logout.
* The account/scope selector re-renders rows for the active
  provider only (Rackspace shows its billing-account scope;
  `acct:` selectors for Rackspace/AWS; `sub:` for Azure).
* If the persisted selection points at a provider that is no longer
  selectable, the UI falls back to AWS instead of getting stuck.

## Provider capability discovery

`GET /api/cloud/providers` advertises per-provider capabilities; the
UI drives availability from this discovery rather than hard-coding:

```text
AWS:        COSTS=SUPPORTED  RESOURCES=SUPPORTED  OPTIMIZATION(SUPPORTED)
AZURE:      COSTS=SUPPORTED  RESOURCES=SUPPORTED  OPTIMIZATION(SUPPORTED)
RACKSPACE:  COSTS=SUPPORTED  RESOURCES=SUPPORTED  OPTIMIZATION(SUPPORTED)
            (BILLING=SUPPORTED, AUTHENTICATION=SUPPORTED; ADVISOR/METRICS
             UNSUPPORTED — capability-gated, never silently attempted)
```

"Recommendations" in the UI matrix is the canonical `OPTIMIZATION`
capability. AWS and Azure capability rows are unchanged by 9D.1.

## Rackspace Costs

`GET /api/cloud/costs?provider=RACKSPACE`, envelope
`source=RACKSPACE_BILLING`.

* **Billing-cycle semantics.** Rackspace Billing answers for the
  *current billing cycle* only (anniversary-to-anniversary, e.g.
  live: `2026-10-19 → 2026-11-19`, 31 days). The canonical envelope
  carries the ACTUAL source period and the UI derives its label from
  it (`billingCycleLabel(report.period)`) — the page never blindly
  labels the data as a calendar month. The fallback label is
  "Current Billing Cycle".
* **Currency.** Rendered from the envelope (`currency=USD` live,
  `currency_status=OK`).
* **Service breakdown.** Rendered from `by_service` (live data
  includes "Un-itemized charges", Cloud Backup / Block Storage /
  Files / Next Gen Servers line items). Zero-value services render
  as `$0.00` line items — that is upstream truth, not fabrication.
* **Unsupported dimensions.** Rackspace Billing exposes no daily
  granularity and no regional split. The envelope returns **empty**
  `daily_trend` / `by_region` and the UI shows honest
  "not available" notices instead of a fake chart or a fake "0
  regions" table:
  * Daily trend: "Not available for the current Rackspace Billing
    integration. Rackspace Billing reports estimated charges for
    the current billing cycle without a daily breakdown."
  * Regional cost: "Not available … Rackspace Billing scopes
    charges to the billing account, not to individual datacenters."
* **Previous period.** Not exposed for the current cycle.
  `previous_period_cost` stays `null` with
  `previous_period_unavailable=true` — the UI renders "—" /
  "Previous period unavailable", never a fabricated `$0` baseline
  or a fake change percentage.

## Rackspace Resources

`GET /api/cloud/resources?provider=RACKSPACE`, envelope
`source=RACKSPACE_CLOUD_API`.

* **Summary.** `total_resources` reconciles with `by_type` and with
  the returned resource list (live: 50 = 34 servers + 16 volumes +
  0 load balancers; counts are live data, never hard-coded).
* **Server view.** Cloud Servers table with Status (ACTIVE etc.),
  Region (DFW/ORD/…), Flavor, and the shared "—" fallback for
  missing optional fields.
* **Volume view.** Block Storage table with size, region and an
  attachment badge: **ATTACHED** (success tone) is visually
  distinct from **DETACHED** (warning tone). Detached is evidence
  for review only — the resources view renders **no destructive or
  management actions** for Rackspace resources (asserted by test).
* **Load balancer empty state.** Zero Cloud Load Balancers renders
  a successful empty state ("no load balancers"), never an error.

## Rackspace Recommendations

`GET /api/cloud/recommendations?provider=RACKSPACE`, envelope
`source=RACKSPACE_OPTIMIZATION` (Phase 9C.1 deterministic engine).

* **Advisory semantics.** Findings are review guidance, not
  executable actions. RS-VOL-001 (detached volume) renders as
  REVIEW/advisory — never deletion language. No destructive action
  verb (RIGHTSIZE / DOWNSIZE / STOP / DELETE / TERMINATE …) appears
  in canonical Rackspace output, and the UI cannot generate its own
  findings: every row's `source` is the backend engine.
* **Severity / confidence.** `impact` (LOW/MEDIUM/HIGH) and
  `extensions.confidence` (LOW/MEDIUM/HIGH) render from canonical
  fields (live: 8 findings, all MEDIUM/MEDIUM-high engine output,
  all RS-VOL-001 on currently-detached volumes).
* **Evidence / limitations.** The advisory detail renders
  `extensions.evidence` and `extensions.limitations` lists.
* **Unknown savings.** All Rackspace findings currently have
  `estimated_savings = null`; the summary carries
  `savings_status=EMPTY`, `potential_savings=null`,
  `recommendations_with_savings=0`. The UI renders **"Not
  available" / "—"** for unknown savings — in rows, KPI tiles and
  the detail panel — and **never** `$0`, "$0 savings" or "No
  savings". A `$0` would fabricate certainty; "Not available"
  preserves the distinction between "zero" and "unknown".
* **ACTIVE-server safety.** ACTIVE servers produce zero
  recommendations: the engine never derives rightsizing evidence
  from ACTIVE + flavor/RAM/vCPU/age (asserted by tests and by the
  phase validator's live join between recommendations and
  resources).

## Loading, empty, PARTIAL_SUCCESS, UNAVAILABLE, ERROR

Shared states across all three surfaces:

* **Loading** — skeletons; no rows and no fabricated totals render
  while a request is pending.
* **Empty / NO_DATA** — a *successful* empty state with guidance,
  not an error (e.g. zero load balancers, zero recommendations).
* **PARTIAL_SUCCESS** — valid data renders together with a partial
  notice naming the failed scope/service (e.g. an honest
  "unavailable" section where a sub-surface failed).
* **UNAVAILABLE** — dimension-level unavailability (Rackspace daily
  trend / regional split / previous period) renders as explicit
  unavailable notices; data never appears as zeros.
* **ERROR** — a professional error state with retry; no fabricated
  rows. When a last-known-good snapshot exists
  (`keep-last-good` caching), the prior authoritative snapshot
  stays visible with a stale indicator instead of blanking.

## Cross-provider stale-data protection

* Every canonical cache key is **provider-prefixed**:
  `<provider>:cloud:costs:…`, `<provider>:cloud:resources:…`,
  `<provider>:cloud:recommendations:…` — an AWS response can never
  satisfy a Rackspace request or vice versa.
* A provider (or scope) switch bumps `providerVersion` and
  invalidates cache entries (keep-last-good variant), so the target
  provider's view shows a **loading state first** and only its own
  data afterwards.
* Component tests prove all four switch directions
  (AWS→Rackspace, Azure→Rackspace, Rackspace→AWS,
  Rackspace→Azure) render no stale previous-provider rows for
  resources and recommendations, and the cache-key isolation tests
  prove keys cannot collide.

## Security

* **Trust boundary.** The browser talks only to the application
  backend over the authenticated `/api/*` surface. Rackspace (and
  AWS/Azure) credentials live exclusively in the backend
  environment; the SPA bundle contains none.
* **No browser-side Rackspace integration.** Static scans of
  `frontend/src` and the production bundle find **zero** references
  to `RACKSPACE_API_KEY`, `RACKSPACE_BILLING_ACCOUNT`,
  `X-Auth-Token`, `api.rackspacecloud.com`, AWS or Azure direct-API
  endpoints. The only textual occurrences are **negative
  assertions** in tests (`expect(...).not.toMatch(/X-Auth-Token/)`).
* **Authentication.** All three canonical surfaces require the
  production JWT + RBAC exactly like AWS/Azure. No dev auth
  shortcut was added for validation — validators mint a token the
  same way the established Phase 7D.1 pattern does.
* **Read-only.** The Rackspace client performs GET-only product
  calls (write verbs = 0, spy-checked in Phase 9C.2).

## Test / build / validator results (Batch C closure)

| Check | Result |
| --- | --- |
| Frontend tests (`vitest run`, full suite) | **648/648 passed**, exit 0 |
| TypeScript (`tsc -p tsconfig.json --noEmit`) | clean, exit 0 |
| Lint | **NOT CONFIGURED** (no ESLint/Biome config or script in the repo; nothing introduced in a closure phase) |
| Production build (`npm run build`) | success, exit 0 (388 kB JS / 105 kB gzip) |
| `scripts/phase9d1_unified_dashboard_validation.sh` | **PASS=56 FAIL=0**, exit 0 |
| Focused 9D.1 component tests (inside validator) | 78 passed |

Focused Phase 9D.1 component-test files:

* `costs-page.test.tsx` (Rackspace billing-cycle wording,
  unavailable dimensions)
* `rackspace-resources.test.tsx` (canonical route, summary
  reconciliation, ATTACHED/DETACHED, LB empty state, partial/error,
  cross-provider isolation)
* `rackspace-recommendations.test.tsx` (canonical route, RS-VOL-001
  advisory wording, severity/confidence, evidence/limitations,
  unknown-savings ≠ $0, ACTIVE-server safety, cross-provider)
* `phase7d3-unified-dashboard.test.tsx` (provider context incl.
  RACKSPACE slug)
* `phase7c1-provider-context.test.tsx` / `phase7c1-cache-key-isolation.test.ts`
  (provider switching + cache-key isolation)

Backend regression gates (each run once against the deployed stack;
exit 0 = validation successful):

| Validator | Result |
| --- | --- |
| `phase9d1_unified_dashboard_validation.sh` | PASS=56 FAIL=0 — exit 0 |
| `phase9c2_rackspace_canonical_recommendation_validation.sh` | PASS=38 FAIL=0 — exit 0 |
| `phase9c1_rackspace_optimization_validation.sh` | PASS=17 FAIL=0 — exit 0 |
| `phase9b2_rackspace_resource_validation.sh` | PASS=29 FAIL=0 — exit 0 |
| `phase9b1_rackspace_cost_validation.sh` | PASS=26 FAIL=0 — exit 0 (first run hit a live upstream 500 — see Known limitations) |
| `phase9a_rackspace_poc_validation.sh` | PASS=17 FAIL=0 — exit 0 (same upstream blip; rerun green) |
| `phase8_4_final_security_validation.sh` | exit 0 — "RELEASE READY" (LiteLLM warning accepted while AI_ENABLED=false) |
| `phase7d5_release_validation.sh` | exit 0 — 0 failures (live LLM generation skipped: AI_ENABLED=false) |

AWS / Azure canonical regression (explicit probes, all `SUCCESS`):
costs, resources and recommendations for AWS and AZURE answered
`SUCCESS` after the Rackspace frontend integration.

## Phase 9D.1 validator

`scripts/phase9d1_unified_dashboard_validation.sh` follows the
existing validator conventions (PASS/FAIL counters, exit
`0`=success / `1`=validation failure / `2`=precondition failure),
is deterministic, read-only and never prints secret values. It
validates:

1. **Preconditions** — `.env` present; backend + frontend
   containers running; `ProviderRegistry` resolves RACKSPACE.
2. **Reachability** — backend health, frontend root and the HTTPS
   application route all answer 200 through nginx.
3. **Provider discovery** — AWS/AZURE/RACKSPACE all present with
   COSTS / RESOURCES / OPTIMIZATION = SUPPORTED.
4. **Rackspace canonical surfaces** — the three authenticated
   production endpoints return canonical envelopes; 32 envelope
   invariants checked (status/source/currency/period, unsupported
   dimensions empty-not-fabricated, previous-period null-not-zero,
   count reconciliation, ID uniqueness, unknown-savings semantics,
   severity/confidence, evidence/limitations, no destructive
   actions, RS-VOL-001↔detached-volumes join, ACTIVE-server
   safety, backend-only recommendation sources, LB-zero valid).
5. **Frontend source contracts** — RACKSPACE selectable in
   provider context; Costs/Resources/Recommendations UI route
   RACKSPACE through the canonical services; provider-prefixed
   cache keys; unknown savings render unavailable; no
   browser-side Rackspace credentials/API calls.
6. **Focused frontend tests** — the six Phase 9D.1 component-test
   files run inside the validator (skip with
   `PHASE9D1_RUN_NPM=0`).

## Docker deployment

The frontend is image-baked (`build: ./frontend`), so Batch C
rebuilt and recreated **only** the frontend service through the
normal Compose flow (`docker compose build frontend &&
docker compose up -d frontend`); PostgreSQL was untouched.

```text
cost-detective-backend    Up (healthy)        — rebuilt in Phase 9D.1 Batch A
cost-detective-frontend   Up (healthy)        — rebuilt + recreated in Batch C
cost-detective-nginx      Up (healthy)
cost-detective-postgres   Up (healthy)
cost-detective-litellm    health: starting    — accepted degraded state (AI_ENABLED=false)
```

Live endpoint checks after deployment: `http://localhost/` → 200,
`https://finops.hiplink.com/` → 200, `http://localhost/api/health`
→ 200.

## Manual browser acceptance checklist

### Login
* [ ] Login succeeds
* [ ] Dashboard loads

### AWS
* [ ] Select AWS
* [ ] AWS Costs load
* [ ] AWS Resources load
* [ ] AWS Recommendations load

### Azure
* [ ] Select Azure
* [ ] Azure Costs load
* [ ] Azure Resources load
* [ ] Azure Recommendations load

### Rackspace
* [ ] Select Rackspace

Costs:
* [ ] Current Billing Cycle amount displayed
* [ ] Currency displayed
* [ ] Billing cycle dates displayed
* [ ] Service breakdown displayed
* [ ] Daily trend shows unavailable rather than fake chart
* [ ] Regional cost shows unavailable rather than 0 regions
* [ ] Previous period unavailable is not shown as $0

Resources:
* [ ] Total resource count displayed
* [ ] Cloud Servers table displayed
* [ ] Server status/region/flavor fields displayed
* [ ] Block Storage table displayed
* [ ] ATTACHED/DETACHED visually distinguishable
* [ ] Detached volume does not show destructive action
* [ ] Zero Load Balancers shows valid empty state

Recommendations:
* [ ] Rackspace recommendation count displayed
* [ ] RS-VOL-001 rendered as REVIEW/advisory
* [ ] Severity displayed
* [ ] Confidence displayed
* [ ] Evidence/limitations available
* [ ] Unknown savings displays Not available / —
* [ ] Unknown savings never displays $0
* [ ] No fabricated ACTIVE-server rightsizing findings

### Provider switching
* [ ] AWS → Rackspace
* [ ] Azure → Rackspace
* [ ] Rackspace → AWS
* [ ] Rackspace → Azure
* [ ] Loading state appears
* [ ] No stale previous-provider costs
* [ ] No stale previous-provider resources
* [ ] No stale previous-provider recommendations

### Responsive / basic UX
* [ ] No overlapping dashboard cards
* [ ] Tables usable at normal laptop width
* [ ] Provider selector remains usable
* [ ] No obvious console/render errors

## Known limitations

* **Rackspace Billing upstream availability.** During Batch C's
  regression battery the Rackspace Billing API returned a transient
  HTTP 500 on `estimated_charges` (observed in backend logs,
  04:28:52). The canonical envelope correctly degraded to
  `UPSTREAM_ERROR` / 502 and Phase 9B.1's validator reported the
  upstream failure; the same validator passed 26/0 minutes later
  once upstream recovered. This is external availability, not an
  application defect — and it demonstrates the error envelope
  never masquerades as `$0`.
* **No daily/regional/previous-period cost dimensions for
  Rackspace** — inherent to Rackspace Billing's current-cycle
  estimated-charges model; rendered as explicit unavailability.
* **Savings are unknown (not zero) for Rackspace findings** — the
  deterministic engine does not fabricate dollar figures; the UI
  communicates "Not available".
* **LiteLLM restart loop** — `cost-detective-litellm` flaps
  (`health: starting`); accepted while `AI_ENABLED=false` and
  explicitly out of scope for Phase 9D.1.
* **Lint is not configured** in this repository (no ESLint/etc.);
  documented as-is, nothing introduced in a closure phase.

## Phase 9D.2 handoff

* The unified dashboard (AWS/Azure/Rackspace) is complete and
  regression-gated; Phase 9D.2 can start from a green
  `phase9d1_unified_dashboard_validation.sh` baseline.
* Open threads for 9D.2:
  * Rackspace invoice/charge-history dimensions (daily trend,
    regional split, previous-period comparison) if/when a Rackspace
    API surface provides them — the canonical envelope already
    carries the fields.
  * Savings estimation for Rackspace findings (requires a pricing
    source; the envelope supports `estimated_savings` +
    `savings_status=OK` when available).
  * LiteLLM stability (restart loop) — separate from dashboard
    work; unblocks AI analyst features when addressed.
* Do **not** merge or tag from Phase 9D.1; the branch
  `phase-9d1-unified-dashboard` is the handoff artifact.
