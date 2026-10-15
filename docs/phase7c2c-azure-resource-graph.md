# Phase 7C.2C — Azure Resource Graph Integration

This document describes the Azure Resource Graph inventory
integration shipped in Phase 7C.2C.  It covers the architecture,
KQL query shape, normalized resource model, pagination strategy,
multi-subscription behavior, error handling, security, and live
validation results.

## Architecture

```
Frontend (ProviderContext + AccountContext)
    │
    └─ AZURE path → /api/cloud/resources  (canonical, Phase 7C.2C)
                          │
                          ├─ ProviderRegistry dispatch
                          │
                          └─ AzureCloudProvider.get_resources() / get_resource_summary()
                                  │
                                  ├─ AzureResourceGraphClient (REST)
                                  └─ AzureResourceAggregator (fan-out + dedup + counts)
                                          │
                                          └─ CloudResource / CloudResourceSummary (normalized)
```

The AWS legacy routes (``/api/aws/resources`` and
``/api/aws/resources/all``) are **untouched** by this phase.
The canonical provider-neutral route delegates to the AWS adapter
for ``provider='AWS'`` so the same normalized shape is returned,
but every AWS byte-for-byte behaviour is preserved.

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
| limit 1000
```

* Only the columns the application needs are projected.
* No oversized ``properties`` payload is fetched.
* Tags are projected so the front-end can render resource tagging
  without a second round-trip.
* The hard row limit (``PAGE_TOP = 1000``) bounds the upstream
  response size; pagination is driven by ``$skipToken`` when the
  server indicates ``dataTruncated=true``.

## Normalized resource model

Each Azure Resource Graph row is translated into a
:class:`~app.services.cloud.models.CloudResource`:

| Resource Graph column | CloudResource field |
|-----------------------|---------------------|
| `id` | `resource_id` (and embedded in `composite_id = "AZURE|<id>"`) |
| `name` | `name` |
| `type` | `resource_type` and `service` (canonical provider identifier preserved) |
| `location` | `region` |
| `subscriptionId` | `account_id` (also exposed via `by_subscription` for attribution) |
| `resourceGroup` | `metadata["resource_group"]` |
| `tags` | `metadata["tags"]` (coerced to `str → str`) |

**No raw Azure ``properties``, ``sku``, ``identity``, or ``plan``
blobs ever reach the shared model.**  ``normalize_row`` explicitly
projects only the columns the application needs.

## Pagination strategy

Resource Graph paginates via the response payload's
``$skipToken`` (a server-issued opaque cursor) when the response
is marked ``dataTruncated=true``.

The client:

* follows ``$skipToken`` verbatim on the next request,
* preserves the same Bearer token / credential mechanism as the
  initial call,
* bounds the loop with ``MAX_PAGES = 50`` so a pathological
  response cannot drive an unbounded request chain,
* defensively terminates the loop if ``dataTruncated=true`` is
  returned without a ``$skipToken`` (the upstream is signalling
  truncation without a cursor — we keep the rows we have).

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

## Live validation results

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

## What Phase 7C.2C does NOT ship

* Azure Advisor / optimization — Phase 7C.2D.
* Rackspace provider — not part of 7C.2C.
* Cost forecasting — separate phase.
* UI redesign — only minimal changes (ResourcesPage remains
  AWS-typed; Azure inventory reaches the front-end through the
  shared CloudContext bar; a dedicated Azure surface is a future
  enhancement).
* Resource lifecycle actions — strictly READ-ONLY.
* VM / storage / network modifications — none.
