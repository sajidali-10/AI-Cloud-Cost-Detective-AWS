# Phase 7C.2D — Azure Advisor Cost Recommendations

This document describes the Azure Advisor Cost recommendations
integration shipped in Phase 7C.2D.  It covers the architecture,
KQL query design, normalised recommendation model, active
recommendation semantics, savings semantics, currency handling,
pagination, subscription behaviour, permissions, security, and
live validation results.

## Architecture

```
Frontend
   ↓
GET /api/cloud/recommendations?provider=AZURE[&account_id={all|sub:<guid>}]
   ↓
app.api.cloud_recommendations (canonical route)
   ↓
ProviderRegistry → AzureCloudProvider.get_recommendations()
   ↓
AzureAdvisorAggregator
   ├─ single subscription: per-row Advisor query
   └─ multi subscription: ONE tenant-mode advisorresources query,
      partitioned by subscriptionId
          ↓
AzureAdvisorClient (httpx, reuses Resource Graph endpoint)
   ↓
Microsoft Resource Graph /providers/Microsoft.ResourceGraph/resources
advisorresources table
```

The AWS legacy routes (`/api/aws/optimization/*`, Phase 7B) are
**untouched**.  The canonical provider-neutral route currently
serves provider=AZURE; AWS callers continue to use the existing
AWS-specific endpoints.

## Why Resource Graph advisorresources

Microsoft documents that Advisor data is exposed through the
``advisorresources`` Resource Graph table.  This phase uses that
surface — it does NOT call the Advisor REST API directly.  The
reasons:

* Resource Graph is the documented unified surface for Advisor
  data, accessible with the same Reader role that Phase 7C.2B/C
  already required.
* The same hardened Resource Graph client (Phase 7C.2C.1) is
  reused — pagination, sanitisation, error mapping, defensive
  loop guards.
* No additional Azure RBAC needed; the existing Reader +
  Cost Management Reader roles are sufficient.

## Authentication reuse

The Advisor client reuses Phase 7C.2A's
``ClientAssertionCredential`` via ``acquire_access_token()``.  No
new credentials.

## KQL query design

```kql
advisorresources
| where type == "microsoft.advisor/recommendations"
| where properties.category == "Cost"
| where properties.recommendationStatus == "Active"
| project subscriptionId, id, name, type, properties
```

Critical contract:

* **No** ``| limit`` / ``| take`` / ``| sample`` operator —
  Microsoft Resource Graph treats those as final and disables
  ``$skipToken`` continuation.  Page size is controlled via
  ``options.$top`` alone (default 1000).
* Filters:
  * ``type == "microsoft.advisor/recommendations"`` — only the
    Advisor recommendation resource type.
  * ``properties.category == "Cost"`` — only Cost recommendations
    (Reliability / Security / Performance / Operational
    Excellence are out of scope).
  * ``properties.recommendationStatus == "Active"`` — only
    currently-actionable recommendations.  Completed /
    Dismissed are intentionally excluded.

## Active recommendation semantics

Microsoft's consolidated lifecycle field
``properties.recommendationStatus`` is used (NOT the internal
``customerState`` / ``platformState`` fields — those are
documented as legacy/internal and would risk double-counting).

The documented ACTIVE values are: ``"Active"``.

## Canonical recommendation model

Each Advisor row is normalised into a provider-neutral
:class:`AzureAdvisorRecommendation` dataclass that maps cleanly
onto :class:`app.services.cloud.models.CloudRecommendation`.

| Resource Graph field | Canonical field |
|----------------------|-----------------|
| `id` (full ARM id) | `recommendation_id` (stable, unique) |
| `properties.subscriptionId` | `subscription_id` |
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

The raw ``properties`` and ``extendedProperties`` blobs are
**never** echoed to the caller — only the documented sub-fields
above.

## Savings semantics

* ``extendedProperties.annualSavingsAmount`` is the documented
  annual savings field.  We never annualise a monthly amount
  ourselves.
* When ``annualSavingsAmount`` is absent or non-numeric,
  ``annual_savings`` is ``None``.  The recommendation is STILL
  returned with ``recommendations_without_savings += 1``.
* When only ``savingsAmount`` (monthly) is present, the
  recommendation is returned with ``annual_savings=None``.
  Per spec, we do NOT multiply by 12 unless Microsoft
  explicitly documents ``savingsAmount`` as monthly for that
  specific recommendation type.
* Zero (``0``) is treated as a valid Decimal value, NOT as
  missing.

## Currency handling

* One currency → ``savings_status="OK"``, ``potential_annual_savings``
  is the summed Decimal in that currency, ``savings_by_currency``
  carries the single-currency totals.
* Multiple currencies → ``savings_status="MIXED_CURRENCY"``,
  ``potential_annual_savings=None``, ``savings_by_currency``
  carries the per-currency totals.  **No FX conversion is ever
  invented.**
* No recommendations with savings → ``savings_status="EMPTY"``.

The route serialises ``savings_by_currency`` as a list of
``{currency, amount}`` dicts so the front-end renders per-currency
totals when MIXED_CURRENCY is reported.

## Pagination

Reuses the Phase 7C.2C.1 contract: ``options.$top`` +
``options.$skipToken`` + ``MAX_PAGES = 50`` loop guard +
repeated-cursor detection + truncated-without-token detection +
schema-drift detection.

Identical query + ``$top`` + ``subscriptions`` across every
page; only ``$skipToken`` mutates.  Within-page dedup by
``composite_id`` (= ``AZURE|<id>``).

## Multi-subscription behaviour

Two paths:

### Single subscription

The aggregator fans out across the selected subscription via
``AzureAdvisorClient.query_active_cost_recommendations(
subscription_ids=[<sub>])``.

### All Azure Subscriptions

The aggregator issues **one** tenant-mode Resource Graph query
(empty ``subscriptions`` list — Resource Graph fans out across
every subscription the configured identity is authorised to see)
and partitions the rows by ``subscriptionId``.

## Error handling

Every non-2xx response is folded into a sanitised
``AzureAdvisorErrorCategory``:

| HTTP | Category | HTTP response |
|------|----------|---------------|
| 401 | `AUTH_FAILED` | 401 |
| 403 | `ACCESS_DENIED` | 403 |
| 404 | `NOT_FOUND` | 404 |
| 429 | `THROTTLED` | 429 + `Retry-After` |
| 5xx | `UPSTREAM_ERROR` | 502 |
| network | `NETWORK_ERROR` | 502 |
| bad JSON | `INVALID_RESPONSE` | 502 |
| truncated + no cursor + count < total | `PAGINATION_INCOMPLETE` | 502 |

## Security

* No ``Authorization: Bearer …`` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs truncated to first 8 hex chars via
  ``safe_subscription_label`` before logging.
* KQL hardcoded; the front-end cannot inject KQL.
* No raw Azure ``properties`` / ``extendedProperties`` blobs
  ever reach the canonical model.
* **READ-ONLY** — no Advisor recommendation-generation, no
  lifecycle updates, no resource mutations.

## Permission validation

The Phase 7C.2B/C-validated identity (STS federated to Entra) was
probed against Advisor BEFORE any code was written.  The probe
returned:

```
POST /providers/Microsoft.ResourceGraph/resources
   advisorresources | where category == "Cost"
HTTP 200
```

The Reader + Cost Management Reader roles inherited from Phase
7C.2B/C are sufficient for Advisor read access.  No new RBAC was
assigned.

## Live validation results

Phase 7C.2D was validated against the live backend with the
real Azure subscriptions.

### Azure → MCPP

```
status: SUCCESS
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
savings_by_currency: []
subscription_errors: []
```

Zero ACTIVE Cost recommendations.  Legitimate empty state.

### Azure → Sponsorship

```
status: SUCCESS
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
savings_by_currency: []
subscription_errors: []
```

Zero ACTIVE Cost recommendations.  Legitimate empty state.

### Azure → All Azure Subscriptions

```
status: SUCCESS
total_recommendations: 0
recommendations_with_savings: 0
potential_annual_savings: null
savings_by_currency: []
subscription_errors: []
```

Zero ACTIVE Cost recommendations across both subscriptions.  Legitimate
empty state.

### Advisor field inspection (audited during Step 0)

During the Step 0 permission probe, the only existing Cost
recommendation observed was a Sponsorship VM with:

* `recommendationStatus`: **Completed** (intentionally excluded
  by the ``Active`` filter)
* `customerState`: New
* `platformState`: Resolved

The completed recommendation's documented Advisor fields:

| Field | Live value |
|-------|------------|
| `properties.category` | "Cost" |
| `properties.impact` | "High" |
| `properties.recommendationTypeId` | "e10b1381-5f0a-47ff-8c7b-37bd13d7c974" |
| `properties.impactedField` | "Microsoft.Compute/virtualMachines" |
| `properties.shortDescription.problem` | "Right-size or shutdown underutilized virtual machines" |
| `properties.shortDescription.solution` | "Right-size or shutdown underutilized virtual machines" |
| `properties.lastUpdated` | "2026-09-27T00:21:48.4467372Z" |
| `properties.createdTime` | "2026-09-22T00:22:05.9987548Z" |
| `properties.resourceMetadata.resourceId` | "/subscriptions/9353d858-.../resourcegroups/hiplinkqa/providers/microsoft.compute/virtualmachines/windows2025-hiplink-qa-9-11-2026" |
| `extendedProperties.annualSavingsAmount` | "1488" |
| `extendedProperties.savingsCurrency` | "USD" |
| `extendedProperties.savingsAmount` | "124" (monthly — NOT used as annual) |
| `extendedProperties.term` | "P1Y" |
| `extendedProperties.lookbackPeriod` | "P30D" |
| `extendedProperties.recommendationSubCategory` | "UsageOptimization" |
| `extendedProperties.recommendationType` | "Shutdown" |

This recommendation is correctly excluded by the ACTIVE filter
because its `recommendationStatus="Completed"` rather than
`"Active"`.  The aggregator would have surfaced it as a
`recommendation_without_savings` row IF the filter allowed
Completed recommendations; the spec forbids this.

## Zero-recommendation UI behaviour

The AzureAdvisorView renders a legitimate empty state when
``total_recommendations == 0``:

* KPI tiles show ``0`` for all four metrics.
* The breakdowns render with empty data sets.
* The recommendation table renders with no rows.
* No error banner — ``status="SUCCESS"`` + ``total_recommendations=0``
  is treated as a valid empty state, not as an error.

The MCPP / Sponsorship / All Azure Subscriptions views all show
this empty state during live validation.

## Known limitations

* If Azure returns only ``savingsAmount`` (monthly) without
  ``annualSavingsAmount``, the recommendation is shown with
  ``annual_savings=None``.  We do NOT annualise.  When
  Microsoft's documentation explicitly establishes ``savingsAmount``
  as monthly for a specific recommendation type, a future phase
  can compute the annualised value.
* Subscriptions with > 50,000 recommendations would hit
  ``MAX_PAGES=50``.  Not realistic for any Phase 7C.2D target.

## What Phase 7C.2D does NOT ship

* Advisor recommendation-generation (explicitly out of scope).
* Recommendation lifecycle updates (dismiss / postpone / mark
  completed).
* VM resize / shutdown / delete operations.
* Reservation / savings plan purchases.
* AI / LLM-generated recommendations.
* Reliability / Security / Performance / Operational Excellence
  recommendations (only Cost is in scope).
* Rackspace provider — not part of 7C.2D.
