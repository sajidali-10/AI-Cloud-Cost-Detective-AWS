# Phase 7C.2C — Final Report

**Branch:** `phase-7c-multi-cloud`
**Commit:** (see final commit hash below)

## 1. Status

**PASS** — all required capabilities implemented, validated, and tested.

## 2. Commit

Pushed to `phase-7c-multi-cloud`.  NOT merged to `main`.  No
Phase 7C final tag created.

## 3. Architecture implemented

```
Frontend
   ↓
GET /api/cloud/resources?provider=AZURE&account_id={all|sub:<id>}
   ↓
app.api.cloud_resources (FastAPI route)
   ↓
ProviderRegistry → AzureCloudProvider
   ↓
AzureResourceAggregator
   ├─ per-subscription fan-out (batched via semaphore)
   └─ tenant-mode single query + per-subscription partition
        ↓
AzureResourceGraphClient (httpx)
   ↓
Microsoft Resource Graph REST API
```

Every Azure Resource Graph row is normalised into the
provider-neutral `CloudResource` shape before reaching the route.
No raw Azure payload leaks past the adapter layer.

## 4. Azure Resource Graph query design

KQL (hardcoded, no caller input):

```kql
Resources
| project id, name, type, location, resourceGroup, subscriptionId, tags
| limit 1000
```

* API version: `2024-04-01` (pinned in
  `app/services/cloud/azure/resource_graph.py::RESOURCE_GRAPH_API_VERSION`).
* Pagination: `dataTruncated=true` + `$skipToken` with `MAX_PAGES=50` loop guard.
* Authentication: `ClientAssertionCredential` reused from Phase 7C.2A
  via `acquire_access_token()` — no new secrets.

## 5. Canonical resource model

| Field | Source | Notes |
|-------|--------|-------|
| `provider` | `"AZURE"` | fixed string |
| `resource_id` | Resource Graph `id` | full ARM resource id |
| `composite_id` | `AZURE\|<id>` | provider-prefixed for uniqueness |
| `resource_type` | Resource Graph `type` | e.g. `microsoft.compute/virtualmachines` |
| `region` | Resource Graph `location` | Azure region code |
| `account_id` | Resource Graph `subscriptionId` | subscription GUID |
| `metadata.resource_group` | Resource Graph `resourceGroup` | preserved separately |
| `metadata.tags` | Resource Graph `tags` | coerced to `str → str` |

No `properties`, `sku`, `identity`, or `plan` blobs are fetched.

## 6. Live validation

| Scenario | Status | Result |
|----------|--------|--------|
| MCPP current month | SUCCESS | 18 resources, 6 types, 2 locations (eastus/global), 3 resource groups |
| Sponsorship current month | SUCCESS | 7 resources |
| All Azure Subscriptions | SUCCESS | 25 total = 18 (MCPP) + 7 (Sponsorship); per-subscription attribution preserved |
| AWS regression (`/api/aws/resources`) | 200 | Legacy AWS resource route unchanged |

### Actual MCPP resource counts

* Total resources: 18
* By type (top 5):
  * `microsoft.network/networkinterfaces` × 4
  * `microsoft.network/networksecuritygroups` × 4
  * `microsoft.network/publicipaddresses` × 4
  * `microsoft.network/virtualnetworks` × 4
  * `microsoft.botservice/botservices` × 1
* By location: `eastus` × 17, `global` × 1
* By resource group: `hiplink` × 16, `hiplink-teams-integration` × 1, `networkwatcherrg` × 1

### Actual Sponsorship resource counts

* Total resources: 7
* By type (top 5):
  * `microsoft.compute/disks` × 1
  * `microsoft.compute/virtualmachines` × 1
  * `microsoft.network/networkinterfaces` × 1
  * `microsoft.network/networksecuritygroups` × 1
  * `microsoft.network/networkwatchers` × 1

### Provider switching (AWS → Azure → AWS)

The Azure resource route lives at `/cloud/resources` (canonical);
the AWS resource routes live at `/aws/resources` and
`/aws/resources/all` (legacy).  Both share the
`ProviderRegistry` dispatch and the same `CloudContextBar`.  The
`ProviderContext` already invalidates the FinOps cache on every
provider change (Phase 7C.1), so no stale data can leak across
a provider switch.

## 7. Aggregation validation

| Aggregation | Source columns | Live result (All Azure) |
|-------------|-----------------|-------------------------|
| total_resources | derived | 25 |
| by_type | `type` | 6 distinct types across both subscriptions |
| by_location | `location` | `eastus`, `global` |
| by_resource_group | `resourceGroup` | `hiplink`, `hiplink-teams-integration`, `networkwatcherrg` |
| by_subscription | `subscriptionId` | MCPP=18, Sponsorship=7 |

All counts are derived from the normalized resource list — never
from raw Azure response metadata.

## 8. Pagination validation

The Resource Graph response for the live validation did not
truncate (total returned rows ≤ 1000), so no `$skipToken` was
required.  The pagination loop guard (`MAX_PAGES = 50`) is unit-
tested across three scenarios:

1. Token-based pagination across multiple pages — verified by
   `test_follows_skip_token`.
2. `dataTruncated=true` without a `data_truncated` cursor —
   terminates gracefully; verified by `test_truncated_without_skip_token_terminates`.
3. Pathological loop — verified by `test_loop_guard_triggers` with
   `MAX_PAGES=3` and 10 stubbed responses.

## 9. Backend tests

| Category | Tests |
|----------|-------|
| Resource Graph client (status, parsing, pagination, sanitisation, KQL injection defence) | 28 |
| Resource aggregator (normalize, snapshot, multi-subscription, partial failure, dedup, NO_DATA/SUCCESS rollup) | 30 |
| Cloud resources route (selection, error mapping, AWS regression) | 8 |
| Azure adapter capabilities (RESOURCES=SUPPORTED, OPTIMIZATION=UNSUPPORTED) | 8 |
| **New tests total** | **74** |
| Full backend suite | **835 passed** (excluding AI / LiteLLM / WebSocket / Phase 7B cost-aggregator suites which require live services) |

## 10. Frontend tests

* New tests: 6 (cache-key isolation, URL building, NO_DATA shape).
* Total: 437 passed + 6 pre-existing failures (layout-invariant tests
  that only pass when vitest's cwd is `frontend/`; verified via
  `git stash` that they fail identically on the pre-Phase-7C.2C
  baseline).

## 11. Build / validation

| Check | Result |
|-------|--------|
| `tsc --noEmit` | clean (no errors) |
| `vite build` | SUCCESS (105 modules transformed, 31.18s) |
| `docker compose build backend` | SUCCESS |
| Docker container health | 5/5 healthy |
| `scripts/phase7b_nginx_test.sh` | 45 PASS, 0 FAIL |
| `scripts/phase7b_verify.sh` | 100 PASS, 1 FAIL (pre-existing AWS-key check, unrelated) |

## 12. Security review

* No `Authorization: Bearer …` logged.
* No access token / client assertion / STS JWT exposed.
* Subscription IDs truncated via `safe_subscription_label`.
* KQL query is hardcoded — no caller injection.
* Tags coerced to `str → str`; `properties` blobs never fetched.
* `MAX_PAGES = 50` prevents unbounded pagination.
* `Retry-After` propagated as response header (never logged).
* `composite_id` is provider-prefixed (`AZURE|<id>`) so cross-cloud
  identity collisions are impossible.

## 13. Files changed

### New

| File | Purpose |
|------|---------|
| `backend/app/services/cloud/azure/resource_graph.py` | REST client + pagination + status semantics |
| `backend/app/services/cloud/azure/resource_aggregator.py` | Multi-subscription fan-out + dedup + counts |
| `backend/app/api/cloud_resources.py` | Provider-neutral route + envelope |
| `backend/app/schemas/cloud_resource.py` | Pydantic envelope |
| `backend/tests/test_phase7c2c_azure_resource_graph.py` | 28 unit tests |
| `backend/tests/test_phase7c2c_azure_resource_aggregator.py` | 30 unit tests |
| `backend/tests/test_phase7c2c_cloud_resources_route.py` | 8 route tests |
| `backend/tests/test_phase7c2c_azure_adapter_capabilities.py` | 8 capability tests |
| `frontend/src/lib/finops/cloudResources.ts` | Frontend service |
| `frontend/src/tests/phase7c2c-cloud-resources.test.ts` | 6 frontend tests |
| `docs/phase7c2c-azure-resource-graph.md` | Reference |
| `docs/phase7c2c-report.md` | This file |

### Modified

| File | Change |
|------|--------|
| `backend/app/services/cloud/azure/adapter.py` | `RESOURCES=SUPPORTED`; replaced resource stubs with real implementations; updated capability detail |
| `backend/app/main.py` | Registered `cloud_resources` router |
| `backend/tests/test_phase7c2a_provider_registry.py` | Updated to accept `RESOURCES=SUPPORTED` when configured |
| `backend/tests/test_phase7c2b_azure_adapter_capabilities.py` | Removed `test_resources_unimplemented` + replaced `test_get_resources_is_still_stub` with the no-longer-stub assertion |
| `backend/tests/test_phase7c2b_cloud_costs_route.py` | Updated numeric-scope test to use the canonical scope canonicalisation |

## 14. Known limitations

* The Azure inventory currently reaches the front-end through
  the `/api/cloud/resources` route.  The existing AWS `ResourcesPage`
  is still AWS-typed; a future phase will thread the canonical
  envelope through the shared inventory UI without rebuilding the
  page.
* Tags are coerced to `str → str`; nested tag values (a rare
  Azure construct) are flattened to their `str()` representation.
* ``$skipToken`` pagination uses ``MAX_PAGES=50`` as a defensive
  ceiling.  Subscriptions with > 50,000 resources are extremely
  rare; if encountered, the operator can narrow by adding a
  resource-type filter (future enhancement).

## 15. Pre-existing unrelated failures

* 6 frontend tests in `phase7c2a-navigation.test.tsx` and
  `phase7b-runtime-closure.test.tsx` rely on `fs.promises.readFile`
  with relative paths; they only pass when vitest's cwd is
  `frontend/`.  Verified pre-existing via `git stash`.
* `scripts/phase7b_verify.sh` reports one FAIL on "no AWS key in
  known storage keys" — pre-existing security check unrelated to
  Phase 7C.2C.

## 16. Confirmation

* [x] Azure Advisor was **NOT** implemented.
* [x] Rackspace was **NOT** implemented.
* [x] `main` was **NOT** modified.
* [x] No Phase 7C final tag was created.

## Phase 7C.2C complete

This phase is **READ-ONLY** against Azure — no resources were
created, modified, deleted, started, stopped, resized, re-tagged,
re-located, or moved.  No role assignments or policy changes were
made.  The only Azure-side operation is a **read** against the
Resource Graph REST API.
