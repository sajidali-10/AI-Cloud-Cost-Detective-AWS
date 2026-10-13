# Phase 7C.1 — Provider Abstraction

> Phase 7C.1 introduces the provider-neutral layer that lets
> future adapters (Azure, Rackspace) plug in without rewriting
> the application.  AWS remains the only live adapter today —
> no AWS behaviour changes; no Azure / Rackspace implementation
> ships in this phase.

## 0. Goals

1. Define a provider-neutral domain model so the application
   can produce cost / resource / recommendation data from any
   provider through a uniform shape.
2. Wrap the existing Phase 7B AWS code paths in a
   `CloudProvider` adapter — **no AWS behaviour changes**.
3. Centralize the registry / factory so a new provider only
   needs to import one module to wire in.
4. Evolve the `cloud_accounts` schema so future Azure /
   Rackspace rows can be inserted without a schema change.
5. Reserve credential-strategy types for Azure workload
   federation and client-secret flows (shapes only — no
   credentials stored).
6. Expose a provider-neutral HTTP surface at `/api/cloud/*`
   alongside the existing `/api/aws/*` routes.
7. Plumb a `ProviderContext` above `AccountContext` in the
   frontend and include the provider in every FinOps cache key.
8. Tag AI evidence with provider + account so the AI Cost
   Analyst can distinguish AWS from future-Azure evidence.

## 1. Layering

```
FastAPI routes
    │
    ▼
CloudProvider ABC  ◀── the only contract the routes know about
    │
    ▼
AWSCloudProvider   ◀── wraps Phase 7B code (no behaviour change)
    │
    ▼
AWS service modules (cost_explorer, resource_aggregator, …)
```

Two principles:

1. **Do not duplicate AWS service logic.**  The AWS adapter is
   a thin wrapper that re-uses the existing
   `app.services.aws.*` modules.  No data shapes are
   re-derived; the adapter maps existing dataclasses into the
   normalized models below and back.
2. **Provider-neutral where it matters, provider-specific where
   it doesn't.**  Cost / resource / recommendation shapes carry
   `provider`, `account_id`, `region`, and `source` on every
   record so a future `All Clouds` aggregate can attribute
   every figure.  Provider-specific metadata lives under a
   free-form `extensions` dict.

## 2. Module map

| Module | Purpose |
|---|---|
| `app/services/cloud/base.py` | `CloudProvider` ABC, `CloudCapability`, `CloudCapabilityState`, `CloudCredentialStrategy`, `CloudProviderStatus`, `AccountValidationResult` |
| `app/services/cloud/models.py` | `CloudAccount`, `CloudCostSummary`, `CloudCostTrend`, `CloudResource`, `CloudResourceSummary`, `CloudRecommendation`, `CloudServiceCost`, `CloudProviderKind`, `CloudRegion` |
| `app/services/cloud/registry.py` | Process-wide `ProviderRegistry` singleton |
| `app/services/cloud/aws/__init__.py` | Sub-package init |
| `app/services/cloud/aws/adapter.py` | `AWSCloudProvider` — wraps Phase 7B code |
| `app/api/cloud_providers.py` | `/api/cloud/*` HTTP routes |
| `frontend/src/lib/providerContext.tsx` | `<ProviderProvider>` + `useProviderContext()` |

## 3. Schema evolution — `0005_cloud_provider_abstraction`

| Change | Reason |
|---|---|
| `account_id` widened from `VARCHAR(12)` to `VARCHAR(64)` | Azure subscription GUIDs are 36 chars; Rackspace tenant IDs vary |
| `role_arn` made nullable | Azure / Rackspace rows do not have an IAM role |
| `provider` CHECK widened from `('AWS')` to `('AWS', 'AZURE', 'RACKSPACE')` | Reserved provider identifiers |
| New nullable column `tenant_id` | Entra ID tenant GUID (Azure) |
| New nullable column `subscription_id` | Azure subscription GUID |
| New column `provider_metadata` JSONB (NOT NULL, default `'{}'`) | Per-provider federation / connection hints |

No data migration is performed — every AWS row continues to
satisfy the new shape.  Downgrade reverses all of the above
in reverse order; it will fail with a constraint error if a
non-AWS row has been inserted, which is the intended operator
signal.

## 4. Credential strategies

```python
class CloudCredentialStrategy(str, Enum):
    AWS_INSTANCE_PROFILE = "AWS_INSTANCE_PROFILE"     # AWS management account
    AWS_ASSUME_ROLE      = "AWS_ASSUME_ROLE"          # AWS target accounts
    AZURE_WORKLOAD_FEDERATION = "AZURE_WORKLOAD_FEDERATION"   # reserved for 7C.2
    AZURE_CLIENT_SECRET   = "AZURE_CLIENT_SECRET"     # reserved for 7C.2 fallback
    RACKSPACE_API_KEY     = "RACKSPACE_API_KEY"       # reserved for 7C.3
```

Phase 7C.1 declares the type so a future Azure / Rackspace
adapter can declare its strategy up-front.  **No Azure or
Rackspace credential is ever stored at this point.**

## 5. Capability model

```python
class CloudCapability(str, Enum):
    COSTS, RESOURCES, OPTIMIZATION, METRICS, BILLING, ADVISOR, AI_EVIDENCE

class CloudCapabilityState(str, Enum):
    SUPPORTED, UNSUPPORTED, UNAVAILABLE, PERMISSION_DENIED, NOT_CONFIGURED
```

The AWS adapter declares `COSTS / RESOURCES / OPTIMIZATION /
METRICS / AI_EVIDENCE` as `SUPPORTED`.  `BILLING` and
`ADVISOR` are intentionally `UNSUPPORTED` — the AWS console
billing surface and Trusted Advisor are not part of this
read-only contract.  Future Azure / Rackspace adapters will
publish their own capability tables; the UI dispatches off
this table rather than hard-coding provider-specific
branching.

## 6. Registry semantics

```python
ProviderRegistry.current()         # process-wide singleton
registry.register(adapter)         # idempotent; rejects duplicate kinds
registry.disable(kind) / .enable(kind)
registry.require(kind)             # raises ProviderNotFound (registered vs disabled)
registry.try_get(kind)             # non-raising
registry.snapshot()                # {kind: adapter} for live adapters
```

Disabled providers fail explicitly — useful for staged
rollouts (e.g. Azure present but disabled in regions where
Entra ID is not yet wired).  No dynamic arbitrary imports:
adapters are imported at module load time via
`_register_known_providers`.

## 7. Provider-neutral HTTP routes

```
GET /api/cloud/providers                  ── list registered providers + capabilities
GET /api/cloud/accounts/selectable        ── selector payload across every provider
GET /api/cloud/accounts/selectable/{kind} ── selector payload scoped to a provider
```

Phase 7C.1: AWS is the only provider with rows in the
registry today, but the routes fan out across every registered
adapter so a future Azure / Rackspace adapter appears
automatically.  The existing Phase 7B routes
(`/api/cloud-accounts/...`, `/api/aws/...`) remain
untouched.

Reserved providers (`AZURE`, `RACKSPACE`) are surfaced as
`is_registered=false, status=NOT_REGISTERED` so the frontend
can render an "available in a future release" hint without
polling a second endpoint.

## 8. Frontend integration

```tsx
<ThemeProvider>
  <Router>
    <AuthProvider>
      <ProviderProvider>        {/* NEW — Phase 7C.1 */}
        <AccountProvider>       {/* Phase 7B */}
          <FinopsPeriodProvider>
            <AppRoot />
          </FinopsPeriodProvider>
        </AccountProvider>
      </ProviderProvider>
    </AuthProvider>
  </Router>
</ThemeProvider>
```

`ProviderContext` exposes:

| Field | Purpose |
|---|---|
| `provider`, `setProvider` | Active provider, persisted to `sessionStorage['cd.provider.selection.v1']` |
| `providers`, `refreshProviders` | Capability snapshot from `/api/cloud/providers` |
| `providerVersion` | Monotonic counter that bumps on every provider change; cache keys include it indirectly via the provider segment |
| `isProviderReady` | True iff `is_registered && is_enabled && status === 'OK'` |
| `providerSlug` | Lower-case wire form (`aws`, `azure`, `rackspace`) |

On logout the selection resets to `AWS` via a `storage` event
listener watching `cd.auth.token.v1`.

## 9. Cache isolation

Every FinOps cache key is now scoped on
`(provider, period, region, account)`:

```
{cacheProvider}:costs:days:30:us-east-1:acct:42
{cacheProvider}:optimization:summary:month:2026-10:default:all
{cacheProvider}:resources:us-east-1:all
{cacheProvider}:utilization:us-east-1:30:ec2,rds:all
```

`cacheProvider` is derived from `providerSlug`.  The
`ProviderProvider` exposes a `providerVersion` counter that
bumps on every provider change; pages consume it as a
dependency in the `useEffect` that invalidates the cache so a
provider switch always wipes stale entries before any new
fetch lands.

## 10. AI evidence attribution

Every `ResourceEvidence` carries `provider` (default `"AWS"`)
and `account_id` (default `None`).  Every `EvidenceResponse`
carries the same fields at the top level so the AI Cost
Analyst can disambiguate AWS from future-Azure evidence
without scanning every resource record.  Existing Phase 2
callers continue to produce schemas that pass validation
because the new fields default to `"AWS"` / `None`.

## 11. Phase 7B invariants — preserved

* Management AWS account still bootstraps on first boot.
* Production target AWS account still routed via
  `AssumeRole` + external ID.
* `All AWS Accounts` still returns the multi-account aggregate.
* Costs / Resources / Optimization flows are unchanged
  (the AWS adapter delegates to Phase 7B modules).
* Region filter behaviour is unchanged.
* Account lifecycle (register / edit / disable / delete) is
  unchanged.
* Auth / RBAC (ADMIN / ANALYST / VIEWER) is unchanged.
* nginx routing for `/api/cloud/*` follows the existing
  `proxy_pass http://backend;` pattern.

## 12. Out of scope (later phases)

* Live Azure integration (Phase 7C.2).
* Entra ID federation / client-secret wiring (Phase 7C.2).
* Rackspace APIs (Phase 7C.3).
* AWS IAM changes (none in Phase 7C.1).
* `All Clouds` aggregate that sums across providers (post-7C.3).

## 13. Verification

| Gate | Status |
|---|---|
| Backend pytest suite | green |
| Frontend test suite | green |
| Frontend typecheck | green |
| Frontend production build | green |
| `scripts/phase7b_verify.sh` | green |
| `scripts/phase7b_nginx_test.sh` | green |
| Docker 5/5 healthy | yes |
| Live AWS Management / Production / All validation | confirmed |
