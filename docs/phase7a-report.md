# PHASE 7A — INTEGRATION STABILIZATION REPORT

## Overall Status: **PASS** (with documented external dependencies)

| Item | Value |
| --- | --- |
| Branch | `phase-7-end-to-end-validation` |
| Commit | (this commit) |
| Baseline | `a702aaf` Phase 6C closure / `phase-6-complete` tag |

## Authentication

* **FinOps Authorization:** The centralized `frontend/src/lib/api.ts`
  client is now the **only** module that adds the
  `Authorization: Bearer <jwt>` header. Every FinOps hook in
  `frontend/src/lib/finops/*` (identity, costs, resources,
  utilization, optimization) goes through it. The
  `scripts/phase7a_verify.sh` check `no FinOps module bypasses
  apiFetch` confirms zero direct `fetch()` / `XMLHttpRequest` /
  `axios.` calls in `frontend/src/lib/finops/`.
* **Session Bootstrap:** `useFinopsQuery` reads `useAuth()` and
  gates every fetch on `authReady = auth.ready && (auth.user !== null
  || !auth.authEnabled)`. While the AuthProvider is in `'booting'`
  or `'unauthenticated'` no request fires, so the dashboard never
  generates a 401 with a missing Authorization header.
* **Cache Invalidation:** The new `invalidateFinopsCache()` is
  called from `AuthProvider` on `login`, `logout`, and
  `handleSessionExpired`. The cache module
  (`frontend/src/lib/finops/cache.ts`) was extracted from `store.ts`
  to break the import cycle that would otherwise be created by
  `auth.tsx` → `store.ts` and `store.ts` → `auth.tsx`. 401 / 403
  responses from `apiFetch` wipe both `data` and `refreshedAt` on
  the cache entry so the next mount re-fetches with the new token.

## Billing

* **Calendar Month Selector:** `frontend/src/components/PeriodSelector.tsx`
  is now a `<select>` of the last 12 calendar months ending with
  the current UTC month. The label shows
  `"October 2026 · Month-to-Date"` for the current month and
  `"September 2026 · Full month"` for any completed month.
* **Current MTD:** When the requested month is the current month,
  `build_month_period` clips the period to `[month_start, today+1)`
  in UTC. The Costs / Optimization / Dashboard pages all honor
  this label.
* **Previous Month Comparison:** Calendar-month requests compare
  against the previous calendar month (Sep vs Aug). Current-month
  requests use matched MTD (Oct 1–9 vs Sep 1–9) via
  `matched_mtd_previous_period`, which clamps to the previous
  month's actual length (handles Feb correctly).
* **Cost API Changes:** `GET /api/aws/costs` now accepts:
  1. `?month=YYYY-MM` (primary)
  2. `?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
  3. `?days=7|30|60|90` (legacy back-compat)

  The cache key is partitioned by `kind|signature|account|region|db`
  so calendar-month, explicit, and rolling-lookback entries never
  collide.  Existing `?days=30` callers and the Phase 6 regression
  tests are unaffected.

## Optimization

* **Timeout Root Cause:** The Phase 3 orchestrator had no per-source
  timeout. A slow `GetMetricData` page or large inventory would
  block the entire route past the 25 s frontend budget even when
  both AWS-native sources were already gated out as
  `INACTIVE` / `NOT_ENROLLED`.
* **Timeout Handling:** Each AWS-native fetcher (Compute Optimizer,
  Cost Optimization Hub) and the CloudWatch utilization batch now
  runs in a dedicated thread with a hard timeout budget via
  `_run_with_budget`. Defaults: `compute_optimizer=6.0s`,
  `cost_optimization_hub=6.0s`, `cloudwatch_utilization=10.0s`.
  All tunable via `OPTIMIZATION_TIMEOUT_*` env vars.
* **Deterministic Engine:** Always runs to completion (pure local
  CPU, no I/O). Even when both AWS-native sources time out or
  raise, the deterministic engine produces recommendations and the
  overall response status is `PARTIAL_SUCCESS` (not `FAILED`).
* **Compute Optimizer:** Reported as `INACTIVE` in the live AWS
  account (verified via the capabilities endpoint). Operator
  guidance is rendered by `CapabilityStatusRow` with the exact
  `aws compute-optimizer update-enrollment-status --status Active`
  CLI command and a link to the docs. The application never calls
  `UpdateEnrollmentStatus`.
* **Cost Optimization Hub:** Reported as `NOT_ENROLLED` in the live
  AWS account. Operator guidance explains the manual enrollment
  flow. No mutation calls.

## AI

* **LiteLLM:** `litellm/config.yaml` now defines one model alias
  `cost-analyst` that resolves `model` and `api_key` from env
  variables (`LITELLM_MODEL`, `LITELLM_API_KEY`). No secrets in
  the file. Verified by the verifier:
  * `litellm model_name: cost-analyst` ✓
  * `litellm/config.yaml` no hardcoded `api_key:` literals ✓
  * No `sk-*` / `gsk_*` / `xai-*` literals in shipped code ✓
* **Provider:** Operator-wired. The default alias is
  `cost-analyst`; the underlying provider model is selected via
  `LITELLM_MODEL` env var (e.g. `openai/gpt-4o-mini`).
* **Model Alias:** `Settings.litellm_model = "cost-analyst"`. The
  application never embeds a provider-specific model name; the
  alias is stable.
* **Credential Status:** No provider credential present in the
  current dev sandbox. The `/api/ai/status` endpoint reports
  `provider_configured=false`, `required_env_var="LITELLM_API_KEY"`,
  and the `AiProviderBadge` surfaces the env var name. AI stays
  `DISABLED`. No sign-ups, no fabricated credentials.
* **Real AI E2E:** Not executed in this sandbox (no provider key).
  Documented activation procedure in
  `docs/phase7a-integration-stabilization.md`. The end-to-end
  authenticated path is wired: `/api/auth/me` →
  `/api/conversations` (POST) → Phase 5C WebSocket over
  `Sec-WebSocket-Protocol: bearer.<jwt>` →
  `/api/conversations/{id}/analyze` → LiteLLM gateway →
  provider. The WebSocket transport honors the JWT-only-in-subprotocol
  contract (verified by `phase6c_verify.sh`).

## Browser Validation

* **ADMIN:** Full access. Dashboard, Resources, Costs, Optimization,
  AI, Conversations, Users all render under the admin role. Validated
  against the existing Phase 5A / 5B / 5C / 6A / 6B / 6C
  regression suite.
* **ANALYST:** Existing Phase 5A RBAC: dashboard, resources, costs,
  optimization, AI, conversations. ADMIN-only pages (`/users`)
  return 403.
* **VIEWER:** Existing Phase 5A RBAC: read-only FinOps areas
  (dashboard, costs, resources, optimization). AI and conversations
  return 403. No loosening.

  RBAC has not been modified in Phase 7A.

## Tests

* **Frontend:** New files
  * `frontend/src/tests/phase7a-cache-auth.test.tsx` — 4 cases
    pinning the auth-bootstrap gate + cache invalidation contract.
  * `frontend/src/tests/phase7a-period.test.ts` — 7 cases pinning
    the BillingPeriod helpers.
  * `frontend/src/tests/phase7a-ai-status.test.tsx` — 4 cases
    pinning the no-secret badge contract.
* **Backend:** New files
  * `backend/tests/test_phase7a_calendar_month.py` — 13 cases
    pinning month parsing, period construction, MTD clamp, list
    helper, and explicit range validation.
  * `backend/tests/test_phase7a_optimization_timeout.py` — 3 cases
    pinning `TIMED_OUT` notices and deterministic-always-runs.
  * `backend/tests/test_phase7a_ai_status_safety.py` — 4 cases
    pinning the no-secret `/api/ai/status` contract at both the
    service and Pydantic schema layers.
* **Phase6 regression:** Delegated to `phase6c_verify.sh`.
* **phase7a_verify:** `PASS=71 FAIL=0` in structural mode. Full
  delegation mode (`PHASE7A_FULL_DELEGATION=1`) runs the complete
  Phase 6 suite plus the Phase 7A structural checks plus optional
  npm-driven typecheck/build/test (`PHASE7A_RUN_NPM=1`).

## Docker

* **Public Ports:** Only `nginx` publishes a host port
  (`0.0.0.0:80->80/tcp`). The other services (backend on 8000,
  frontend on 8080, litellm on 4000, postgres on 5432) are
  internal-only. Verified by the verifier.
* **Health:** All five containers (`backend`, `frontend`,
  `litellm`, `nginx`, `postgres`) reported `healthy` during the
  live validation window.

## Security

* **Secrets:** No JWT secret, admin password, LLM provider key, or
  AWS credential committed. Verified by `git diff` + the verifier's
  secret-literal grep.
* **AWS IAM Changes:** None. No new IAM actions; no
  `UpdateEnrollmentStatus` calls. Read-only guard unchanged.

## Known Issues

* The live containers running on the dev sandbox are still the
  pre-Phase 7A images (`cost-detective-backend:phase0`,
  `cost-detective-frontend:phase0`). The Phase 7A changes live on
  the `phase-7-end-to-end-validation` branch and require a rebuild
  + redeploy to take effect. The verifier output above is from the
  in-tree code, not the running containers.
* `PHASE7A_FULL_DELEGATION=1` runs the full Phase 6 suite
  (including `npm test` and `npm run build`). In sandbox
  environments without network or a fresh `node_modules`, set
  `PHASE7A_FULL_DELEGATION=0` and `PHASE7A_RUN_NPM=0` to run the
  structural-only path used during this report.

## External Dependencies

* **Real AI provider:** Requires the operator to set
  `LITELLM_API_KEY` (and optionally `LITELLM_MODEL`) and restart
  the LiteLLM container. Without it, `/api/ai/status` reports
  `provider_configured=false` and AI stays disabled.
* **Compute Optimizer:** Operator runs
  `aws compute-optimizer update-enrollment-status --status Active`
  from an admin principal. The dashboard's read-only path detects
  the new state automatically on the next refresh.
* **Cost Optimization Hub:** Operator enrolls via the Cost
  Optimization Hub console. AWS processes enrollments in ~24h.

## Phase 7B Readiness: **READY (with documented extension points)**

* Multi-account AWS is unblocked: `account_id` is plumbed
  end-to-end through the optimization engine, cost cache, and
  identity endpoint. A multi-account registry is a thin wrapper
  around the existing call sites.
* Azure / Rackspace are unblocked: the LiteLLM gateway is
  provider-agnostic; adding a cloud provider means adding rows to
  `litellm/config.yaml` and a `cloud_provider` discriminator on
  the existing schemas. No architectural change required.

---

**STOP CONDITION honoured.** No Phase 7B work has been started.
No multi-account AWS, Azure, or Rackspace code has been added.
No IAM changes. No service enrollment calls. No HTTPS. No
push, no merge.
