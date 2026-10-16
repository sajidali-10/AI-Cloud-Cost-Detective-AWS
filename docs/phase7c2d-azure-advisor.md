# Phase 7C.2D — Azure Advisor Cost Recommendations (Hardened in 7C.2D.1)

This document describes the Azure Advisor Cost recommendations
integration shipped in Phase 7C.2D and corrected in Phase 7C.2D.1.

## Portal concept vs Resource Graph value — critical distinction

The Azure Portal surfaces "Active" recommendations as a
user-facing concept.  Internally, the Resource Graph
``properties.recommendationStatus`` field uses a separate set
of lifecycle values:

| Portal label | Resource Graph `recommendationStatus` value |
|---------------|------------------------------------------------|
| **Active** (actionable) | **`New`** |
| Active / in progress | `InProgress` |
| Completed | `Completed` |
| Postponed | `Postponed` |
| Dismissed | `Dismissed` |

Phase 7C.2D.1 corrects the KQL filter from the Portal-flavoured
string `"Active"` to the documented Resource Graph value `"New"`.
The literal string `"Active"` is **NOT** a documented
``recommendationStatus`` value — it is the Portal's label that
encompasses `New` (and sometimes `InProgress`).

**Do not conflate the two concepts.**  The Portal's "Active" is
not a Resource Graph enum value; Microsoft's documented query
filters on `recommendationStatus == "New"`.

## Final KQL

```kql
advisorresources
| where type == "microsoft.advisor/recommendations"
| where properties.category == "Cost"
| where properties.recommendationStatus == "New"
| project subscriptionId, id, name, type, properties
```

Critical contract:

* **No** `| limit` / `| take` / `| sample` — disables `$skipToken`.
* Page size via `options.$top` (default 1000).
* Filters: Cost + New lifecycle stage.
* Pagination: `$skipToken`, MAX_PAGES=50, repeated-cursor +
  truncated-without-cursor + schema-drift protections.

## Why `InProgress` is excluded

`InProgress` is a documented `recommendationStatus` value but
this project deliberately excludes it.  Rationale:

* The Cost dashboard treats Advisor as a **"newly generated,
  action required"** snapshot.
* Remediation tracking (which would correspond to `InProgress`)
  belongs to a separate workflow not in Phase 7C.2D scope.
* Including `InProgress` would surface the same recommendation
  twice in the UI: once at `New` (when first generated) and
  again at `InProgress` (after remediation begins).

## Excluded lifecycle values

| Value | Why excluded |
|-------|--------------|
| `Completed` | No action remaining |
| `Postponed` | User deferred the recommendation |
| `Dismissed` | User explicitly dismissed |
| `InProgress` | Remediation already underway; out of dashboard scope |

## Category / metadata mapping

The `category` field on `microsoft.advisor/recommendations` rows
is reliably present (`Cost`, `OperationalExcellence`,
`HighAvailability`, etc.).  The Phase 7C.2D query filters on
`properties.category == "Cost"` to scope the dashboard to Cost
recommendations only.  The remaining categories are out of scope.

Per-row field mapping (the canonical `AzureAdvisorRecommendation`
dataclass):

| Resource Graph field | Canonical field |
|----------------------|-----------------|
| `subscriptionId` (top-level) | `subscription_id` |
| `id` (full ARM id) | `recommendation_id` |
| `properties.resourceMetadata.resourceId` | `resource_id` |
| `properties.impactedField` | `resource_type` |
| `properties.recommendationTypeId` | `recommendation_type_id` |
| `extendedProperties.recommendationSubCategory` | `subcategory` |
| `properties.impact` | `impact` (High / Medium / Low) |
| `properties.shortDescription.problem` | `title` |
| `properties.shortDescription.solution` | `solution` |
| `extendedProperties.annualSavingsAmount` | `annual_savings` |
| `extendedProperties.savingsCurrency` | `savings_currency` |
| `extendedProperties.term` | `term` |
| `extendedProperties.lookbackPeriod` | `lookback_period` |
| `properties.lastUpdated` | `last_updated` |
| `properties.recommendationStatus` | `recommendation_status` |

The raw `properties` and `extendedProperties` blobs are never
echoed to the caller — only the documented sub-fields above.

## Configuration / exclusion semantics

Microsoft documents that the Portal's "Active cost recommendations"
view accounts for the subscription-level Advisor configuration
(`/providers/Microsoft.Advisor/configurations`) — which scopes
recommendations by Cloud, Subscription, or Resource Group
exclusion, and applies recommendation-type-specific thresholds
(``lowCpuThreshold`` for the "right-size VM" family).

Phase 7C.2D does **NOT** reproduce that exclusion layer.  Our
query surfaces every `New + Cost` row from the advisorresources
table.  Rationale:

* Reproducing the configuration join requires a second Resource
  Graph query against `/providers/Microsoft.Advisor/configurations`
  per subscription, plus a per-recommendation-type rule evaluation
  — substantial scope creep for Phase 7C.2D.
* The current Advisor configuration on both subscriptions is the
  default (no exclusions); the dashboard would not change.
* Phase 7C.2D's design choice is to expose the raw Advisor
  population and let the operator act on the dashboard — the same
  shape the existing Phase 7B AWS optimization page uses.

When Microsoft's exclusion logic is required (future phase), the
configuration join is a single additional Resource Graph query
that filters the canonical list by exclusion predicate before the
aggregator runs.

## Savings semantics

* `extendedProperties.annualSavingsAmount` is the documented
  annual savings field.  Used directly.
* `savingsAmount` (monthly) is NOT annualised.
* Missing `annualSavingsAmount` → `annual_savings=None`;
  recommendation is still retained.
* Zero (`0`) is a valid Decimal value, distinct from missing.

## Currency handling

* One currency → `savings_status="OK"`, summed total.
* Multiple currencies → `savings_status="MIXED_CURRENCY"`,
  `potential_annual_savings=None`, per-currency totals preserved.
* No recommendations with savings → `savings_status="EMPTY"`.
* **No FX conversion is ever invented.**

## Security

* No `Authorization: Bearer …` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs truncated to first 8 hex chars.
* KQL hardcoded; the front-end cannot inject KQL.
* No raw Azure `properties` / `extendedProperties` blobs ever
  reach the canonical model.
* **READ-ONLY**: no Advisor generation, no lifecycle updates,
  no resource mutations.

## Permission validation

The Phase 7C.2B/C-validated identity (STS federated to Entra)
has `Microsoft.ResourceGraph/resources/read` via the existing
Reader + Cost Management Reader roles.  No new RBAC was
assigned in Phase 7C.2D or Phase 7C.2D.1.

## Live validation — Step 1 distinct statuses observed

The Step 1 unfiltered inspection across both subscriptions
returned:

| Distinct `recommendationStatus` | Tenant count |
|----------------------------------|--------------|
| `Completed` | 1 (Sponsorship VM) |
| `New` (only on OperationalExcellence / HighAvailability) | 20 (MCPP) |

`Cost + New` = **0** rows across both subscriptions and tenant-wide.
The previous Phase 7C.2D zero result was for the right reason
after Phase 7C.2D.1 — there is genuinely no `New + Cost`
recommendation across the configured Azure identity.

## Live validation — corrected active=New dashboard

| Subscription | total_recommendations | status |
|--------------|----------------------:|--------|
| MCPP | 0 | SUCCESS |
| Sponsorship | 0 | SUCCESS |
| All Azure | 0 | SUCCESS |

All three return SUCCESS with `total_recommendations=0`,
`savings_status=EMPTY`, no errors.  The UI renders a legitimate
empty state — not an error, not mock recommendations.

The previously observed Sponsorship VM Cost recommendation
(`annualSavingsAmount=1488 USD`, `term=P1Y`,
`lookbackPeriod=P30D`, `recommendationSubCategory=UsageOptimization`)
remains correctly excluded because its `recommendationStatus`
is `Completed`, not `New`.

## What Phase 7C.2D / 7C.2D.1 does NOT ship

* Advisor recommendation-generation (out of scope).
* Recommendation lifecycle updates (dismiss / postpone / mark
  completed).
* VM resize / shutdown / delete operations.
* Reservation / savings plan purchases.
* AI / LLM-generated recommendations.
* Reliability / Security / Performance / Operational Excellence
  recommendations (only Cost is in scope).
* Subscription-level Advisor configuration exclusion join
  (future enhancement; documented above).
* Rackspace provider — not part of 7C.2D.
