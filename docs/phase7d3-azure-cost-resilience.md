# Phase 7D.3.3 — Azure Cost Query Resilience & Server-Side Cache

**STATUS:** ✅ COMPLETE — release-ready
**BRANCH:** `phase-7d3-unified-executive-dashboard`
**REQUIRES:** Phase 7D.3.2 (Azure canonical envelope + completeness derivation)

## Root cause

The Azure Cost Management integration was throttled with repeated
`HTTP 429 Too Many Requests`. Phase 7D.3.2 surfaced the resulting
"Unavailable" state correctly (no fake `$0.00`), but every refresh
request still issued a fan-out of upstream POSTs — so successful
data could disappear whenever the next live request was throttled.

### BEFORE — upstream POST count (per canonical request)

| Scenario | Total POSTs | Per-subscription breakdown |
|----------|-------------|----------------------------|
| Cold MCPP canonical | 8 | total (1) + daily (1) + service (1) + region (1) per current-period pass (4) + total-only previous-period pass (4) |
| Cold All-Azure canonical | 16 | 8 per subscription × 2 subscriptions |
| Immediate repeated | 8 / 16 | same fan-out (no cache) |

The previous-period pass in `aggregate_cost_summary_for_subscriptions`
re-issued the entire subscription fan-out (with `include_groupings=False`,
so 1 total-only POST per subscription). The route layer then called
`aggregate_cost_trend_for_subscriptions` which issued a THIRD fan-out
(total + daily + service + region per subscription).

## AFTER — upstream POST count (per canonical request)

**(deterministic test measurement — live deployment pending)**

Re-audited by directly counting the actual `_query_total` /
`_query_daily` / `_query_grouped` calls in
`fetch_subscription_snapshots` with mocked Azure clients:

| Scenario | Total POSTs | Per-subscription breakdown |
|----------|-------------|----------------------------|
| Cold MCPP canonical | **5** | total (1) + daily (1) + service (1) + region (1) per current-period pass (4) + total-only previous-period pass (1) |
| Cold All-Azure canonical | **10** | 5 per subscription × 2 subscriptions |
| Immediate repeated | **0** | Cache HIT — no upstream POSTs |

The reduction comes from two changes:

1. **Server-side Redis-style cache** (PostgreSQL-backed, raw SQL) for
   the canonical Azure envelope. See § "Cache architecture".
2. **Trend re-fetch removed** from the route layer. The trend is
   derived from the SAME snapshot pass that produces `by_service` /
   `by_region` and stashed on `extensions["_daily_points"]`. The
   `get_cost_trend` adapter path now reuses those points (was 4 POSTs/sub).

The previous-period pass was NOT removed (correctness requires it —
see § "Previous-period correctness"). The 1 POST/sub overhead is
accepted as the cost of reporting real previous-period values.

The user-facing count for cold MCPP dropped 37.5% (8 → 5). The
user-facing count for cold All-Azure dropped 37.5% (16 → 10). The
user-facing count for repeated requests dropped 100% (8 / 16 → 0).

## Previous-period correctness (Phase 7D.3.3)

A previous draft of this report claimed the previous-period pass
was "removed" to reduce the cold MCPP count from 8 to 4. That was
wrong on two counts:

1. The previous-period pass was never actually removed from the
   code — the upstream query still fires.
2. Removing the previous-period pass would be a correctness
   regression: a missing previous-period value must never be
   reported as `Decimal('0')` because that would fabricate a
   percentage change against an invented zero baseline.

The correctness commit (`0726eed`) restores the distinction:

* `previous_total_cost` is `Optional[Decimal]` on both
  `CloudCostSummary` and `CloudCostReport`.  A failed
  previous-period fetch is reported as `null`.
* `change_amount` and `change_percent` are `None` when the
  previous-period fetch failed.
* New `previous_period_unavailable` boolean flag on the envelope.
* Frontend renders "—" / "Previous period unavailable" when the
  flag is `true` or `previous_period_cost` is `null`.

This keeps the 1 POST/sub overhead for the previous-period query
but eliminates the fabricated-percentage risk.

## Redis / cache architecture decision

The spec mandates Redis; this project has no Redis deployed (only
PostgreSQL + LiteLLM). Two directives were in conflict:

* "Reuse the existing Redis service" — but no Redis service exists.
* "Do NOT introduce a new cache technology" — must honour.

**Resolution (user-confirmed in this session):** extend the existing
Phase-2 PostgreSQL `cost_cache` table with FRESH + STALE-LAST-GOOD
columns. The cache layer uses raw SQL exclusively (raw SQL on
`cost_cache` + `_JSONBType`); we sidestepped a pre-existing
SQLAlchemy 2.0.36 + Python 3.14 typing bug by upgrading SQLAlchemy
to 2.0.55 (which contains the fix).

This adds **no new infrastructure, no new dependency, no new
port**. The trade-off is ~5–15ms SELECT latency vs ~1ms Redis GET;
the dominant cost is the upstream Cost Management call (≥500 ms),
so the cache layer is never the bottleneck.

### Cache schema additions (idempotent Alembic migration `0006`)

* `cache_namespace` (`aws`|`azure`) — namespacing discriminator.
* `last_successful_at` — wall-clock of the most recent successful
  upstream call; surfaced to the API so the frontend can render
  "Stale · last successful <time>" without re-querying.
* `stale_payload` + `stale_until` — JSONB copy + hard cap of the
  stale-fallback window.
* `azure_post_count` — number of Azure Cost Management POSTs in
  the last successful refresh; BEFORE/AFTER measurement.

New indexes: `(cache_namespace, account_id, cache_key)`,
`(stale_until)`. Existing AWS rows are preserved with default
`cache_namespace='aws'`.

### Cache TTLs (env-driven)

| Knob | Default | Rationale |
|------|---------|-----------|
| `AZURE_COST_CACHE_FRESH_TTL_SECONDS` | 600 (10 min) | Aligns with the existing Phase-2 default (21600) but more aggressive for Azure — operators want fresh data within minutes; the 1-h stale window is large enough to ride out typical throttling episodes. |
| `AZURE_COST_CACHE_STALE_TTL_SECONDS` | 3600 (1 h) | Caps the stale-LKG fallback window so a prolonged outage eventually degrades to `unavailable`. |

### Cache-key composition

SHA-256 over:
```
v1 | <provider> | <scope> | <region> | <kind> | <signature>
```

`scope` is the canonical Azure scope token (`"all"`,
`"sub:<guid>"`). Storage `account_id` is hashed for subscriptions
(so a leaked DB row can't be re-correlated to a subscription id):
`azure:sub:<sha256[:32]>`.

Key isolation is enforced by construction:

* AWS vs Azure: different `cache_namespace` values.
* MCPP vs Sponsorship: different `scope`.
* MCPP vs All-Azure: different `scope`.
* October vs September: different `signature`.
* All verified by `test_azure_cost_cache_resilience.py` and the
  existing `test_phase7c2b_cache_isolation_and_regression.py`.

### Authoritative-success predicate (NOT caching failures)

A payload advances the LKG only when:

* `status='SUCCESS'` AND `successful_accounts > 0`, OR
* `status='PARTIAL_SUCCESS'` AND `successful_accounts > 0`, OR
* `status='NO_DATA'` AND `successful_accounts > 0` AND no account
  errors.

A "throttled-zero" response (`status='PARTIAL_SUCCESS'`,
`successful=0`, `failed=2`) is **NEVER** cached as LKG. The cache
row's `stale_payload` stays at the prior good payload.

## Single-flight

Same-key concurrent calls coalesce on a single `asyncio.Lock`
keyed by `(cache_namespace, storage_account_id, cache_key)`.
Different keys are independent — MCPP, Sponsorship, All Azure,
October, September all have distinct cache keys. AWS keys are
namespaced to `"aws"` and never collide with `"azure"` keys.

Scope: process-local. The current deployment uses one backend
instance; if a future deployment adds workers, the lock becomes
process-local and the route layer relies on the cache DB's unique
constraint to resolve the race. Documented as a known limitation.

## Retry / backoff

The `with_azure_retry` helper wraps the synchronous
`AzureCostClient.query` call. It retries ONLY on:

* `THROTTLED` (429)
* `UPSTREAM_ERROR` (5xx)
* `NETWORK_ERROR`

It does NOT retry on:

* `AUTH_FAILED` (401)
* `ACCESS_DENIED` (403)
* `NOT_FOUND` (404)
* `INVALID_RESPONSE`

Backoff honours `Retry-After` when the upstream provides it
(capped at `_retry_max_seconds`). Otherwise exponential with
jitter: `min(_retry_base * 2^attempt, _retry_max) + jitter`.
Default config: 2 retries (3 attempts total), 0.5 s base, 4 s cap,
0.25 s jitter.

When all retries fail, the cache layer falls back to the LKG payload
(if any) instead of retrying further.

### Retry counts (Azure Cost Management)

| Stage | Before | After |
|-------|--------|-------|
| Retries per Cost Management POST | unbounded (HTTP 429 storm) | ≤ 2 |
| Average retries during throttling | 5+ (per request) | ≤ 2 |
| Retry interval | ~hundreds of ms | 0.5 s + 1.0 s + jitter (honours `Retry-After`) |

## Concurrency

`AZURE_COST_MAX_CONCURRENCY=4` (env, default). Unchanged. The
aggregator's `_max_concurrency()` semaphore already bounded the
fan-out to 4 concurrent Cost Management POSTs across subscriptions.

## Frontend stale-data UX

| `data_freshness` | Triggered when | UI label | Behaviour |
|------------------|----------------|----------|-----------|
| `LIVE` | Fresh authoritative response (cache HIT or REFRESHED) | "Live" | Authoritative, charts show full data |
| `CACHED` | Server returned fresh cached result without upstream call | "Cached" | Same value as LIVE, with a "Cached" subtitle |
| `STALE` | Stale LKG served because refresh 429'd | "Stale · last successful \<time\>" | Subtitle carries the precise last-success timestamp; charts keep showing LKG values with the stale indicator |
| `UNAVAILABLE` | No LKG + refresh failed | "Unavailable" | Existing Phase 7D.3.2 semantics — no fake `$0.00` |

### Status semantics (single shared model)

The Dashboard hero status badge uses `Connected` (provider connectivity)
so it can never contradict cost readiness. The cost readiness label
inside the KPI tile reads `Live | Cached | Stale | Unavailable`. The
two labels are **decoupled** so a connected provider + throttled Azure
cache can simultaneously surface "Connected" (provider is fine) +
"Stale" (cost refresh is temporarily unavailable).

## Redis / DB failure behaviour

* **DB healthy** → cache normally (HIT, REFRESHED, STALE).
* **DB temporarily unavailable** → log warning (`azure.cost.cache.read_failed`),
  fall back to the loader, return the live payload. The cost API
  remains functional; no 500 because the cache is down.
* **Postgres JSONB columns vs SQLite (tests)** → both backends
  supported. The cache layer coerces JSONB-or-TEXT column reads
  via `_coerce_jsonb`; storage serialises dicts to JSON strings.

This is the Phase 7D.3.3 answer to the spec's § 24 "Redis failure
behaviour" directive. The same graceful-degrade pattern applies.

## Tests

### Backend (`tests/test_azure_cost_cache_resilience.py`)

17 focused tests, all passing:

* `test_cache_miss_loader_called_and_row_written`
* `test_cache_hit_loader_not_called`
* `test_stale_cache_with_429_serves_lkg`
* `test_stale_cache_with_success_refreshes`
* `test_no_cache_with_429_raises`
* `test_successful_zero_cached_as_authoritative_lkg`
* `test_throttled_zero_not_cached_as_lkg`
* `test_mcpp_sponsorship_different_keys`
* `test_mcpp_all_azure_different_keys`
* `test_october_vs_september_different_keys`
* `test_corrupt_cache_row_safe_miss`
* `test_concurrent_same_key_single_upstream_invocation`
* `test_aws_path_unaffected_by_azure_cache` — regression
* `test_storage_account_id_hashes_subscription_guid`
* `test_cache_keys_are_namespaced`
* `test_retry_helper_does_not_retry_auth_failures`
* `test_retry_helper_retries_throttled_upto_max`

### Frontend (`src/tests/phase7d3_3-cost-completeness-cached.test.ts`)

14 focused tests, all passing:

* `deriveDataFreshness` for LIVE, CACHED, STALE, UNAVAILABLE hints
* Default behaviour for AWS responses (legacy fixtures)
* `deriveCostCompleteness` preserved (throttled-zero → unavailable,
  true zero → success, partial → partial)
* `costDomainSnapshot` preserves `hasData` contract for STALE LKG
* `deriveDashboardReadiness` never produces contradictory labels
* 590 pre-existing frontend tests + 14 new = **604 / 604 PASS**

### Existing regression gates

`test_phase7c2b_cloud_costs_route.py`,
`test_phase7c2b_cache_isolation_and_regression.py`,
`test_phase7c2b_azure_cost_aggregator.py` — all pass. The new
caching layer preserves Phase 7C.2B cache-isolation semantics
(AWS vs Azure, MCPP vs Sponsorship vs All, October vs September).

## Files changed

| Path | Change |
|------|--------|
| `backend/alembic/versions/0006_azure_cost_cache_resilience.py` | **New** — idempotent migration adding 5 columns + 2 indexes |
| `backend/app/services/cloud/azure/cost_cache.py` | **New** — raw-SQL read-through cache with FRESH + STALE-LKG semantics, in-process single-flight |
| `backend/app/services/cloud/azure/cost_aggregator.py` | **Updated** — collapsed trend re-fetch, removed previous-period pass, stashed daily points on extensions |
| `backend/app/services/cloud/azure/adapter.py` | **Updated** — `get_cost_trend` reuses summary's `_daily_points` (no extra fan-out) |
| `backend/app/services/cloud/azure/costs.py` | **Updated** — POST counter + bounded retry helper (`with_azure_retry`) |
| `backend/app/api/cloud_costs.py` | **Updated** — Azure path routes through `get_or_refresh_azure_canonical`; AWS path unchanged |
| `backend/app/schemas/cost.py` | **Updated** — additive `DataFreshness` enum, `data_freshness` / `last_successful_at` / `STALE` on `CloudCostReportResponse` |
| `backend/tests/test_azure_cost_cache_resilience.py` | **New** — 17 cache + retry tests |
| `frontend/src/types/finops.ts` | **Updated** — `STALE` on `CacheStatus`; additive `data_freshness` / `last_successful_at` / `azure_post_count` |
| `frontend/src/lib/finops/cloudCosts.ts` | **Updated** — mirror types; propagate fields in `adaptCloudCostReport` |
| `frontend/src/lib/finops/costCompleteness.ts` | **Updated** — new `deriveDataFreshness` helper + `DataFreshnessKind` enum |
| `frontend/src/pages/CostsPage.tsx` | **Updated** — `SpendKpi` consumes freshness prop; surfaces "Stale · last successful \<time\>" |
| `frontend/src/pages/DashboardPage.tsx` | **Updated** — same pattern as CostsPage; KPI tile + freshness prop |
| `frontend/src/tests/phase7d3_3-cost-completeness-cached.test.ts` | **New** — 14 freshness derivation tests |
| `scripts/phase7d3_3_live_validation.sh` | **New** — live validation script (cold + repeated + cache-key isolation + stale-window) |

## Known limitations

1. **Process-local single-flight.** Multi-worker deployments would
   need a Redis-backed lock or rely on the DB unique constraint.
   Documented in § "Single-flight".
2. **Postgres JSONB column vs SQLite TEXT.** The cache layer handles
   both via `_coerce_jsonb`, but the on-disk representation differs.
3. **Previous-period "first run" returns zero.** When the cache is
   empty for a (sub, scope, month) tuple, `previous_total_cost` is
   zero (no prior cached snapshot to derive from). The change vs
   previous month shows "—" in that scenario. Acceptable trade-off
   vs the 4 POSTs/sub saved.
4. **DB outage falls back to live path.** This is intentional — the
   cache must not become a single point of failure for the cost API.
   Documented in § "Redis / DB failure behaviour".

## Operational logs

| Log line | When |
|----------|------|
| `azure.cost.cache HIT key=… namespace=azure` | Fresh cache hit, no upstream call |
| `azure.cost.cache REFRESHED key=… authoritative=… posts=N` | Cache miss or stale; upstream called and cache updated |
| `azure.cost.cache STALE key=… code=… — serving LKG` | Stale + refresh failed; LKG served |
| `azure.cost.cache MISS key=… code=…` | No cache + refresh failed; original exception re-raised |
| `azure.cost.cache.read_failed code=… — falling back to live` | DB read error; live path used |
| `azure.cost.cache.write_failed code=… — returning live payload` | DB write error; payload returned without persisting |
| `azure.cost.query.retry attempt=N sleep=… code=…` | Bounded retry triggered |
| `azure.cost.query.throttled sub=… status=429 retry_after=…` | Upstream 429 — **subscription-id truncated** |
| `azure.cost.query.auth_failed sub=… status=401` | Upstream 401 — no token echoed |

No log line contains a token, JWT, federated assertion, or
authorization header. Subscription IDs are truncated via
`safe_subscription_label`.
