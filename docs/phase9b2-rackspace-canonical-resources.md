# Phase 9B.2 — Rackspace Canonical Resources

**Status:** complete — live validation PASS=29 FAIL=0 WARN=0
**Branch:** `phase-9b2-rackspace-resources` (baseline: `phase-9b1-complete`)
**Scope added:** provider=`RACKSPACE` on the canonical resource surface
(`GET /api/cloud/resources?provider=RACKSPACE`), the catalog-driven
multi-region read-only inventory client, and the canonical
normalization/aggregation layer for Cloud Servers, Cloud Block
Storage, and Cloud Load Balancers.

No frontend changes. No optimization. No AI. Phase 9C not started.
This phase introduces zero product write capability (see
*Security / read-only guarantees*).

---

## 1. Architecture

Three components, all built on the Phase 9A transport/Identity
infrastructure (there is exactly one Rackspace authentication
implementation in the codebase):

| Component | File | Role |
|---|---|---|
| Inventory client | `backend/app/services/rackspace/inventory.py` | `RackspaceInventoryClient` — resolves regional product endpoints from the Identity service catalog and walks marker-paginated OpenStack list endpoints. |
| Canonical aggregation | `backend/app/services/cloud/rackspace/resources.py` | Normalizes raw payloads into `CloudResource` / `CloudResourceSummary`; per-(service, region) failure isolation; empty-success and partial-success semantics. |
| Route integration | `backend/app/api/cloud/resources.py` | `provider=RACKSPACE` branch + `_build_rackspace_envelope` + sanitized `RackspaceResourceError → HTTP` mapping. |

The provider adapter (`backend/app/services/cloud/rackspace/adapter.py`)
now declares `RESOURCES = SUPPORTED` (when the Identity credentials
are configured) and implements `get_resources` / `get_resource_summary`
by delegating to the same canonical aggregation the route uses.

Nothing is hard-coded to a product URL: every product endpoint is
resolved from the Identity service catalog
(`cloudServersOpenStack` / `cloudServers` / `compute`,
`cloudBlockStorage` / `volumev2` / `volumev3`,
`rax:load-balancer` / `cloudLoadBalancers`). The Identity URL itself
is the one pinned endpoint, exactly as in Phase 9A.

## 2. Cloud Servers

- **API:** `GET /servers/detail` per region + `GET /flavors/detail`
  once per region (indexed by id — no N+1 per-server calls).
- **Captured per server:** id, name, status (upper-cased), region,
  availability zone, created timestamp (normalized to ISO-8601 UTC),
  flavor id + resolved flavor name / RAM MB / vCPUs / disk GB, image id.
- **Flavor failure degradation:** if `/flavors/detail` fails, the
  region degrades to flavor-id-only metadata and appends a warning —
  server inventory is never discarded because an enrichment endpoint
  failed.
- **Never surfaced:** `addresses` (IPs), `adminPass`, `personality`,
  user-data, the OpenStack `metadata` dict, `hostId`,
  `OS-EXT-SRV-ATTR:*` host attributes.

## 3. Cloud Block Storage

- **API:** `GET /volumes/detail` per region.
- **Captured per volume:** id, name (`displayName`), status, size GB,
  volume type, region, availability zone, created timestamp, and
  **attachment state** — `ATTACHED` (with the sorted, distinct
  `attached_server_ids`) or `DETACHED`. A detached volume is a valid,
  fully-inventoried resource; utilization inference is deferred to
  Phase 9C.
- Device paths and connection credentials are never copied.

## 4. Cloud Load Balancers

- **API:** `GET /loadbalancers` (v1) per region, resolved from the
  `rax:load-balancer` / `cloudLoadBalancers` catalog identifiers.
- **Captured per LB:** id, name, status, protocol, port, algorithm,
  created timestamp (Rackspace's nested `{"time": ...}` shape is
  supported).
- Node lists and virtual IPs are deliberately excluded (operational
  detail with no FinOps value in this phase).
- If the service is absent from the tenant catalog, or the legacy v1
  API fails in every region, the capability is **accurately reported**
  as a sanitized per-service failure (`UNSUPPORTED` / `ACCESS_DENIED`
  / `NOT_FOUND`) — never fabricated, never allowed to discard the
  other services. On the live tenant the v1 API answers and returned
  **0 load balancers — a valid SUCCESS** (see §8).

## 5. Multi-region, pagination, identity

- **Multi-region:** the Identity catalog publishes one endpoint per
  authorized datacenter; discovery queries *every* regional endpoint
  per service and stamps the region onto each resource. The live
  tenant published 5 regions; servers exist in DFW + ORD (see §8).
- **Canonical identity:**
  `RACKSPACE|<tenant>|<region>|<resource_type>|<provider_id>` —
  Rackspace ids are not assumed globally unique, so the composite id
  is region-qualified defensively. The flat list is deduplicated by
  composite id (aggregation guard; 0 duplicates live).
- **Pagination:** a generic marker walker handles Nova
  (`servers_links`), Cinder (`volumes_links`), Cloud Load Balancers
  (`loadBalancers_links` / `loadBalancer_links`) and a bare `links`
  fallback. It terminates on no-next-link, empty page, unchanged
  marker, or duplicate ids, with a hard cap of 50 pages per
  collection (defence against hostile endpoints).

## 6. Status semantics

- **Empty success is SUCCESS.** HTTP 200 with `{"servers": []}` (or
  `[]` volumes / load balancers) is a successful inventory of an
  empty tenant — never NO_DATA, never an error. A tenant with zero
  resources yields `status=SUCCESS, total_resources=0`.
- **Partial success is preserved.** Each (service, region) fetch is
  failure-isolated. Failed services surface as sanitized per-service
  errors (`subscription_errors` on the wire envelope, keyed to the
  safe Cloud tenant id; `service_status` in the summary extensions)
  and the overall status becomes `PARTIAL_SUCCESS`. The successful
  services' resources are always retained.
- **Total failure is an error, never an empty list.** Missing
  configuration, Identity authentication failure, an elevated role on
  the principal (fail-closed), or every service failing everywhere
  raises `RackspaceResourceError`, mapped to a precise HTTP envelope
  (503 NOT_CONFIGURED / 401 AUTH_FAILED / 403 ACCESS_DENIED /
  404 NOT_FOUND / 429 THROTTLED + Retry-After / 502 UPSTREAM_ERROR).
  Error messages carry stable category semantics only.
- **Reconciliation:** `total_resources == len(resources)` and the
  `by_type` / `by_location` / `by_status` / `by_subscription` sums all
  equal `total_resources` — enforced by tests and by the live
  validator on every run.

## 7. Security / read-only guarantees

- **Product APIs are GET-only.** `RackspaceHttpClient` exposes `get`
  and `post_json` **only**; `post_json` is reserved for the Identity
  authentication call. `RackspaceInventoryClient` exposes no
  post/put/patch/delete at all (asserted in tests and in the live
  validator).
- **Zero product write requests**, proved two ways live:
  1. static: no write verbs exist on the transport/inventory surface;
  2. dynamic: a transport-level spy wrapped both verbs during a real
     aggregation run — 20 product GETs across 4 distinct paths
     (`/servers/detail`, `/flavors/detail`, `/volumes/detail`,
     `/loadbalancers`), exactly 1 POST (Identity `/v2.0/tokens`),
     **0** product write requests.
- **No leakage:** the API key, X-Auth-Token, and Billing Account
  Number (RAN) are never logged or serialized. The RAN plays no role
  on the resource path and is never read there. The validator
  string-scans every captured artifact — 0 hits.
- **Role discipline:** the principals must hold observer roles only;
  any elevated role fails closed (`ACCESS_DENIED`).

## 8. Live validation results (2026-10-24)

`scripts/phase9b2_rackspace_resource_validation.sh` — **PASS=29
FAIL=0 WARN=0** against the real Rackspace APIs and the deployed
canonical route:

- Rackspace Identity authentication: OK; roles
  `billing:observer, compute:default, identity:default,
  identity:tenant-access, object-store:default, observer`; no
  elevated roles.
- Service catalog: 21 service types published; 5 regions queried
  (DFW, HKG, IAD, ORD, SYD).
- **Cloud Servers: 34 servers** — regions DFW (ORD co-located),
  all `ACTIVE`; flavors: 4 GB General Purpose v1 ×20, 8 GB General
  Purpose v1 ×11, 15 GB Compute v1 ×1, 2 GB General Purpose v1 ×1,
  8 GB Classic v1 ×1.
- **Cloud Block Storage: 16 volumes, 1,195 GB** — 8 `IN-USE`
  (ATTACHED), 8 `AVAILABLE` (DETACHED).
- **Cloud Load Balancers: 0** — v1 API reachable and answered;
  **empty list = SUCCESS** (the empty-success contract verified on a
  live endpoint).
- Canonical route `/api/cloud/resources?provider=RACKSPACE`:
  `status=SUCCESS`, `scope=all`, `source=RACKSPACE_CLOUD_API`,
  `total_resources=50`, route total == aggregator total, breakdown
  sums reconcile, 0 duplicate composite ids.
- Product write requests: **0**. Secret leakage: **0**.

## 9. Tests

Focused suite (all stubbed HTTP; no real credentials, no network):
**71 passed**.

- `backend/tests/test_phase9b2_rackspace_resources.py` — 39 tests:
  normalization (servers/volumes/LBs, sensitive-field exclusion),
  pagination (marker shapes, duplicate prevention, unchanged-marker
  guard, max-pages cap), catalog resolution (type-or-name matching,
  multi-region, URL dedupe), aggregation (empty=SUCCESS, multi-region
  rollups, flavor once-per-region, flavor-degradation, partial
  success, missing-from-catalog=UNSUPPORTED), total failures
  (config/auth/transport/elevated-role/dominant throttle),
  reconciliation, log hygiene, read-only surface.
- `backend/tests/test_phase9b1_rackspace_adapter.py` — 24 tests
  (updated for RESOURCES=SUPPORTED and the delegating
  `get_resources`/`get_resource_summary`).
- `backend/tests/test_phase7c2c_cloud_resources_route.py` — 8 tests
  (canonical route regression).

## 10. Deferred items

- **Optimization / recommendations** — Phase 9C (adapter keeps
  `OPTIMIZATION = UNSUPPORTED`; `get_recommendations` raises
  `NotImplementedError`).
- **Metrics, Advisor, AI evidence** — out of scope, `UNSUPPORTED`.
- **Cloud Databases / DNS / other catalog services** — evaluated and
  deliberately not inventoried in 9B.2; the catalog-driven design
  makes adding a service an identifier-table change later.
- **Per-account `acct:<tenant>` scoping** — implemented on the route;
  multi-tenant Rackspace registries are not present in this
  deployment (single tenant).

## 11. Validator

```bash
bash scripts/phase9b2_rackspace_resource_validation.sh
```

Reads `RACKSPACE_USERNAME` / `RACKSPACE_API_KEY` /
`RACKSPACE_ACCOUNT_ID` from the host `.env` (values never printed);
requires the backend container running the Phase 9B.2 image. Zero
live resources is a valid passing state. Exit codes: 0 pass / 1
validation failure / 2 precondition failure.
