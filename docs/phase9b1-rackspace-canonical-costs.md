# Phase 9B.1 — Rackspace Canonical Cost Integration

Status: complete (branch `phase-9b1-rackspace-costs`)

Phase 9B.1 promotes the validated Phase 9A Rackspace Billing
proof-of-concept into the canonical multi-cloud cost architecture.
`RACKSPACE` is now a first-class provider on the existing
provider-neutral cost surface — there is no separate Rackspace API.

## Architecture

```
/api/cloud/costs?provider=RACKSPACE          (existing canonical route)
        │
        ▼
app.api.cloud_costs.get_cloud_costs          (provider-generic dispatch)
        │  RACKSPACE branch
        ▼
app.services.cloud.rackspace.adapter.RackspaceCloudProvider
        │  get_cost_summary()  (CloudProvider ABC)
        ▼
app.services.cloud.rackspace.costs           (normalization + typed errors)
        │  fetch_cost_snapshot()
        ▼
app.services.rackspace.billing.RackspaceBillingClient
        │  fetch_estimated_charges()          (GET only)
        ▼
https://billing.api.rackspacecloud.com/v2/accounts/{ran}/estimated_charges
        │  auth via
        ▼
app.services.rackspace.identity.RackspaceIdentity (Phase 9A, reused)
        │  POST /v2.0/tokens (the ONLY write-like verb)
        ▼
cost_cache (PostgreSQL, namespace "rackspace")  FRESH + STALE-LKG
```

Key modules:

| Module | Role |
|---|---|
| `backend/app/services/cloud/rackspace/adapter.py` | `RackspaceCloudProvider` — the `CloudProvider` implementation, capability reporting, account validation, startup account bootstrap, self-registration |
| `backend/app/services/cloud/rackspace/costs.py` | pure normalizer (Billing V2 → `RackspaceCostSnapshot`), typed error model, period-overlap rule |
| `backend/app/services/cloud/rackspace/cost_cache.py` | Rackspace read-through cache wrapper (namespace, hashed storage ids, TTL knobs) |
| `backend/app/services/cloud/cost_envelope.py` | `build_rackspace_envelope` — shared canonical envelope builder |
| `backend/app/api/cloud_costs.py` | RACKSPACE dispatch branch + HTTP error mapping |
| `backend/app/services/rackspace/*` (Phase 9A) | shared HTTP client, Identity, Billing client — **the single authentication implementation** |

## Identity vs Billing identifiers

Rackspace has two distinct identifiers that are **never conflated**:

* **Cloud tenant / account id** (`RACKSPACE_ACCOUNT_ID`) — the
  Identity-scoped tenant. Non-secret; stored in the `cloud_accounts`
  registry row (`account_id` / `tenant_id` columns) and surfaced in
  API responses and the account selector.
* **Billing Account Number / RAN** (`RACKSPACE_BILLING_ACCOUNT`) —
  the path scope for Billing V2 calls. Protected configuration:
  never persisted in PostgreSQL, never logged, never echoed in API
  responses or error messages, never part of any cache key.

Secrets (`RACKSPACE_USERNAME`, `RACKSPACE_API_KEY`,
`RACKSPACE_BILLING_ACCOUNT`) remain in the protected host `.env`,
passed to the backend container as process environment only. No
Rackspace credential is ever stored in the database.

## Billing source

Canonical cost source: `RACKSPACE_BILLING`.

```
GET https://billing.api.rackspacecloud.com/v2/accounts/{ran}/estimated_charges
```

Supporting read-only endpoints (Phase 9A probes, not used by the
canonical cost path): `/v2/accounts/{ran}`,
`/v2/accounts/{ran}/currency`, `/v2/accounts/{ran}/billing-summary`,
`/v2/accounts/{ran}/invoices/latest`.

The Billing API is intentionally not in the Identity service catalog;
the documented base URL is used directly (Phase 9A finding).

## Canonical mapping

| Billing V2 field | Canonical field |
|---|---|
| `currency` / `currencyCode` (or the single distinct record currency) | `currency` — never assumed to be USD |
| `currentBillingPeriodStartDate` / `currentBillingPeriodEndDate` (or `currentPeriod{start,end}`) | `period.start` / `period.end` — the **actual source period**, exposed verbatim |
| record amounts (`amount` et al.) summed with `Decimal` | `total_cost` (canonical total = exact record sum) |
| `chargeTotal` / `estimatedTotal` / `total` / `currentCharges` / `amount` | cross-check only: drift > $0.01 becomes a warning, never silently absorbed |
| record product fields (`service`, `offeringCode`, `product`, `category`, `productName`, `offering`) | `by_service[].service` |
| record without a product field | explicit `Uncategorized` bucket + warning (breakdown always reconciles) |

All money handling uses `Decimal(str(value))` — no binary-float
accumulation. When charge records are present the canonical total is
their exact Decimal sum, so `sum(by_service) == total_cost` by
construction (a unit test asserts reconciliation and a mismatch
detector).

## Period semantics

`estimated_charges` answers **only for the current Rackspace billing
cycle**; it does not support arbitrary date-range queries and the
integration does not pretend otherwise.

* Requests whose window **overlaps** the actual source period
  (including a 30-day lookback spanning two cycles, or
  `month=<current>`) are served as `SUCCESS`, with the envelope
  exposing the **actual source period returned by Rackspace** — not
  the requested window. When the two differ, the
  `period_adjusted_to_source_billing_cycle` warning is attached.
* Requests with **zero overlap** (historical or future windows)
  receive an explicit controlled error: HTTP 422
  `PERIOD_UNSUPPORTED` with a message naming the supported cycle.
  Current-cycle data is **never silently substituted**.
* `period.days` uses the canonical exclusive-end convention
  (``start`` inclusive, ``end`` exclusive — the live cycle
  ``2026-10-19`` → ``2026-11-19`` is exactly 31 days).

## Historical totals investigation

`GET /v2/accounts/{ran}/invoices/latest` can return a total for the
just-ended cycle. Using it to populate the previous-period comparison
would require alignment guarantees (invoice coverage window vs the
canonical previous-period bounds, currency consistency, partial /
disputed invoice states) that are non-trivial to prove safe.
Arbitrary historical windows would additionally need invoice
pagination. Historical support is therefore **deferred to Phase 9B
follow-up**; `previous_period_cost` / `change_amount` /
`change_percent` are `null` with `previous_period_unavailable=true`
(fabricated baselines are a correctness violation — Phase 7D.3.3).

## True-zero semantics

A valid HTTP 200 with `total == 0` — including an empty charge-record
list totalling zero — is a **SUCCESS**. It is cached as authoritative
last-known-good exactly like any other success. Zero cost, empty
account, API failure, permission failure, and missing configuration
are five distinct conditions and are never conflated:

| Condition | Outcome |
|---|---|
| Valid 200, total $0 | `SUCCESS`, authoritative |
| Valid 200, empty records | `SUCCESS`, empty breakdown |
| 401 / 403 / 404 / 429 / 5xx / timeout / malformed | typed error → HTTP 401/403/404/429/502/503/422 — **never** `$0` |
| RAN not configured | `NOT_CONFIGURED` → 503 |

## Failure semantics

| Category | HTTP | Trigger |
|---|---|---|
| `NOT_CONFIGURED` | 503 | RAN (or credentials) missing |
| `AUTH_FAILED` | 401 | Identity rejected credentials / token (one re-auth retry on 401) |
| `ACCESS_DENIED` | 403 | billing:observer not granted |
| `NOT_FOUND` | 404 | invalid / inaccessible Billing Account |
| `THROTTLED` | 429 (+`Retry-After`) | HTTP 429 |
| `NETWORK_ERROR` | 502 | timeout, network, 5xx, non-JSON |
| `INVALID_RESPONSE` | 502 | 200 with unusable payload (missing currency / period / amount, mixed currency, non-reconciling breakdown) |
| `PERIOD_UNSUPPORTED` | 422 | requested window has zero overlap with the current cycle |

Error envelopes carry stable category codes only — never the API
key, X-Auth-Token, RAN, or raw Rackspace payloads.

## Cache behavior

The Rackspace cache reuses the Phase 7D.3.3 shared read-through
machinery (generalized with `cache_namespace` /
`storage_account_id` parameters — Azure behavior unchanged):

* namespace `rackspace` — AWS / Azure / Rackspace rows can never
  collide on `(account_id, cache_key)`;
* cache keys are SHA-256 hashes over provider/scope/region/kind/
  signature; storage ids hash the tenant id (`rackspace:acct:<sha256>`)
  — the RAN is never part of any key;
* single-flight per `(namespace, storage_id, cache_key)`;
* TTL knobs: `RACKSPACE_COST_CACHE_FRESH_TTL_SECONDS` (default 600)
  and `RACKSPACE_COST_CACHE_STALE_TTL_SECONDS` (default 3600) —
  conservative values aligned with the established Azure convention
  (estimated charges advance during the cycle; 10 minutes keeps
  totals current without hammering the Billing API);
* states: `HIT` (fresh, no upstream call), `REFRESHED` (miss or
  refresh), `STALE` (bounded last-known-good fallback when the
  refresh fails within the stale window), `MISS` (no LKG exists —
  the typed error propagates);
* a database outage degrades to live loads (never a 5xx from the
  cache layer itself).

## Last-known-good (authoritative data rule)

Only a valid successful snapshot may advance LKG:

* `status=SUCCESS` with `successful_accounts > 0` → advances
  `payload` + `stale_payload` (a true `$0` **is** authoritative);
* 401 / 403 / 404 / 429 / timeout / 5xx / malformed / configuration
  failures **never** overwrite last-good data — the row is left
  untouched and the error propagates (with the STALE fallback
  serving the still-valid LKG payload);
* a non-authoritative live response is returned to the caller but is
  **not** persisted, so a later request cannot HIT-serve an
  inauthentic payload.

## Service breakdown

Records are grouped **only** by reliable, documented product fields
(`service`, `offeringCode`, `product`, `category`, `productName`,
`offering` — first match per record). Records without any product
field land in an explicit `Uncategorized` bucket with a warning. No
category is ever inferred from undocumented positional assumptions,
and the breakdown always reconciles exactly with the canonical total.

## Unsupported dimensions

| Dimension | Behavior |
|---|---|
| `daily_trend` | empty collection — `estimated_charges` has no reliable per-day granularity; daily points are never derived from the cycle total |
| `by_region` | empty collection — the response carries no regional dimension |
| `previous_period` | `null` + `previous_period_unavailable=true` (Phase 9B follow-up) |
| historical totals | `PERIOD_UNSUPPORTED` (422) |

## Security / read-only guarantee

* Product transport exposes **no** `PUT` / `PATCH` / `DELETE`
  (enforced structurally on `RackspaceHttpClient`; unit-tested).
* The only write-like verb in the entire integration is the
  Identity authentication `POST`.
* Never logged: `RACKSPACE_API_KEY`, `X-Auth-Token`,
  `RACKSPACE_BILLING_ACCOUNT`, authorization-like credentials.
  Cache keys and storage ids are hashed; no plaintext identifiers.
* Phase 8.4 secret-validation behavior is preserved (Phase 9A
  secret-blanking conftest + `test_secrets_not_logged` extended
  coverage in the new suites).
* `validate_account` fails closed (`ACCESS_DENIED`) when an elevated
  role (`admin`, `billing:admin`, `creator`, `identity:*`) is
  observed on the principal.

## Account discovery

The single Rackspace account is **derived, not manually inserted**:
at startup (`app.main._phase9b1_bootstrap_rackspace_account`) an
idempotent bootstrap inserts a `cloud_accounts` row from
`RACKSPACE_ACCOUNT_ID` (tenant id) +
`RACKSPACE_ACCOUNT_DISPLAY_NAME` (default `Rackspace Cloud`) when
the full cost configuration (Identity credentials + RAN) is present.
The row carries only non-secret metadata
(`credential_strategy=RACKSPACE_API_KEY`,
`bootstrap_source=phase9b1`). The provider appears in
`/api/cloud/providers` and in the account selector once registered.
No DB migration was required — the `cloud_accounts.provider` CHECK
constraint already allowed `'RACKSPACE'` (Phase 7C.1).

## Validation

* Unit/integration suites (mocked HTTP — no real credentials):
  * `tests/test_phase9b1_rackspace_cost_normalization.py`
  * `tests/test_phase9b1_rackspace_adapter.py`
  * `tests/test_phase9b1_rackspace_canonical_route.py`
    (includes cache HIT/REFRESH/STALE, LKG poisoning protection,
    true-zero caching, provider cache isolation, cross-provider
    regression, secret redaction)
* Live validator: `scripts/phase9b1_rackspace_cost_validation.sh`
  (exit 0 success / 1 failure / 2 precondition) — proves Identity,
  billing:observer, no elevated roles, Billing GETs, the canonical
  `provider=RACKSPACE` response (currency / period / total /
  breakdown reconciliation), read-only transport, zero secret
  leakage, and valid-zero-vs-error distinction.

## Rollback

Revert the Phase 9B.1 commits (single feature branch, no `main`
changes). The integration is fully additive: reverting restores the
Phase 9A state with no schema changes to undo. The only runtime
artifacts are `cost_cache` rows under the `rackspace` namespace
(harmless if left in place; removable with
`DELETE FROM cost_cache WHERE cache_namespace = 'rackspace';`) and
the derived `cloud_accounts` row (removable via the account API or
SQL). To disable without reverting: remove the `RACKSPACE_*`
environment passthrough from `docker-compose.yml` and recreate the
backend — the bootstrap then becomes a no-op and the canonical route
reports `NoAccountsRegistered` (503).

## Known limitations

* Current billing cycle only — historical / custom windows are
  rejected with `PERIOD_UNSUPPORTED` (see above).
* No previous-period comparison; no daily trend; no regional
  breakdown (source does not provide them).
* Single billing scope per deployment (one RAN); the registry model
  supports multiple rows but the cost path addresses the one
  configured billing account.
* `estimated_charges` record field names are accepted from the
  documented variants; a record without any recognized product field
  is bucketed as `Uncategorized` rather than guessed.

## Next step — Phase 9B.2

Rackspace **resources** (Cloud Servers inventory via the Phase 9A
`servers` client → `get_resources` / `get_resource_summary`), then
canonical resource surfaces, frontend integration, and AI FinOps
context. The adapter deliberately raises `NotImplementedError` for
those surfaces today so any premature call is loud.
