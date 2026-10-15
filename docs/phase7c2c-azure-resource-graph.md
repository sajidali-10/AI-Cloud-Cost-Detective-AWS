# Phase 7C.2C — Azure Resource Graph Integration (Hardened in 7C.2C.1)

This document describes the Azure Resource Graph inventory
integration shipped in Phase 7C.2C and hardened in Phase 7C.2C.1.
It covers the architecture, KQL query shape, **corrected**
pagination contract, normalized resource model, multi-subscription
behavior, error handling, security, and live validation results.

## Architecture

```
Frontend
   ↓
GET /api/cloud/resources?provider=AZURE[&account_id={all|sub:<guid>}]
   ↓
app.api.cloud_resources (canonical route)
   ↓
ProviderRegistry → AzureCloudProvider
   ↓
AzureResourceAggregator
   ├─ single subscription: fan-out per-row
   └─ multi subscription:  ONE tenant-mode query, partition by subscriptionId
          ↓
AzureResourceGraphClient (httpx)
   ↓
Microsoft Resource Graph REST API
```

The AWS legacy routes (`/api/aws/resources`, `/api/aws/resources/all`) are
**untouched** by this phase.  The canonical provider-neutral route
delegates to the AWS adapter for ``provider='AWS'`` so the same
normalized shape is returned, but every AWS byte-for-byte behaviour
is preserved.

## Resource Graph dependency

No new SDK package was added.  Resource Graph is a REST endpoint
at:

```
POST https://management.azure.com/providers/Microsoft.ResourceGraph/
     resources?api-version=2024-04-01
```

We use ``httpx`` (already in the dependency graph for the AI client
and the Azure Cost Management client) with the same sanitisation
contract as Phase 7C.2A/B.

Adding ``azure-mgmt-resourcegraph`` would have pulled in the full
Azure SDK stack (~30 transitive packages) for a single REST call.

## Authentication reuse

The Resource Graph client reuses Phase 7C.2A's
``ClientAssertionCredential`` via
``acquire_access_token()``.  No new credential strategy was added.
The same STS federation pipeline that authenticates Cost Management
queries authenticates Resource Graph queries.

## KQL query shape

The query is **hardcoded** inside
``app/services/cloud/azure/resource_graph.py::_build_resources_query``.
The frontend cannot inject KQL — the route does not accept a KQL
parameter and the body is built entirely server-side.

```kql
Resources
| project id, name, type, location, resourceGroup, subscriptionId, tags
```

* Only the columns the application needs are projected.
* No oversized ``properties`` payload is fetched.
* Tags are projected so the front-end can render resource tagging
  without a second round-trip.
* The KQL contains **NO** ``| limit`` / ``| take`` / ``| sample``
  operator — see the pagination section below for why this matters.

## Pagination contract (corrected in Phase 7C.2C.1)

Microsoft Resource Graph paginates large responses via the
``$skipToken`` mechanism.  The page size is controlled by the
``options.$top`` request field.  The server returns
``resultTruncated=true`` (note: ``resultTruncated``, **not**
``dataTruncated``) when the response is incomplete.

### Why the KQL must NOT include ``| limit`` / ``| take`` / ``| sample``

Microsoft's documentation states that ``limit`` / ``take`` /
``sample`` operators are **incompatible** with ``$skipToken``
continuation.  Using them returns ``resultTruncated=false`` even
when more rows are available, silently capping the inventory at
that value and disabling pagination.  The page size is therefore
controlled via ``options.$top`` alone, never via a KQL operator.

### Pagination algorithm

1. First request: ``options.resultFormat=objectArray``,
   ``options.$top=PAGE_TOP`` (default 1000); KQL has NO ``| limit``.
2. While ``resultTruncated=true``:
   * Send the next request with the **same** query, the **same**
     ``options.resultFormat``, the **same** ``$top``, and
     ``options.$skipToken=<token>``.
   * Defensive loop guard (``MAX_PAGES = 50``): if more than 50 pages
     are required, raise ``UPSTREAM_ERROR`` rather than silently
     returning a partial result.
   * Defensive ``$skipToken`` protection: a repeated cursor means
     the upstream is looping — raise ``UPSTREAM_ERROR``.
   * Defensive page-signature protection: if the upstream echoes
     the same page back (same first 3 ids), raise ``UPSTREAM_ERROR``.
   * Defensive ``resultTruncated=true`` without a ``$skipToken``:
     raise ``PAGINATION_INCOMPLETE`` when ``count < totalRecords``.
     A special case — when ``count == totalRecords`` the page is
     logically complete and we accept it as the success boundary.
3. Within-page dedup: rows with the same ``composite_id`` are
   dropped so an upstream that echoes rows across pagination
   windows never produces duplicates in the aggregator.

### Field name correction

The previous implementation checked ``dataTruncated`` (the legacy
Microsoft Resource Manager field); Resource Graph uses
``resultTruncated``.  Phase 7C.2C.1 fixes this so pagination is
actually triggered in production.

## Normalized resource model

Each Azure Resource Graph row is translated into a
:class:`~app.services.cloud.models.CloudResource`:

| Resource Graph column | CloudResource field |
|-----------------------|---------------------|
| `id` | `resource_id` (and embedded in `composite_id = "AZURE\|<id>"`) |
| `name` | `name` |
| `type` | `resource_type` and `service` (canonical provider identifier preserved) |
| `location` | `region` |
| `subscriptionId` | `account_id` (also exposed via `by_subscription` for attribution) |
| `resourceGroup` | `metadata["resource_group"]` |
| `tags` | `metadata["tags"]` (coerced to `str → str`) |

**No raw Azure ``properties``, ``sku``, ``identity``, or ``plan``
blobs ever reach the shared model.**  ``normalize_row`` explicitly
projects only the columns the application needs.

## Multi-subscription behavior

Two paths through the aggregator:

### Single subscription (``account_id=sub:<guid>``)

The aggregator fans out across the selected subscription via
``AzureResourceGraphClient.query(subscription_ids=[<sub>])``.

### All Azure Subscriptions (``account_id=all`` or absent)

The aggregator issues **one** tenant-scoped Resource Graph query
(empty ``subscriptions`` list — Resource Graph then fans out across
every subscription the configured identity is authorised to see)
and partitions the rows by ``subscriptionId``.  This is one
upstream call regardless of subscription count, with per-subscription
attribution preserved in the response.

### Status semantics

| Status | Meaning |
|--------|---------|
| `SUCCESS` | At least one subscription returned resources, no failures. |
| `NO_DATA` | Every subscription returned zero rows AND no failures (genuine empty). |
| `PARTIAL_SUCCESS` | At least one subscription succeeded, another failed. |
| `ERROR` | Every subscription failed. |

Per-subscription attribution is preserved via
``by_subscription`` and ``subscription_errors``.

### Aggregation invariants (Phase 7C.2C.1)

The aggregator enforces the following invariants:

* ``sum(by_type counts) == total_resources``
* ``sum(by_location counts) == total_resources`` (resources
  without a region are bucketed under ``"Unassigned"`` so the sum
  always equals the total).
* ``sum(by_resource_group counts) == total_resources`` (same
  ``"Unassigned"`` treatment for resources without a group).
* ``sum(by_subscription counts) == total_resources``
* Resources are dedup'd by ``composite_id`` within and across
  subscriptions.

These invariants are unit-tested and validated against live data.

## Error handling

Every non-2xx response is folded into a sanitised
``AzureResourceGraphErrorCategory``:

| HTTP | Category | HTTP response |
|------|----------|---------------|
| 401 | `AUTH_FAILED` | 401 |
| 403 | `ACCESS_DENIED` | 403 |
| 404 | `NOT_FOUND` | 404 |
| 429 | `THROTTLED` | 429 + `Retry-After` |
| 5xx | `UPSTREAM_ERROR` | 502 |
| network | `NETWORK_ERROR` | 502 |
| bad JSON | `INVALID_RESPONSE` | 502 |
| truncated + no cursor + `count < totalRecords` | `PAGINATION_INCOMPLETE` | 502 |

## Security considerations

* No ``Authorization: Bearer …`` is ever logged.
* No access token / client assertion / STS JWT is ever exposed.
* Subscription IDs are truncated to the first 8 hex characters via
  ``safe_subscription_label`` before logging.
* The KQL query is hardcoded; the front-end cannot inject KQL.
* Tags are coerced to ``str → str`` and scrubbed via
  ``scrub_log_payload`` before any error path echoes them.
* No raw Azure ``properties`` blob is fetched — we project only
  the columns the application needs.
* ``MAX_PAGES = 50`` prevents an unbounded request chain.

## Live validation results (Phase 7C.2C.1)

The Phase 7C.2B-validated identity (STS federated to Entra) has
the following Resource Graph permissions:

* Read access on the **MCPP** subscription (ID
  ``a54d076b-2650-4b4f-a28c-2deed2011f83``).
* Read access on the **Microsoft Azure Sponsorship** subscription
  (ID ``9353d858-27fa-4ce7-bf87-782f3323d297``).
* Tenant-wide read access via the empty-subscriptions query mode
  (which fans out across every accessible subscription).

No additional RBAC was assigned.  The configuration inherited
from Phase 7C.2B (Reader + Cost Management Reader on the
subscriptions) is sufficient for Resource Graph enumeration.

Required permission (documented for future operators):

```
Microsoft.ResourceGraph/resources/read
```

This is granted by the **Reader** built-in role, which both
subscriptions already have via Phase 7C.2B's bootstrap.

### Aggregation invariants — live validation against the real backend

**All Azure Subscriptions:**

| Metric | Value |
|--------|-------|
| Total resources | 25 |
| Distinct resource types | 8 |
| Distinct locations | 2 |
| Distinct resource groups | 4 |
| Subscriptions | 2 |

By type: `microsoft.network/networkinterfaces` × 5,
`microsoft.network/networksecuritygroups` × 5,
`microsoft.network/publicipaddresses` × 5,
`microsoft.network/virtualnetworks` × 5,
`microsoft.network/networkwatchers` × 2,
`microsoft.botservice/botservices` × 1,
`microsoft.compute/disks` × 1,
`microsoft.compute/virtualmachines` × 1.
Sum: 5+5+5+5+2+1+1+1 = 25. ✓

By location: `eastus` × 24, `global` × 1. Sum: 24+1 = 25. ✓

By resource group: `hiplink` × 16, `hiplinkqa` × 6,
`networkwatcherrg` × 2, `hiplink-teams-integration` × 1.
Sum: 16+6+2+1 = 25. ✓

By subscription: MCPP × 18, Sponsorship × 7. Sum: 18+7 = 25. ✓

**MCPP Subscription:**

| Metric | Value |
|--------|-------|
| Total | 18 |
| Distinct types | 6 |
| Distinct locations | 2 |
| Distinct resource groups | 3 |

**Sponsorship Subscription:**

| Metric | Value |
|--------|-------|
| Total | 7 |
| Distinct types | 7 |
| Distinct locations | 1 |
| Distinct resource groups | 2 |

## What Phase 7C.2C / 7C.2C.1 does NOT ship

* Azure Advisor / optimization — Phase 7C.2D.
* Rackspace provider — not part of 7C.2C.
* Cost forecasting — separate phase.
* Resource lifecycle actions — strictly READ-ONLY.
* VM / storage / network modifications — none.
