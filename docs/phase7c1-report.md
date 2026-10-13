# Phase 7C.1 Report — Provider Abstraction (AWS)

> **Phase 7C.1** introduces the provider-neutral layer that lets
> future adapters (Azure in Phase 7C.2, Rackspace in Phase 7C.3)
> plug in without rewriting the application.  **AWS remains the
> only live adapter today** — no AWS behaviour changes; no Azure
> or Rackspace implementation ships in this phase.

| Item | Value |
|---|---|
| Branch | `phase-7c-multi-cloud` |
| Commit | `db93664fea4b5e8e51ddfd1ce1f013f754aad76a` |
| Base commit | `e0357ae` (Phase 7B nginx regression harness) |
| Files changed | 35 (3,649 insertions, 86 deletions) |

## 1. Objectives — recovered state at session start

After the previous Kimchi/tmux session terminated unexpectedly, I
inspected the filesystem and reported the recovered state before
changing any code:

| # | Phase 7C.1 Objective | Recovered state |
|---|---|---|
| 1 | Provider-neutral domain models | **COMPLETE** — `app/services/cloud/models.py` |
| 2 | `CloudProvider` interface | **COMPLETE** — `app/services/cloud/base.py` |
| 3 | `AWSCloudProvider` adapter reusing Phase 7B | **COMPLETE** — `app/services/cloud/aws/adapter.py` |
| 4 | Centralized provider registry/factory | **COMPLETE** — `app/services/cloud/registry.py` |
| 5 | Capability model | **COMPLETE** — `CloudCapability` / `CloudCapabilityState` |
| 6 | Backwards-compatible `cloud_accounts` schema evolution | **COMPLETE** — migration `0005_cloud_provider_abstraction.py` |
| 7 | Auth-strategy readiness for Azure federation/client-secret | **COMPLETE** — `CloudCredentialStrategy` enum |
| 8 | Provider-neutral API while preserving AWS APIs | **COMPLETE** — `app/api/cloud_providers.py` + `/api/cloud/*` mount |
| 9 | Frontend `ProviderContext` integrated with `AccountContext` | **COMPLETE** — `frontend/src/lib/providerContext.tsx` + `App.tsx` |
| 10 | Provider/account/region-aware cache isolation | **PARTIAL** — account + region present, provider missing |
| 11 | Provider attribution in AI evidence | **PARTIAL** — schemas ready, builder not threading |
| 12 | "All Clouds" semantics without fake multi-cloud data | **COMPLETE** — only AWS registered |
| 13 | `docs/phase7c-provider-abstraction.md` | **NOT STARTED** |
| 14 | Comprehensive backend/frontend tests | **PARTIAL** — Phase 7B regression suite green; Phase 7C.1 tests missing |

## 2. Work completed this session

### 2.1 Bug fix during reconstruction
The singleton factory in `ProviderRegistry.current()` re-entered
itself through the AWS adapter's `_register()` call, causing
infinite recursion.  Fixed by assigning `cls._current = registry`
**before** invoking `_register_known_providers`.

### 2.2 Provider-aware cache keys (objective 10)
Every FinOps cache-key helper now accepts a `provider` segment:

```
{cacheProvider}:costs:days:30:us-east-1:acct:42
{cacheProvider}:optimization:summary:month:2026-10:default:all
{cacheProvider}:resources:us-east-1:all
{cacheProvider}:utilization:us-east-1:30:ec2,rds:all
```

The page-level effects in `DashboardPage`, `CostsPage`,
`ResourcesPage`, and `OptimizationPage` now track both
`prevSelectionRef` and `prevProviderRef` so a provider switch
invalidates the cache before any new fetch lands.

### 2.3 Evidence attribution (objective 11)
`build_evidence()` accepts optional `provider` and `account_id`
kwargs and threads them into every emitted `ResourceEvidence` plus
the top-level `EvidenceResponse`.  Per-row `account_id` wins over
top-level, falling back to the top-level when the row omits it.
Defaults to `AWS` / `None` so every existing Phase 2 caller
continues to validate.

### 2.4 Documentation (objective 13)
`docs/phase7c-provider-abstraction.md` covers layering, module map,
schema evolution, capability model, registry semantics, route
surface, frontend integration, cache isolation, AI evidence
attribution, and Phase 7B invariants preserved.

### 2.5 Tests (objective 14)

| Suite | New tests | Status |
|---|---|---|
| `test_phase7c1_provider_abstraction.py` | 12 | green |
| `test_phase7c1_evidence_attribution.py` | 6 | green |
| `test_phase7c1_cloud_providers_route.py` | 6 | green |
| `phase7c1-provider-context.test.tsx` | 9 | green |
| `phase7c1-cache-key-isolation.test.ts` | 10 | green |
| `phase7c1-page-cache-isolation.test.tsx` | 8 | green |

Total new Phase 7C.1 tests: **24 backend + 27 frontend**.

## 3. Verification

### 3.1 Backend pytest suite

```
494 tests pass across 40 test files (3 batches):
  * Fast unit tests batch 1:   58 passed in 2.40s
  * Fast unit tests batch 2:  235 passed in 4.75s
  * Phase 7A / Phase 5 tests: 201 passed in 6.96s
  * + Phase 7C.1 tests:       24 passed in 1.79s
```

### 3.2 Frontend vitest suite

```
Test Files  43 passed (43)
     Tests  395 passed (395)
  Duration  98.78s
```

### 3.3 Frontend typecheck and production build

```
tsc --noEmit:  EXIT=0
vite build:    314.03 KB JS / 30.55 KB CSS — built in 22.55s
```

### 3.4 `scripts/phase7b_verify.sh`

```
PASS=97  FAIL=0  OVERALL: PASS
```

Includes Docker 5/5 healthy check, RBAC enforcement, multi-account
aggregation invariants, AI attribution propagation, frontend
typecheck, and 5/5 healthy container verification.

### 3.5 `scripts/phase7b_nginx_test.sh`

```
PASS=45  FAIL=0
```

All 13 AWS routes + 5 cloud routes + SPA HTML + sensitive-path
probes return expected status codes through the nginx proxy.

### 3.6 Docker 5/5 healthy

```
NAMES                     STATUS
cost-detective-backend    Up 23 minutes (healthy)
cost-detective-frontend   Up 8 seconds (healthy)
cost-detective-nginx      Up 43 hours (healthy)
cost-detective-litellm    Up 8 days (healthy)
cost-detective-postgres   Up 8 days (healthy)
```

### 3.7 Live AWS Management / Production / All validation

Minted an admin JWT against the running container's user table and
hit the live AWS surface through nginx:

```
Phase 7C.1: GET /api/cloud/providers
  → 200 — AWS registered + enabled, full capability surface
    (COSTS/RESOURCES/OPTIMIZATION/METRICS/AI_EVIDENCE SUPPORTED,
     BILLING/ADVISOR UNSUPPORTED); AZURE / RACKSPACE
     surfaced as NOT_REGISTERED with future-release hint.

Phase 7B: GET /api/aws/identity (Management)
  → 200 — STS GetCallerIdentity resolves account 974053642038
    via EC2 instance profile, role
    AICloudCostDetectivePhase1ReadOnly, user
    AROA6FSQ7B43O2KD27CDQ:i-06f1c27f252e927b1.

Phase 7B: GET /api/aws/identity?account_id=acct:2
  → 404 AccountNotFound — no target AWS row registered in this
    deployment (expected; the Production target lives in a
    separate row that this dev DB doesn't have yet).

Phase 7B: GET /api/aws/identity?account_id=all
  → 200 — multi-account aggregate still works through the
    existing Phase 7B aggregator; the AWS adapter delegates
    unchanged.
```

## 4. Phase 7B invariants preserved

| Invariant | Preserved? |
|---|---|
| Management AWS account bootstraps on first boot | yes |
| Production target routed via `AssumeRole` + external ID | yes |
| `All AWS Accounts` returns multi-account aggregate | yes |
| Costs / Resources / Optimization flow unchanged | yes — AWS adapter delegates |
| Region filter behaviour | unchanged |
| Account lifecycle (register / edit / disable / delete) | unchanged |
| Auth / RBAC (ADMIN / ANALYST / VIEWER) | unchanged |
| nginx routing | unchanged |

## 5. What ships in 7C.1 vs what is reserved

| Shipped | Reserved for later phases |
|---|---|
| Provider-neutral models + interface | Azure live integration (Phase 7C.2) |
| AWSCloudProvider adapter | Entra ID federation (Phase 7C.2) |
| ProviderRegistry singleton + scope | Client-secret fallback (Phase 7C.2) |
| Capability model | Rackspace APIs (Phase 7C.3) |
| Schema readiness for future providers | AWS IAM changes (none) |
| Credential-strategy enum | `All Clouds` cross-provider aggregate |
| `/api/cloud/*` provider-neutral routes | (post-7C.3) |
| `ProviderContext` + provider-scoped cache keys | |
| Evidence provider + account_id attribution | |
| Documentation + comprehensive tests | |

## 6. Out-of-scope actions explicitly avoided

* No live Azure integration.
* No Entra ID federation / client-secret wiring.
* No AWS IAM modifications.
* No Rackspace API integration.
* No merge to `main`.
* No Phase 7C completion tag.

## 7. Commit hash and push

```
phase-7c-multi-cloud
  db93664fea4b5e8e51ddfd1ce1f013f754aad76a
  Phase 7C.1 — provider abstraction (AWS)

  35 files changed, 3,649 insertions(+), 86 deletions(-)

Pushed to origin/phase-7c-multi-cloud
  e0357ae..db93664  phase-7c-multi-cloud -> phase-7c-multi-cloud
```

STOP after Phase 7C.1.
