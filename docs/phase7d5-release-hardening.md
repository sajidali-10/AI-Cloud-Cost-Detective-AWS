# Phase 7D.5 — Multi-Cloud Integration, Observability & Release Hardening

**STATUS:** IMPLEMENTATION IN PROGRESS — release validation pending
**BRANCH:** `phase-7d5-release-hardening`
**WORKING TREE:** changes staged

---

## Final Phase 7D architecture

```
                          ┌─────────────────────┐
                          │   Browser (UI)       │
                          │   /dashboard         │
                          │   /costs             │
                          │   /resources         │
                          │   /optimization      │
                          │   /ai-analyst (7D.4) │
                          │   /cloud-ai (7D.4)   │
                          └──────────┬──────────┘
                                     │  HTTP
                          ┌──────────▼──────────┐
                          │   Nginx (port 80)   │
                          │   /api/* → backend  │
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────────────────┐
                          │   Backend (FastAPI)               │
                          │   /api/cloud/{costs,resources,   │
                          │     recommendations,accounts}      │
                          │   /api/cloud/ai/{status,analyze}  │
                          │   /health   (process liveness)    │
                          │   /health/ready (dependency ready)│
                          └──────────┬──────────────────────┘
                                     │
                ┌────────────────────┼────────────────────┐
                │                    │                    │
        ┌───────▼────────┐   ┌────────▼────────┐   ┌───────▼────────┐
        │  PostgreSQL    │   │   LiteLLM        │   │  Provider     │
        │  cost_cache    │   │   gateway        │   │  adapters     │
        │  (0007 LKG)    │   │   (AI_ENABLED?   │   │  AWS / Azure  │
        └────────────────┘   │    optional)     │   └───────────────┘
                            └──────────────────┘
```

## Deployment procedure

### First-time deploy
```bash
docker compose build backend frontend
docker compose up -d --force-recreate backend frontend nginx
docker compose run --rm backend alembic upgrade head
```

### Subsequent deploys (no schema change)
```bash
docker compose build backend frontend
docker compose up -d --force-recreate backend frontend nginx
```

### Schema-change deploy
```bash
docker compose build backend frontend
docker compose run --rm backend alembic upgrade head
docker compose up -d --force-recreate backend frontend nginx
```

## Migration state

```
Revision: 0007_azure_lkg_cleanup (head)
0001_cost_cache                        (Phase 2 baseline)
0002_app_users                        (Phase 5A)
0003_conversations                    (Phase 6C)
0004_cloud_accounts                   (Phase 7C.1)
0005_cloud_provider_abstraction       (Phase 7C.1)
0006_azure_cost_cache_resilience      (Phase 7D.3.3 — FRESH/STALE-LKG)
0007_azure_lkg_cleanup                (Phase 7D.3.3 closure — invalidates
                                       legacy throttled-zero rows)
```

`alembic upgrade head` is idempotent and can be re-run safely.

## Test procedure

### Backend canonical command
```bash
./scripts/test_backend_release.sh
```

This wrapper auto-detects whether the backend container is running
and either runs pytest inside the container (with `POSTGRES_HOST=postgres`
injected so the docker-compose network resolves) or falls back to
local pytest using the host environment. `POSTGRES_HOST` is NOT
hardcoded in `tests/conftest.py` — the conftest respects the
environment variable first and only falls back to `localhost` (or
`postgres` when resolvable) when unset.

### Frontend canonical command
```bash
cd frontend
npx vitest run
npx tsc -p tsconfig.json --noEmit
npm run build
```

### Full backend release suite
```bash
./scripts/test_backend_release.sh
# Expected: 100% PASS
```

## Release-validation procedure

```bash
./scripts/phase7c2b_live_validation.sh
./scripts/phase7d1_live_validation.sh
./scripts/phase7d2_live_validation.sh
./scripts/phase7d3_live_validation.sh
./scripts/phase7d3_2_live_validation.sh
./scripts/phase7d3_3_live_validation.sh
./scripts/phase7d4_live_validation.sh
./scripts/phase7d5_release_validation.sh
```

`scripts/phase7d5_release_validation.sh` orchestrates the new
Phase 7D.5 checks: cross-page consistency, freshness labels,
cache invariants, AI-disabled mode. It discovers Azure
subscriptions via `/api/cloud/accounts/selectable/AZURE` and
does NOT hardcode subscription GUIDs.

Final contract:
* `exit 0` — every mandatory gate passed (LLM portion may be SKIPPED).
* `exit 1` — one or more gates failed.
* `exit 2` — pre-condition violated (backend not reachable).

## Freshness semantics

Five canonical values are recognised by every UI surface and
server endpoint:

| Value | Meaning | Trigger |
|-------|---------|---------|
| `LIVE` | Authoritative response just produced | cache miss + upstream succeeded |
| `CACHED` | Authoritative cache hit | cache hit, not expired |
| `STALE` | Cache hit but expired; LKG served because refresh failed | cache hit, expired, refresh 429 / 5xx, `stale_payload` + `stale_until > now` |
| `PARTIAL` | Some sub-sources succeeded, some failed | e.g. cost LIVE + resources UNAVAILABLE |
| `UNAVAILABLE` | No usable data and no LKG | cost PARTIAL_SUCCESS + 0 successful + no stale_payload |

**Invariants (cross-page consistency tests in
`tests/test_phase7d5_cross_page_consistency.py`):**

1. Unknown previous period → `null` (NOT `Decimal('0')`).
2. `PARTIAL_SUCCESS + successful_accounts=0` → classified UNAVAILABLE,
   never rendered as authoritative $0.
3. True SUCCESS + `total_cost=0` → authoritative zero
   (preserved as `Decimal('0.00')`).
4. STALE → never labeled LIVE.
5. Cache HIT → `azure_post_count=0` in the response (no upstream
   call); the historical count lives only in the
   `cost_cache.azure_post_count` column.

## Cache semantics

* Backend: PostgreSQL `cost_cache` table.
* Schema additions (Phase 7D.3.3):
  * `cache_namespace` (`aws`|`azure`) — isolates providers.
  * `last_successful_at`, `stale_payload`, `stale_until`,
    `azure_post_count`.
* Read path: cache row is authoritative only if
  `expires_at > now` AND payload passes
  `_is_authoritative_success()`.
* Non-authoritative payloads (throttled-zero, all-failed) are NEVER
  written — a non-authoritative response is returned to the caller
  but the cache row is left unchanged.
* AWS path uses the legacy Phase-2 read-through cache
  (`app.services.cost_cache.get_or_refresh`); Azure path uses the
  new Phase 7D.3.3 FRESH + STALE-LKG layer
  (`app.services.cloud.azure.cost_cache.get_or_refresh_azure_canonical`).
* 0 Azure POSTs on cache HIT (request-local counter ≠ historical
  counter; verified by `test_hit_post_count_is_zero`).

## AI-disabled behavior

When `AI_ENABLED=false`:
* `GET /api/cloud/ai/status` → `{enabled: false, status: "DISABLED"}`.
* `POST /api/cloud/ai/analyze` → `{status: "DISABLED", provider,
  scope, period, data_freshness, evidence: [...cards], warnings}`. The
  AI text is a deterministic fallback string built from the canonical
  envelope; the **evidence cards are always deterministic** (server-
  side extraction from canonical data, never LLM-generated).
* The `cost_cache.azure_post_count` column is never written or read
  inside the deterministic path.
* The validator reports: `LIVE LLM GENERATION: SKIPPED — provider
  not configured`.

When LiteLLM is unreachable (when enabled): the route returns
`status=UNAVAILABLE` with a controlled fallback answer + evidence;
the backend remains healthy.

## Observability

New module: `backend/app/services/observability.py`. Single source
of truth for the canonical operational log shape.

Every canonical route emits ONE structured log line per request
on the `cost-detective-backend.ops` logger:

```
cloud.ops duration_ms=187 status=SUCCESS provider=AWS scope_kind=all \
        freshness=LIVE cache_status=REFRESHED upstream_call_count=4 \
        operation=cloud.costs request_id=f047d9025e7f
```

Fields:
* `request_id` — short hex correlation id (12 chars from uuid4).
* `operation` — e.g. `cloud.costs`, `cloud.resources`.
* `provider` — `AWS` | `AZURE`.
* `scope_kind` — `all` | `single` (NEVER the raw GUID).
* `status` — `SUCCESS` | `FAILURE`.
* `duration_ms` — wall-clock millis.
* `freshness` — `LIVE` | `CACHED` | `STALE` | `PARTIAL` |
  `UNAVAILABLE` | `unknown`.
* `cache_status` — `HIT` | `REFRESHED` | `STALE` | `MISS` |
  `DISABLED` | `unknown`.
* `upstream_call_count` — number of upstream POSTs this request made.
* `retry_count` — number of retries performed.
* `error_code` — only when `status=FAILURE`.

NEVER logs: bearer tokens, Azure JWT, AWS credentials, auth
headers, DB passwords, LiteLLM API keys, full secret env vars.

Failure-path detection via the existing `logger.warning` calls
in the canonical routes (Azure throttling, upstream errors) is
preserved; the structured log adds the success-side observability.

## Health / readiness

Two distinct endpoints:

| Endpoint | Purpose | Status codes |
|----------|---------|--------------|
| `GET /health` | Process liveness | always 200 if the process is alive |
| `GET /health/ready` | Dependency readiness | 200 if PostgreSQL is reachable; 503 only if PostgreSQL is NOT reachable; **LiteLLM down does NOT make the backend unhealthy** |

Phase 7D.5 changed the readiness contract:
* PostgreSQL down → `503 not_ready` (the application cannot
  operate correctly without its primary DB).
* LiteLLM down → `200 ready` with a `dependency_conditions`
  field listing `["litellm_unavailable"]`. The AI endpoints
  surface `UNAVAILABLE` / `DISABLED` instead of failing the
  request.
* AWS / Azure transient throttling → does NOT affect readiness
  (those are upstream data sources, not infra dependencies).

## Three distinct "unavailable" states

The application reports three categorically different conditions
to the user; they are NOT equivalent:

| State | Meaning | UI renders |
|-------|---------|------------|
| `application unavailable` | Backend cannot serve requests | `/health` returns 5xx; UI shows "Application unavailable" |
| `cloud data unavailable` | Backend healthy; specific AWS/Azure data source returned 429/5xx | `/health/ready` is 200; UI shows "Azure cost data unavailable" with reason |
| `AI provider unavailable` | Backend + cloud data healthy; LiteLLM gateway unreachable | `/health/ready` is 200 with `dependency_conditions`; AI endpoint returns `status=UNAVAILABLE` with deterministic evidence |

These three are surfaced distinctly in the UI and the operational
logs so on-call can distinguish infrastructure failure from
upstream throttling from AI-gateway downtime.

## Security review

* No new cloud write permissions added.
* AWS + Azure remain read-only (no client_secret, no static keys).
* No LLM access to cloud credentials — the AI service consumes
  only canonical envelopes; the model never sees AWS keys, Azure
  tokens, or DB passwords.
* No HTTPS / WAF / enterprise SSO / domain work added (that's
  Phase 8).
* Bearer tokens + auth headers are never written to logs.
* Subscription IDs are truncated via the existing
  `safe_subscription_label` helper in the logs and validator output.
* No new dependencies introduced.

## Known limitations

1. **Single-process single-flight.** The Azure cache layer's
   `asyncio.Lock` is process-local. Multi-worker deployments would
   need a Redis-backed lock or rely on the DB unique constraint.
2. **AI cache-context granularity.** The FinOps context builder
   is currently invoked per-request; for very high QPS we could
   cache the context envelope itself (topology unchanged, just
   the lookup path).
3. **No streaming responses.** `POST /api/cloud/ai/analyze`
   returns a complete JSON envelope. Streaming can be added in a
   future phase without changing the request contract.
4. **LiteLLM retry policy.** Bounded retries (≤ 2) honour
   `Retry-After` when the upstream provides one. A spike of
   sustained throttling may still result in `UNAVAILABLE` rather
   than `STALE` if no prior authoritative row exists.

## Troubleshooting

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `/health` 5xx | Backend process crashed | `docker compose logs backend` |
| `/health/ready` 503 | PostgreSQL down | `docker compose ps postgres` + `docker compose logs postgres` |
| `/health/ready` 200 + `dependency_conditions: ["litellm_unavailable"]` | LiteLLM gateway down | `docker compose ps litellm` + `docker compose logs litellm`. AI endpoints still work (deterministic path). |
| AI endpoint returns `status=UNAVAILABLE` with no evidence | Azure throttled and no LKG row exists | Wait for Azure to recover; the cache will warm on the next successful call. |
| AI endpoint returns `status=STALE` with last_successful_at | Azure throttled but LKG row exists | UI shows stale data + warning. No action required; refresh resumes when Azure recovers. |
| Cost endpoint returns `data_freshness=PARTIAL` | Some subscriptions throttled | UI shows the partial state explicitly; no fabrication. |
| Validator reports `LIVE LLM GENERATION: SKIPPED` | `AI_ENABLED=false` (expected on this deployment) | No action. AI endpoint still serves deterministic evidence. |
| Full backend pytest fails with `psycopg.OperationalError` | Backend container not running | `docker compose up -d --force-recreate backend` then re-run `./scripts/test_backend_release.sh` |

## Files changed

| Path | Change |
|------|--------|
| `backend/tests/conftest.py` | env-first `POSTGRES_HOST` / `LITELLM_HOST`; falls back to localhost or postgres only via DNS probe |
| `backend/app/services/observability.py` | NEW — structured operational logging (`operation_timer`, `mark_failure`, `new_request_id`) |
| `backend/app/api/cloud_costs.py` | emits structured log on AWS + Azure success paths |
| `backend/app/main.py` | `/health/ready` contract: only PostgreSQL down = 503; LiteLLM down = 200 with dependency_conditions |
| `backend/tests/test_phase7d5_cross_page_consistency.py` | NEW — 6 cross-page consistency tests |
| `scripts/test_backend_release.sh` | NEW — canonical release wrapper (auto-detect container vs local) |
| `scripts/phase7d5_release_validation.sh` | NEW — consolidated Phase 7D.5 release validator (discovers subscriptions via accounts endpoint, env-overridable) |
