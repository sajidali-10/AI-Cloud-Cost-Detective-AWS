# Phase 9A — Rackspace Cloud API Proof of Concept

This document records the design, observed behaviour, and
results of the Phase 9A proof-of-concept integration against the
Rackspace Cloud APIs.  Phase 9A is a *read-only* POC — it proves
that the existing FinOps platform can authenticate to Rackspace
Identity, observe its service catalog, and exercise the
least-privilege Billing and Cloud Servers endpoints without
touching any Rackspace resource.

## Scope

The POC introduces a new, isolated Rackspace client under
`backend/app/services/rackspace/`:

```
backend/app/services/rackspace/
    __init__.py
    client.py        # shared httpx wrapper (GET + Identity POST only)
    exceptions.py    # typed exception hierarchy
    config.py        # credential resolution from Settings
    identity.py      # Identity authentication + token cache
    billing.py       # read-only Billing probes
    servers.py       # read-only Cloud Servers probes
```

The client is **not** registered with
`app.services.cloud.registry.ProviderRegistry`.  It does not
appear in `/api/cloud/*` routes and is not wired into the
dashboard, the canonical AWS/Azure provider model, or the
optimization engine.  It exists only to demonstrate secure
read-only access to Rackspace.

## Authentication flow

Rackspace Cloud uses the OpenStack Keystone-derived Identity API
v2.0.  Authentication is performed against the canonical
endpoint:

```
POST https://identity.api.rackspacecloud.com/v2.0/tokens
{
  "auth": {
    "RAX-KSKEY:apiKeyCredentials": {
      "username": "<RACKSPACE_USERNAME>",
      "apiKey":   "<RACKSPACE_API_KEY>"
    }
  }
}
```

A successful response carries:

* `access.token.id` — the temporary X-Auth-Token
* `access.token.expires` — ISO-8601 expiry timestamp
* `access.user.roles` — the assigned role set
* `access.serviceCatalog` — the tenant's authorized service
  catalog

The identity client (`app/services/rackspace/identity.py`)
parses both the OpenStack-derived shape and the modern Rackspace
Cloud shape (where `serviceCatalog` is a bare list and `roles`
live at `access.user` instead of `access.token.user`).  Either
layout is normalised into a single `IdentitySession` dataclass.

### Token caching

The client keeps the issued token in memory and re-authenticates
only when:

1. The cached token is within `TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS`
   (60 s) of expiry, **or**
2. A downstream product API returns HTTP 401 (the cached session
   is invalidated and the next call re-authenticates).

The token is **never** logged.  `IdentitySession.__repr__` renders
the token as `<redacted>` and the shared HTTP client drops
`X-Auth-Token`, `X-Auth-Key`, and `X-Server-Management-Url`
headers from any response wrapper before returning to callers.

## Credential handling

* `RACKSPACE_USERNAME`, `RACKSPACE_API_KEY`, `RACKSPACE_ACCOUNT_ID`
  are read once at authentication time via `Settings`.
* The values are never logged.  The shared HTTP client never
  echoes request bodies, and the `__repr__` of
  `RackspaceCredentials` substitutes `<redacted>` for every field.
* The application **does not refuse to start** when these
  variables are absent.  `get_config_status()` reports
  `NOT_CONFIGURED` and the rest of the application continues to
  function normally — Rackspace remains an optional provider.
* The host `.env` is `chmod 600` and is `.gitignore`d.  The
  validator script verifies this on every run.
* `.env.example` contains only empty placeholders.

## Role model observed

The POC fails closed on any of these elevated roles being
present in the authenticated principal's role set:

| Role                  | Why it's flagged                      |
|-----------------------|---------------------------------------|
| `admin`               | Tenant-wide write capability          |
| `billing:admin`       | Write access to billing records       |
| `creator`             | Legacy write role on First Gen        |
| `identity:user-admin` | Can create users                      |
| `identity:admin`      | Can change identity configuration     |

For the validated Rackspace tenant the assigned role set is:

```
billing:observer
compute:default
identity:default
identity:tenant-access
object-store:default
observer
```

Every role in this set is read-only.  No elevated role is
present, so the read-only contract holds.

## Service catalog

The catalog returned by the validated tenant contains 21
service types — see the `service_types` field in the validator
output.  The POC resolves product endpoints by either the
catalog `type` field or the more stable `name` field so it works
against both the modern and the OpenStack-derived layouts.

## Billing API result

The validated tenant does **not** include `cloudBilling` (or any
of the legacy billing identifiers) in its service catalog.  The
POC reports `NOT_FOUND` for every Billing endpoint — this is the
expected graceful-degradation outcome when the Billing API is
not enabled on an account.

When Billing *is* enabled on a future tenant the POC will probe:

* `GET /v1/billing` — billing account metadata (currency,
  contact email, sanitized account identifier).
* `GET /v1/billing/payments` — record count, currency set,
  date range.
* `GET /v1/billing/invoices` — record count, currency set,
  date range.

Each endpoint produces a sanitized `BillingEndpointResult`
with status `OK` / `AUTH_FAILED` / `PERMISSION_DENIED` /
`NOT_FOUND` / `TRANSPORT_ERROR`.  **Raw billing records are
never included** in the summary — only record counts, currency
codes, and date ranges.

## Cloud Servers API result

`GET /servers/detail` against the validated tenant returns
HTTP 200 with an empty `servers` list.  The aggregate summary
reports:

* `total_count: 0`
* `status_counts: {}`
* `regions: []`
* `flavor_ids: []`

This is the expected outcome for a freshly-provisioned,
read-only POC tenant.

The servers client exposes only `list_servers`,
`list_servers_detail`, and `get_aggregate_summary`.  IP
addresses, hostnames, and `user-data` payloads are
**deliberately excluded** from the aggregate summary because
they can contain customer-sensitive material.

## Read-only guarantees

The shared HTTP client (`RackspaceHttpClient`) exposes only
`get()` and `post_json()`.  There is no `put`, `patch`, or
`delete` method.  The identity POST is the only write-like
verb allowed, and it is reserved exclusively for authentication.

The `scripts/phase9a_rackspace_poc_runner.py` runtime check
asserts this contract by introspecting the public verb surface
and reporting `write_methods_exposed: []`.  The validator script
asserts the same contract at runtime and fails if any write
verb is observed.

## Security

* The application does not call any Rackspace write endpoint.
* The application does not modify Rackspace roles, billing
  data, or servers.
* The application does not log tokens, API keys, passwords, or
  support PINs.
* The application refuses to start with write-capable roles
  (see *Role model observed*).
* The `.env` file is `.gitignore`d and the validator verifies
  that its permissions do not allow world-read.

## Tests

39 unit tests under `backend/tests/test_rackspace_*.py`:

* `test_rackspace_identity.py` — Identity success, invalid
  credentials, role parsing, elevated-role detection, token
  expiration, token caching, service-catalog parsing (both
  layouts), token redaction.
* `test_rackspace_billing.py` — Billing 200 / 401 / 403 / 404,
  missing-catalog graceful degradation, transport error, probe
  aggregator.
* `test_rackspace_servers.py` — list / detail, aggregate
  summary, 401 invalidates identity, 403 / 404 / transport,
  write-method surface check.
* `test_rackspace_config_and_client.py` — `get_config_status`
  variants, `get_credentials` missing, credential redaction,
  HTTP-client verb surface, default timeout bound, header
  redaction, pinned identity URL, `.env.example` placeholder
  check, `.gitignore` check.

No test requires real Rackspace credentials.

## Live POC validator

`scripts/phase9a_rackspace_poc_validation.sh` exercises every
gate against the live Rackspace APIs and exits with:

* `0` — POC successful.
* `1` — validation failure (a gate failed).
* `2` — configuration / pre-condition violated.

## Unsupported / unavailable APIs

The validated tenant does not include:

* `cloudBilling` / `rax:billing` — no Billing endpoints reachable.

These are reported as `NOT_FOUND` rather than errors.

## Next-step recommendation

1. Wire the Rackspace POC into the canonical cloud provider
   model (`app/services/cloud/registry.py`) only after the
   Billing API is enabled for the tenant.  Until then, Billing
   remains `NOT_FOUND`.
2. Add `cloudBlockStorage` and `cloudFiles` probes once a
   workload exists — Phase 9A deliberately stopped at the two
   highest-value APIs (Billing, Cloud Servers).
3. Promote the read-only guard from a runtime check to a CI
   job that fails when the public verb surface of any Rackspace
   client grows beyond `get` / `post_json`.

## Files added / changed

```
backend/app/services/rackspace/__init__.py
backend/app/services/rackspace/client.py
backend/app/services/rackspace/config.py
backend/app/services/rackspace/exceptions.py
backend/app/services/rackspace/identity.py
backend/app/services/rackspace/billing.py
backend/app/services/rackspace/servers.py
backend/app/core/config.py                 (typed Rackspace settings)
backend/tests/test_rackspace_identity.py
backend/tests/test_rackspace_billing.py
backend/tests/test_rackspace_servers.py
backend/tests/test_rackspace_config_and_client.py
scripts/phase9a_rackspace_poc_validation.sh
scripts/phase9a_rackspace_poc_runner.py
.env.example                                (placeholder-only entries)
docs/phase9a-rackspace-api-poc.md           (this file)
```
