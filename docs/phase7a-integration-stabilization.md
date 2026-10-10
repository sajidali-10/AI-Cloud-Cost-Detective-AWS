# Phase 7A — Integration Stabilization

This document captures the Phase 7A architecture, the five issues
addressed, and the operator-facing extension points that Phase 7B
and 7C will need.

## Scope

Phase 7A is an **integration stabilization** phase, not a redesign.
The Phase 0–6 contracts are preserved; the calendar-month billing,
authorization lifecycle, optimization timeout, and AI-provider
wiring are surgically improved on top of the existing architecture.

**Out of scope** (deferred):

* Multi-account AWS (Phase 7B).
* Azure / Rackspace (Phase 7C).
* HTTPS termination.
* AWS IAM changes; AWS service enrollment.
* New LLM provider SDKs in the backend (LiteLLM only).

## Architecture invariants (preserved)

1. **Browser → backend → LiteLLM → provider.** Never browser → provider.
2. **Read-only AWS.** `app/services/aws/guard.py` is unchanged.
3. **Centralized API client.** All `/api/aws/*`, `/api/ai/*`, and
   `/api/conversations/*` calls go through `frontend/src/lib/api.ts`,
   which is the only module that adds the `Authorization: Bearer`
   header.
4. **JWT transport.** Header for REST, `Sec-WebSocket-Protocol:
   bearer.<jwt>` for WebSocket. Never in URLs or query strings.
5. **Capability error model.** `AVAILABLE / INACTIVE / NOT_ENROLLED /
   TIMED_OUT / UNAVAILABLE / FAILED`. Inactive enrollment is not an
   application failure.

## Issue 1 — Authorization propagation

### Root cause

`useFinopsQuery` (in `frontend/src/lib/finops/store.ts`) fires the
network request in a `useEffect` that runs on mount. While the
`AuthProvider` is still in `'booting'` or `'unauthenticated'` after
the `/api/auth/me` probe, the centralized `apiFetch` client sees
`getToken()` return `null` and emits a request **without an
Authorization header** → backend returns `401 missing Authorization
header`. Other hooks that mount later receive a token and succeed,
which produced the inconsistent browser symptom.

### Fix

* The new `useFinopsQuery` reads `useAuth()` and only fires when
  `authReady = auth.ready && (auth.user !== null || !auth.authEnabled)`.
* `runFetch()` treats `ApiError(401|403)` as a non-cacheable failure:
  the entry's data and `refreshedAt` are wiped, so the next mount
  (after login) automatically re-fetches with the new token.
* `AuthProvider` calls `invalidateFinopsCache()` on
  `login`, `logout`, and `handleSessionExpired` — no stale
  authenticated response from user A can surface under user B.
* Cache primitives were extracted into `frontend/src/lib/finops/cache.ts`
  to break the import cycle between `auth.tsx` and `store.ts`.

### Coverage

* `frontend/src/tests/phase7a-cache-auth.test.tsx` — four cases
  pinning the contract.
* `scripts/phase7a_verify.sh` — grep checks confirming every FinOps
  module uses `apiFetch`, no module bypasses it, and no JWT literal
  appears in any shipped source.

## Issue 2 — Optimization timeout

### Root cause

The `/api/aws/optimization/recommendations` and `.../summary`
handlers run CloudWatch batch queries against every EC2 / RDS /
Lambda / ALB / NLB resource before the orchestrator runs. A slow
`GetMetricData` page or a large inventory blocks the entire handler
past the 25 s frontend budget, even when **both** AWS-native
sources (Compute Optimizer, Cost Optimization Hub) were already
gated out as `INACTIVE` / `NOT_ENROLLED`.

The orchestrator had no per-source timeout, so the deterministic
engine's recommendations could not surface until **all** upstream
calls returned.

### Fix

* Each AWS-native fetcher (Compute Optimizer, Cost Optimization Hub)
  and the CloudWatch utilization batch now run in a dedicated thread
  with a hard timeout budget (`PROVIDER_TIMEOUT_SECONDS`).  Defaults:
  * `compute_optimizer`: 6.0 s
  * `cost_optimization_hub`: 6.0 s
  * `cloudwatch_utilization`: 10.0 s

  Tunable via `OPTIMIZATION_TIMEOUT_*` env vars.
* On breach the engine emits a structured `OptimizationNotice` with
  `code="TIMED_OUT"` and **continues**. The deterministic engine
  always runs to completion so valid in-house recommendations remain
  usable even when every AWS source is unresponsive.
* `OptimizationNotice.code` is a free-form string (already
  validated by the Pydantic schema) — `TIMED_OUT` joins the existing
  set without a schema bump.

### Coverage

* `backend/tests/test_phase7a_optimization_timeout.py` — three
  cases pinning `TIMED_OUT` emission and the deterministic engine's
  resilience.
* `scripts/phase7a_verify.sh` — grep checks for the budget map,
  `_run_with_budget`, and the deterministic-always-runs contract.

## Issue 3 — Calendar-month billing

### Backend extension

`backend/app/services/aws/cost_explorer.py` adds:

* `parse_month("YYYY-MM")` — strict format check.
* `build_month_period("2026-09")` → `(2026-09-01, 2026-10-01)`.
* `build_month_period("2026-10")` (current month) →
  `(2026-10-01, today+1)` — MTD clip.
* `previous_month_period("2026-09")` → full prior month.
* `matched_mtd_previous_period("2026-10", today=…)` →
  `(2026-09-01, 2026-09-10)` — fair MTD delta for current-month
  requests. Clamps to the previous month's actual length.
* `list_recent_calendar_months(today, count=12)` → UI selector list.
* `build_explicit_period(start_date, end_date)` → explicit UTC range.

`/api/aws/costs` now accepts the request parameters in this order of
precedence:

1. `?month=YYYY-MM` — primary Phase 7A contract.
2. `?start_date=&end_date=` — explicit UTC range.
3. `?days=7|30|60|90` — legacy back-compat.
4. `days=30` — final fallback.

Cache keys are partitioned by `kind|signature|account|region|db` so
calendar-month, explicit, and rolling-lookback entries never collide.

### Frontend

* `frontend/src/types/finops.ts` adds the discriminated union
  `BillingPeriod = { kind: 'month', month } | { kind: 'days', days }`.
* `buildMonthList(today)` returns the last 12 calendar months.
* `monthLabel("2026-09")` returns `"September 2026 · Full month"`.
  `monthLabel("2026-10")` (current month) returns
  `"October 2026 · Month-to-Date"`.
* `FinopsPeriodProvider` carries the BillingPeriod; a derived
  `days` is exposed for the AI WS protocol which still requires
  `LookbackDays` on its frames.
* `PeriodSelector` is now a `<select>` over the 12-month list.
* The Dashboard, Costs, and Optimization pages read the period
  through the provider so header, hero, KPI tiles, and chart
  descriptions all show the same `"October 2026 · Month-to-Date"`
  label.

### Matched-MTD vs full-prior-month

For current-month requests, the comparison period is matched MTD
(Oct 1–9 vs Sep 1–9). For completed-month requests the comparison
is the full prior calendar month (Sep vs Aug). The backend wires
this in `aws_costs.py` by detecting whether the requested month is
the current month **before** clipping the period.

### Coverage

* `backend/tests/test_phase7a_calendar_month.py` — 13 cases pinning
  parser, period builder, MTD clamp, list helper, and explicit range.
* `frontend/src/tests/phase7a-period.test.ts` — 7 cases pinning the
  helper functions.
* `scripts/phase7a_verify.sh` — grep checks for the new contract.

## Issue 4 — AWS optimization capability states

### What already works

The Phase 3 orchestrator already:

* Gated the AWS-native fetchers on enrollment status (skips when
  `INACTIVE`, `NOT_ENROLLED`, `PENDING`, `FAILED`).
* Surfaced `INACTIVE` and `NOT_ENROLLED` as the actual AWS-reported
  state (not an application failure).

### Phase 7A additions

* `frontend/src/components/CapabilityStatusRow.tsx` accepts a new
  `kind` prop and renders an operator-facing guidance banner for
  Compute Optimizer (`INACTIVE`) and Cost Optimization Hub
  (`INACTIVE` / `NOT_ENROLLED`).
* Guidance text tells the operator how to enable each service
  outside the application — the app never calls `UpdateEnrollmentStatus`.

### Manual enrollment steps

The actual AWS service enrollment is performed manually outside the
application:

* **Compute Optimizer** — from an admin principal:
  ```bash
  aws compute-optimizer update-enrollment-status --status Active
  ```
  Or open the Compute Optimizer console and click **Opt in**.

* **Cost Optimization Hub** — open the Cost Optimization Hub
  console and enroll the management account. AWS then takes ~24h
  to surface recommendations.

Once enrolled, the dashboard's read-only code automatically detects
the new state on the next refresh (the existing
`build_capabilities` path re-resolves enrollment on every request).

### Coverage

* `scripts/phase7a_verify.sh` — grep checks confirming
  `UpdateEnrollmentStatus` is **not** referenced anywhere in
  `backend/` or `frontend/` code, and the operator-facing guidance
  copy exists in the UI.

## Issue 5 — Real AI provider via LiteLLM

### LiteLLM config

`litellm/config.yaml`:

```yaml
model_list:
  - model_name: cost-analyst
    litellm_params:
      model: os.environ/LITELLM_MODEL
      api_key: os.environ/LITELLM_API_KEY
```

The application talks to LiteLLM exclusively through the stable
internal alias `cost-analyst`. The actual provider model and
credentials are resolved by the LiteLLM gateway from environment
variables at startup. **No secrets appear in the file.**

### Activation

To enable a real provider:

```bash
export LITELLM_API_KEY=<your-provider-key>
export LITELLM_MODEL=openai/gpt-4o-mini   # or any LiteLLM-supported model
docker compose restart litellm backend
```

When `LITELLM_API_KEY` is unset:

* `/api/ai/status` returns `provider_configured=false` and
  `required_env_var="LITELLM_API_KEY"`.
* The `AiProviderBadge` shows `"AI waiting on LITELLM_API_KEY"`.
* `AI_ENABLED` defaults to `false`; `/api/ai/executive-summary`,
  `/api/ai/analyze`, and `/api/ai/recommendations/{id}/explain`
  return `AIResponse(status=DISABLED)` without contacting the
  gateway.

### /api/ai/status payload

```json
{
  "status": "DISABLED",
  "ai_enabled": true,
  "litellm_reachable": false,
  "provider_configured": false,
  "model_alias": "cost-analyst",
  "provider_model": null,
  "litellm_base_url": "http://litellm:4000",
  "timeout_seconds": 30,
  "max_output_tokens": 800,
  "context_limits": { "...": "..." },
  "required_env_var": "LITELLM_API_KEY",
  "message": null
}
```

Never includes: API keys, Authorization headers, query-string
credentials, raw provider secrets.

### Boundary enforcement

* `frontend/src/lib/api.ts` is the only module that knows about the
  JWT.
* No frontend code imports `openai`, `anthropic`, `google-generativeai`.
* No backend code imports those SDKs either.
* `requirements.txt` does not list any LLM provider package.

### Coverage

* `backend/tests/test_phase7a_ai_status_safety.py` — 4 cases pinning
  the no-secret guarantee at both the service layer and the Pydantic
  schema layer.
* `frontend/src/tests/phase7a-ai-status.test.tsx` — 4 cases pinning
  the badge contract.
* `scripts/phase7a_verify.sh` — grep checks for the model alias,
  env-driven credentials, no SDK imports, and no secret literals in
  the repository.

## Phase 7B / 7C extension points

These changes intentionally do **not** block the future phases:

* `account_id` is plumbed end-to-end through the optimization
  engine, cost cache, and identity endpoint.  Adding a multi-account
  registry is a thin wrapper around the existing call sites.
* `RegionFilter` already accepts `'all' | string`.  Multi-region
  aggregation is a query-string extension on the existing routes.
* The LiteLLM gateway is provider-agnostic.  Adding Azure or
  Rackspace costs means adding rows to `litellm/config.yaml` and a
  `cloud_provider` discriminator on the existing schemas.  No
  architectural change required.

## Security

* No HTTPS in Phase 7A (dev credentials remain in use).
* No JWT secret, admin password, LLM provider key, or AWS credential
  is committed to the repository.
* No new IAM actions; no `UpdateEnrollmentStatus` calls.
* WebSocket JWT transport unchanged: `Sec-WebSocket-Protocol:
  bearer.<jwt>`.

## Verifier

`scripts/phase7a_verify.sh` is the canonical Phase 7A check.  It
delegates `phase6c_verify.sh` and adds Phase 7A-specific structural,
contract, secret-hygiene, and Docker / nginx topology checks.  Output
is explicit `PASS` / `FAIL` totals.

In the current sandbox environment the verifier runs in structural
mode via `PHASE7A_FULL_DELEGATION=0 PHASE7A_RUN_NPM=0` (no network).
In a fully built environment the default mode runs the full Phase 6
suite plus the Phase 7A structural checks plus the npm-driven
typecheck / build / test.
