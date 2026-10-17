# Phase 7D.1 — Canonical AWS Recommendations Integration

Provider-neutral AWS recommendations behind the single
`/api/cloud/recommendations` endpoint, without breaking the legacy
AWS optimization surface and without any UI redesign.

## 1. The old asymmetry

Before Phase 7D.1 the recommendation surface was split by provider:

| Provider | Endpoint | Envelope |
| --- | --- | --- |
| Azure | `/api/cloud/recommendations?provider=AZURE` | canonical `CloudRecommendationListResponse` |
| AWS | `/api/aws/optimization/{recommendations,summary,capabilities}` | Phase 3 `RecommendationsResponse` / `SummaryResponse` |

The canonical route hard-coded Azure. Any `provider=AWS` request
returned HTTP 400 `UnsupportedProviderForCanonicalRoute`. The
`AWSCloudProvider.get_recommendations()` adapter method existed but
was unreachable from the canonical route and returned only a flat
list (no status, no rollup, no currency grouping).

Gap summary:

* AWS was not a first-class canonical recommendation provider.
* The canonical envelope was Azure-shaped (`by_subscription`,
  `subscription_errors`, annual-only savings) with no AWS account
  grouping and no monthly/annual distinction.
* The canonical recommendation route had **no cache-key function**
  isolating user / provider / scope / region / window.
* Selector coercion was implicit (an Azure `sub:` selector sent to
  AWS simply fell through to a confusing 404).

## 2. Canonical architecture (after 7D.1)

```
                 ┌───────────────────────────────┐
 GET /api/cloud/recommendations?provider=AWS|AZURE
                 └───────────────┬───────────────┘
                                 │  _resolve_rows()  (selection + coercion)
                 ┌───────────────┴───────────────┐
                 │                               │
        provider=AWS                    provider=AZURE
                 │                               │
   services/cloud/aws/                services/cloud/azure/
   recommendation_aggregator          advisor_aggregator
        │  (reuses Phase 7B)               │
        ▼                                  ▼
   aws/optimization_aggregator      azure/advisor (Resource Graph)
   (Compute Optimizer + COH +       (Azure Advisor Cost)
    deterministic engine)
        │                                  │
        └────────────┬─────────────────────┘
                     ▼
        CloudRecommendationListResponse
        (one provider-neutral envelope)
```

Legacy `/api/aws/optimization/*` is **preserved unchanged** for
backward compatibility. It continues to serve the Phase 3
`RecommendationsResponse` envelope. Both surface the same underlying
recommendations for the same scope.

## 3. AWS adapter mapping

`backend/app/services/cloud/aws/recommendation_aggregator.py` is the
AWS analogue of `azure/advisor_aggregator.py`. It **reuses, never
re-implements,** the Phase 7B multi-account aggregation
(`app.services.aws.optimization_aggregator.aggregate_recommendations_for_context`):
bounded concurrency, account ContextVar routing, source-precedence
dedup-merge, and per-account partial-failure isolation.

`recommendation_to_cloud(Recommendation) -> CloudRecommendation`
maps each Phase 7B recommendation onto the canonical model:

| Canonical field | AWS source |
| --- | --- |
| `provider` | `AWS` |
| `account_id` | `Recommendation.account_id` (registry account id) |
| `region` | `Recommendation.region` |
| `recommendation_id` | `Recommendation.recommendation_id` |
| `resource_id` | `Recommendation.resource_id` |
| `resource_type` / `service` | `Recommendation.resource_type` |
| `action` | `Recommendation.action` (e.g. `RIGHTSIZE`) |
| `title` | `Recommendation.title` |
| `category` | `Recommendation.action` (coarse grouping) |
| `impact` | `Recommendation.confidence` (`HIGH`/`MEDIUM`/`LOW`) |
| `description` | `Recommendation.finding` |
| `estimated_savings` | `Recommendation.estimated_monthly_savings` (**monthly**) |
| `currency` | `Recommendation.currency` (default `USD`) |
| `source` | `Recommendation.primary_source` — `AWS_COST_OPTIMIZATION_HUB` / `AWS_COMPUTE_OPTIMIZER` / `UNKNOWN` (deterministic) |
| `extensions` | `confidence`, `data_quality`, `savings_percentage`, `reason_codes`, `finding`, `sources`, `savings_source`, `restart_needed`, `rollback_possible`, `account_display_name`, `savings_period` |

No provider-native field is lost: everything that cannot be cleanly
normalised rides in `extensions`.

## 4. Normalized recommendation model

Phase 7D.1 promotes three previously extension-only fields onto the
shared contract so both providers expose the same columns without
provider-specific branching (additive; defaults preserve existing
callers):

* `category` — AWS action slug / Azure Advisor category (`Cost`).
* `impact` — AWS confidence bucket / Azure Advisor impact.
* `description` — AWS `finding` / Azure Advisor problem text.

The canonical summary envelope also gains provider-neutral fields:

* `potential_savings` + `savings_period` (`MONTHLY` | `ANNUAL`) —
  the generic savings pair. AWS populates a **monthly** figure and
  leaves `potential_annual_savings` null; Azure keeps both its legacy
  annual field and the generic pair.
* `by_account` / `account_errors` — the provider-neutral rename of
  Azure's `by_subscription` / `subscription_errors`.
* `by_source` / `by_action` — generic breakdowns.

All legacy fields (`by_subscription`, `subscription_errors`,
`potential_annual_savings`) are retained unchanged for backward
compatibility.

## 5. Legacy compatibility

* `/api/aws/optimization/capabilities`, `/recommendations`,
  `/summary` are unchanged and still return the Phase 3 envelope.
* `AWSCloudProvider.get_recommendations()` still satisfies the
  `CloudProvider` contract (returns `List[CloudRecommendation]`); it
  now delegates to the shared mapping so there is exactly one AWS →
  canonical translation in the codebase.

Canonical vs legacy:

| | Legacy | Canonical |
| --- | --- | --- |
| Path | `/api/aws/optimization/recommendations` | `/api/cloud/recommendations?provider=AWS` |
| Envelope | Phase 3 `RecommendationsResponse` | `CloudRecommendationListResponse` |
| Savings period | monthly | monthly (`savings_period=MONTHLY`) |
| Audiences | existing AWS consumers | provider-neutral UI / AI |

## 6. Aggregation semantics (All AWS Accounts)

`aggregate_aws_recommendations_for_accounts(rows, region, days)`
fans out across enabled accounts and returns
`(flat CloudRecommendations, rollup, account_errors)`. Preserved:

* **account attribution** — `rollup.by_account`.
* **source attribution** — `rollup.by_source`.
* **deduplication** — Phase 7B `(resource_id, action, region)`
  merge with source precedence.
* **partial failures** — one account failing yields status
  `PARTIAL_SUCCESS` and never erases another account's
  recommendations.
* **statuses** — `SUCCESS` / `PARTIAL_SUCCESS` / `NO_DATA` / `ERROR`
  (NO_DATA when a clean run returns zero recommendations).

Fixed in passing: `optimization_aggregator._merge_recommendations`
previously **crashed** (`ValidationError`) on any multi-account
collision because it assigned to a frozen Pydantic model. It now
re-issues via `model_copy(update=...)`. This latent Phase 7B bug was
uncovered by the canonical All-AWS path; without the fix, All-AWS
dedup would 500.

## 7. Currency handling

AWS reports **monthly** savings; Azure reports **annual** savings.
Savings are grouped by currency and never summed across different
currencies. Mixed currency surfaces an explicit
`savings_status="MIXED_CURRENCY"`, `potential_savings=null`, and
populates `savings_by_currency` per currency. No FX conversion is
performed in this phase. `savings_period` disambiguates the window.

## 8. Cache isolation

`_build_recommendation_cache_key(user_id, provider, scope, region,
days)` folds every response-affecting dimension into a SHA-256 key.
AWS and Azure keys can never collide, and `All AWS Accounts` can
never satisfy `All Azure Subscriptions`. The route is stateless (it
does not persist to `cost_cache`) — the key is a stable correlation
id and is asserted directly by regression tests.

The frontend key builder `buildCloudRecommendationsCacheKey` accepts
optional `region`/`days`; when both are omitted it returns the exact
pre-7D.1 string (`azure:cloud:recommendations:all`), so existing
Azure callers/tests are unaffected.

## 9. Selector coercion

An Azure-style `sub:<guid>` selector sent to `provider=AWS`, or an
AWS-style `acct:<id>` selector sent to `provider=AZURE`, is rejected
with HTTP 400 `ProviderSelectorMismatch` before any provider call.
A selector minted for one provider is never interpreted by another.
`sub:<guid>` remains the Azure identity; AWS accounts use
`acct:<account_id>` (provider-native account id, matching the
`cloud_costs` / `cloud_resources` scope convention).

## 10. Error handling

Per-account AWS failures are produced by the Phase 7B aggregator
already reduced to `{account_id, display_name, code, message}` with
fixed operator-readable messages (e.g. "AWS denied access for this
account"). They surface through `account_errors`. Top-level upstream
failures return a sanitised `UPSTREAM_ERROR` envelope. No AWS
credential, Azure token, `Authorization` header, or raw provider
exception payload is ever emitted — asserted by tests.

## 11. Changed files

Backend:

* `backend/app/services/cloud/aws/recommendation_aggregator.py` (new)
* `backend/app/services/cloud/aws/adapter.py`
* `backend/app/services/cloud/azure/advisor_aggregator.py`
* `backend/app/services/cloud/models.py`
* `backend/app/api/cloud_recommendations.py`
* `backend/app/schemas/cloud_recommendation.py`
* `backend/app/services/aws/optimization_aggregator.py` (frozen-model fix)
* `backend/tests/test_phase7d1_canonical_aws_recommendations.py` (new)

Frontend:

* `frontend/src/lib/finops/cloudRecommendations.ts`
* `frontend/src/tests/phase7d1-canonical-aws.test.ts` (new)

Scripts:

* `scripts/phase7d1_live_validation.sh` (new)
* `scripts/phase7c2b_live_validation.sh` (stale selectable path corrected)