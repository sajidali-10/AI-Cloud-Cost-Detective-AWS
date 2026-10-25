# Phase 9C.2 — Rackspace Canonical Recommendations

Phase 9C.2 exposes the Phase 9C.1 deterministic Rackspace optimization
engine through the project's existing canonical recommendation
architecture. No new route, no new schema, no new rules — only the
integration of the completed engine into the provider-neutral
recommendation surface every provider already shares.

```text
GET /api/cloud/recommendations?provider=RACKSPACE
```

## Architecture

The preferred flow from the phase plan, realised exactly:

```text
/api/cloud/recommendations?provider=RACKSPACE
        ↓
canonical provider adapter (RackspaceCloudProvider, Phase 9B.1)
        ↓
Phase 9B.2 canonical resources (aggregate_rackspace_resources)
Phase 9B.1 canonical cost context (fetch_cost_snapshot — context only)
        ↓
Phase 9C.1 deterministic optimization engine (evaluate_rackspace_optimization)
        ↓
canonical recommendation envelope (CloudRecommendationListResponse)
```

New module:
`backend/app/services/cloud/rackspace/recommendations.py` — the
translation-only aggregator (the Rackspace analog of the AWS
`recommendation_aggregator.py`):

* `finding_to_cloud_recommendation()` — maps a `RackspaceFinding`
  onto the shared `CloudRecommendation` shape.
* `build_rackspace_recommendation_summary()` — folds the engine
  result into a canonical rollup (`RackspaceRecommendationRollup`).
* `aggregate_rackspace_recommendations_for_account()` — the public
  entry point the route and the adapter both call:
  resources → cost context → engine → mapping → rollup.

Modified files:

* `backend/app/api/cloud_recommendations.py` —
  * RACKSPACE branch in the canonical route (same envelope, same
    auth/RBAC as AWS/Azure — the route was already authenticated);
  * `_build_rackspace_envelope()` — mirrors `_build_aws_envelope()`;
  * `_rackspace_error_response()` — the same category→HTTP vocabulary
    the canonical resources route already uses;
  * `_resolve_rows()` — Rackspace scopes accounts the AWS way
    (`acct:<tenant id>`), so Rackspace accepts `acct:` selectors and
    rejects Azure `sub:` selectors (the same guard AWS has).
* `backend/app/services/cloud/rackspace/adapter.py` —
  * `OPTIMIZATION` capability flipped from `UNSUPPORTED` to
    `SUPPORTED` (identity-gated: `NOT_CONFIGURED` without credentials);
  * `get_recommendations()` implemented — delegates to the canonical
    aggregator instead of raising `NotImplementedError`.

No rule from `optimization.py` was duplicated or redesigned. The
engine is consumed as-is.

## Provider capability

`/api/cloud/providers` now advertises (via the unchanged discovery
route — the capability surface is derived from the adapters):

```text
AWS:
  costs            SUPPORTED
  resources        SUPPORTED
  recommendations  SUPPORTED      (OPTIMIZATION capability, unchanged)

AZURE:
  costs            SUPPORTED
  resources        SUPPORTED
  recommendations  SUPPORTED      (OPTIMIZATION capability, unchanged)

RACKSPACE:
  costs            SUPPORTED
  resources        SUPPORTED
  recommendations  SUPPORTED      (new in 9C.2 — identity-gated)
```

The `recommendations` row in the matrix is the `OPTIMIZATION`
capability — the canonical name for the recommendation surface.
AWS and Azure capabilities are untouched; only Rackspace changed.

## Route contract

```text
GET /api/cloud/recommendations?provider=RACKSPACE
    ?account_id=all | acct:<cloud tenant id>
    &region=<ignored — multi-region discovery>
    &days=<ignored — no historical lookback>
```

* **Auth** — the existing production JWT + RBAC (ADMIN / ANALYST /
  VIEWER) exactly like AWS and Azure. No anonymous access.
* **Scope** — the same selector semantics the project already uses.
  Rackspace has one registered tenant (bootstrapped in 9B.1), so
  `all` and `acct:<tenant>` both resolve to it. Azure `sub:` selectors
  are rejected with `ProviderSelectorMismatch` (400).
* **`region` / `days`** — accepted for contract parity and ignored,
  the same way Azure ignores `days`: Rackspace discovery enumerates
  every authorized datacenter from the Identity catalog, and the
  engine has no historical lookback.
* **Statuses** — the existing canonical vocabulary only:
  `SUCCESS` (clean run with findings), `NO_DATA` (valid resources +
  zero findings — a *successful empty* result, never an error),
  `PARTIAL_SUCCESS` (at least one resource service failed; valid
  services still evaluated), plus the HTTP error envelopes for total
  failures. No Rackspace-specific status was invented.

## Canonical recommendation mapping

Each internal `RackspaceFinding` maps onto the existing canonical
schema — the global schema was NOT expanded:

| canonical field | source |
| --- | --- |
| `recommendation_id` | `finding_id` (stable, verbatim — never a random UUID) |
| `provider` | `RACKSPACE` |
| `account_id` | finding `account_id` (safe Cloud tenant id) |
| `resource_id` / `resource_type` | preserved verbatim |
| `region` | preserved verbatim |
| `service` | `Cloud Block Storage` / `Cloud Servers` / `Cloud Load Balancers` (the 9B.2 inventory labels) |
| `action` | `REVIEW` (every 9C.1 rule is a review candidate) |
| `title` | finding title |
| `category` | finding category (`STORAGE_REVIEW`, `SERVER_REVIEW`, `LOAD_BALANCER_REVIEW`) |
| `impact` | finding **severity** (`LOW` / `MEDIUM` / `HIGH`) |
| `description` | finding summary (long-form explanation) |
| `estimated_savings` | `estimated_monthly_savings` (`None` when unknown) |
| `currency` | `None` when savings are unknown (no currency claim) |
| `source` | `RACKSPACE_CLOUD_API` (the finding's evidence source) |
| `extensions` | `rule_id`, `resource_name`, `severity`, `confidence`, `recommended_action`, `evidence`, `savings_basis`, `limitations`, `savings_period`, `account_display_name` |

Fields the shared schema has no column for (`confidence`,
`recommended_action`, `evidence`, `savings_basis`, `limitations`,
`resource_name`) are preserved under `extensions` — the established
mechanism for provider-native semantics. The summary-level `source`
is `RACKSPACE_OPTIMIZATION` (mirroring `AWS_OPTIMIZATION`), while
item-level `source` stays the evidence source `RACKSPACE_CLOUD_API`.

Severity AND confidence are both preserved: `impact` carries the
severity (the same provider-native priority bucket AWS/Azure promote),
and the evidence-driven confidence rides in `extensions.confidence`.

## Severity / confidence mapping

| rule | severity | confidence | trigger |
| --- | --- | --- | --- |
| `RS-VOL-001` | `MEDIUM` (`HIGH` when size known and > 100 GB) | `MEDIUM` | `attachment_state == DETACHED` |
| `RS-SRV-001` | `LOW` | `LOW` | server state in `SHUTOFF` / `SUSPENDED` / `PAUSED` / `ERROR` |
| `RS-LB-001` | `LOW` | `HIGH` | load balancer `status == DELETED` |

The mapping adds no severity of its own — the engine's values are
copied verbatim.

## Savings semantics

```text
unknown savings != $0
```

* Every current deterministic finding has
  `estimated_monthly_savings = null` — the Rackspace Billing V2 API
  provides cycle-level totals only, with no reliable per-resource
  price mapping. No GB pricing, server pricing, or bill allocation
  is invented, and the account total is never proportionally
  allocated across resources.
* On the wire: item `estimated_savings` is `null`, item `currency`
  is `null` (no currency is claimed for a figure that does not
  exist), summary `savings_status = "EMPTY"`,
  `recommendations_without_savings = total`, and
  `potential_savings = null`. The canonical response never reports
  `$0 savings`.
* If a future engine finding ever carries an authoritative figure,
  the rollup already supports single-currency totalisation from the
  account billing currency — with no FX rate ever invented.

## Determinism

Canonical mapping preserves the Phase 9C.1 determinism contract:

* same canonical input → same recommendation IDs, same count, same
  ordering (severity desc → rule → size desc → region → resource id),
  same severity, same confidence, same evidence, same savings nullability;
* the engine's stable `finding_id`
  (`SHA-256(provider|account|rule|region|resource_type|resource_id)`
  → `rs-det-<rule>-<hash12>`) becomes `recommendation_id` verbatim —
  no random UUIDs;
* the evaluation timestamp stays the engine's deterministic sentinel
  (it is not exposed in the canonical envelope at all), so repeated
  invocations are byte-identical.

The live validator runs the route twice and compares identity and
ordering.

## Partial-data semantics

When Phase 9B.2 resource retrieval is `PARTIAL_SUCCESS`
(e.g. servers valid, volumes unavailable):

* the engine evaluates ONLY the valid available resources;
* only evidence-supported findings are generated — a missing volume
  service yields zero volume findings, nothing is fabricated;
* per-service failures are preserved through the canonical
  `account_errors` slot (`account_id` = safe Cloud tenant,
  `display_name` = the Rackspace service that failed, `code` =
  the canonical error category) and the overall status becomes
  `PARTIAL_SUCCESS`;
* a TOTAL resource failure raises `RackspaceResourceError`, which the
  route maps onto the precise HTTP error envelope — an upstream
  resource error is never converted into a zero-recommendation
  success.

The billing cost context is best-effort: a Billing API failure
degrades to `cost_context=None` (logged, not surfaced as an error)
because the context is informational only — it never feeds a finding.

## Summary / breakdown reconciliation

The response summary reconciles:

* `total_recommendations` == `len(recommendations)`;
* `by_impact` (severity) / `by_resource_type` / `by_action` /
  `by_source` / `by_account` all sum to the total;
* `recommendations_with_savings` (0) + `recommendations_without_savings`
  (all) == total;
* `potential_savings` is `null` with `savings_status = "EMPTY"` —
  the canonical equivalent of "unavailable" — never `$0`.

Rule-level breakdowns (category/rule) are derivable per item from
`category` + `extensions.rule_id`; the shared summary schema has no
`by_rule`/`by_category` column and none was added (the AWS envelope
also leaves `by_type`/`by_subcategory` empty).

## Security

* The route keeps the existing production auth/RBAC (JWT, role
  checks), CORS, rate limits, proxy trust — nothing was weakened and
  no new surface was added.
* No secrets appear in recommendation payloads. The canonical payload
  carries only: the safe Cloud tenant id, resource ids/types/regions/
  states, engine evidence, and sanitised error categories. Never
  exposed: `RACKSPACE_API_KEY`, `X-Auth-Token`,
  `RACKSPACE_BILLING_ACCOUNT` (RAN), or raw Identity tokens.
* Error envelopes use the stable category vocabulary
  (`NOT_CONFIGURED`, `AUTH_FAILED`, `ACCESS_DENIED`, `NOT_FOUND`,
  `THROTTLED`, `UPSTREAM_ERROR`, `UNSUPPORTED`) — never raw upstream
  payloads.

## Zero-write guarantee

Canonical recommendation generation is analysis-only:

```text
Rackspace product write requests = 0
```

The engine is deterministic and network-free; the canonical inputs are
read-only `GET` aggregations. The only write-like verb in the entire
Rackspace integration remains the Identity authentication `POST` owned
by the Phase 9A infrastructure. The shared `RackspaceHttpClient`
exposes no `put`/`patch`/`delete` methods at all. The live validator
proves zero product writes with a transport spy over the exact route
code path.

## Cache decision

**No new cache.** The canonical recommendation route for AWS and
Azure is stateless — it computes fresh per request (the route's
"cache key" is only a correlation id, never persisted). Rackspace
follows the same pattern:

* resources are computed on demand through the same Phase 9B.2
  aggregation the `/api/cloud/resources` route uses;
* the billing snapshot is fetched through the Phase 9B.1 canonical
  cost layer (`fetch_cost_snapshot`) as fail-soft context — the
  heavyweight FRESH/STALE-LKG cost-envelope cache belongs to the
  costs route and would be a second, unnecessary cache architecture
  for a context-only input;
* no parallel cache architecture was invented.

## Live validation

`scripts/phase9c2_rackspace_canonical_recommendation_validation.sh`
exercises the actual production endpoint
(`/api/cloud/recommendations?provider=RACKSPACE` through nginx with a
JWT) plus an in-container run of the real route handler with a
transport spy. It validates provider/status/source, count
reconciliation against live canonical resource evidence, ID
uniqueness/stability, determinism across two runs, resource reference
existence, severity/confidence validity, savings semantics
(unknown ≠ 0), ACTIVE-server safety, attached-volume safety,
detached-inventory correspondence, zero product writes, and zero
secret leakage. Exit codes: `0` success, `1` validation failure,
`2` precondition/configuration failure.

## AWS / Azure compatibility

* The shared schema was not changed — the canonical envelope is
  constructed from the same `CloudRecommendationItem` /
  `CloudRecommendationSummaryEnvelope` classes.
* The AWS and Azure route branches are untouched; the only shared
  code change is selector handling, which now routes Rackspace
  through the AWS-style `acct:` path (Azure semantics unchanged:
  `sub:` selectors still rejected by AWS and now by Rackspace too).
* AWS/Azure behaviours were not altered to simplify Rackspace; the
  regression suites (Phase 7D.1 canonical tests, provider discovery
  tests) pass unchanged except the two 9B.1 adapter capability
  assertions that explicitly pinned the pre-9C.2 state
  (`OPTIMIZATION == UNSUPPORTED`, `get_recommendations` raising) —
  updated to pin the new intended contract.

## FinOps context compatibility

No AI integration was started — `FinOpsAIProvider` still accepts
only `AWS` / `AZURE`, LiteLLM behaviour, prompts, and AI generation
are untouched. The smallest necessary integration is already in
place: the generic FinOps context builder consumes recommendations
through `adapter.get_recommendations(...)` and the
`CloudRecommendation` shape (unknown savings render as `null`), so
**Phase 9D/9E can enable RACKSPACE by adding the enum member — the
canonical recommendation route and the context seam need no further
work.**

## Limitations

* `ACTIVE` server + flavor size is **not** rightsizing evidence —
  the metrics capability probe stays `DEFERRED`, so ACTIVE servers
  never produce rightsizing/idle/downsizing/waste recommendations.
* All savings are unknown (`null`) — no per-resource pricing source
  exists in the Rackspace Billing API surface used here.
* Detached-now does not imply detached-for-N-days; `created_at`
  reflects resource age, not detachment duration.
* `RS-LB-001` only triggers on `DELETED` status — descriptive LB
  configuration alone is not idle evidence.
* Canonical recommendations are **advisory / read-only** — they
  recommend review by an owner, never deletion, stop, resize, or
  any product mutation.

## Phase 9D handoff

* `/api/cloud/recommendations?provider=RACKSPACE` serves the complete
  canonical envelope with stable IDs and deterministic ordering — the
  multi-cloud dashboard can consume it without provider branching.
* `/api/cloud/providers` advertises the full RACKSPACE capability
  matrix (costs / resources / recommendations).
* The FinOps/AI layer can consume `adapter.get_recommendations()`
  directly once `FinOpsAIProvider` grows a `RACKSPACE` member
  (Phase 9D/9E decision).
* Unknown savings are represented as `null` end-to-end; any UI must
  render "unknown" rather than `$0`.
