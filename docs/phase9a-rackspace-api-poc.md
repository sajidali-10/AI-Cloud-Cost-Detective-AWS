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
    billing.py       # read-only Billing V2 probes (documented base URL)
    servers.py       # read-only Cloud Servers probes
```

The client is **not** registered with
`app.services.cloud.registry.ProviderRegistry`.  It does not
appear in `/api/cloud/*` routes and is not wired into the
dashboard, the canonical AWS/Azure provider model, or the
optimization engine.  It exists only to demonstrate secure
read-only access to Rackspace.

## Two identifiers, two surfaces

Phase 9A authenticates against **two** distinct Rackspace
identifiers.  These identifiers are NOT interchangeable:

| Identifier                  | Env var                       | Used by                                  |
|-----------------------------|-------------------------------|------------------------------------------|
| Rackspace tenant / customer account number | `RACKSPACE_ACCOUNT_ID` | Identity-issued Cloud API URLs (e.g. `https://iad.servers.api.rackspacecloud.com/v2/{RACKSPACE_ACCOUNT_ID}`) |
| Rackspace Account Number (RAN) | `RACKSPACE_BILLING_ACCOUNT` | Billing API path (`/v2/accounts/{ran}`) — separate surface |

The tenant ID drives the Cloud Servers (Nova) / Cloud Files
(Swift) / Cloud BlockStorage paths.  The RAN drives the Billing
API paths.  A tenant may have one without the other; the POC
must not assume they are the same value.

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

* `RACKSPACE_USERNAME`, `RACKSPACE_API_KEY`, `RACKSPACE_ACCOUNT_ID`,
  `RACKSPACE_BILLING_ACCOUNT` are read via `Settings`.
* The values are never logged.  The shared HTTP client never
  echoes request bodies, and the `__repr__` of
  `RackspaceCredentials` substitutes `<redacted>` for every field.
* The application **does not refuse to start** when these
  variables are absent.  `get_config_status()` reports
  `NOT_CONFIGURED` and the rest of the application continues to
  function normally — Rackspace remains an optional provider.
* `RACKSPACE_BILLING_ACCOUNT` is independently optional: when
  absent, the Billing client reports `NOT_CONFIGURED` without
  issuing a malformed request against the Billing API.
* The host `.env` is `chmod 600` and is `.gitignore`d.  The
  validator script verifies this on every run.
* `.env.example` contains only empty placeholders — never real
  values.

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
present, so the read-only contract holds.  `billing:observer`
is a read-only role — it does NOT grant write access to billing
records.

## Service catalog — Cloud API only

The catalog returned by the validated tenant contains 21
service types — see the `service_types` field in the validator
output.  The POC resolves Cloud-API product endpoints by either
the catalog `type` field or the more stable `name` field so it
works against both the modern and the OpenStack-derived layouts.

### Billing is intentionally NOT in the catalog

The Rackspace Identity service catalog does NOT contain a
Billing entry, regardless of whether the tenant has Billing
enabled.  This is by Rackspace's documented design — the
Billing API is a separate surface that lives outside the
catalog.  Earlier Phase 9A notes incorrectly concluded that the
absence of a `cloudBilling` catalog entry meant Billing was
"unavailable" — this was a documentation error.

The corrected behaviour:

* The Billing client NEVER consults the Identity service catalog
  to discover the Billing endpoint.
* The Billing client always uses the documented base endpoint
  `https://billing.api.rackspacecloud.com`.
* The Billing client always uses the RAN (`RACKSPACE_BILLING_ACCOUNT`)
  as the path component — never the Cloud-API tenant ID.
* A 404 from the Billing API indicates either an invalid RAN or
  Billing not enabled on the account; it is NOT the absence of
  a catalog entry.

## Billing API result

The validated tenant's `RACKSPACE_BILLING_ACCOUNT` is configured
(when present in the host `.env`) and the Billing client probes
the documented V2 endpoints in safe order:

1. `GET /v2/accounts/{ran}` — billing account metadata.  Sanitized
   to `{currency, status, billing_account_identifier_present,
   current_period}`.  Raw `contactEmail` / `address` fields are
   NEVER projected.
2. `GET /v2/accounts/{ran}/currency` — billing currency.
   Sanitized to `{currency}`.
3. `GET /v2/accounts/{ran}/estimated_charges` — current-period
   estimate.  Sanitized to
   `{currency, total, sanitized_current_cost_available,
   current_period, [currencies, date_range, record_count]}`.
   The total is rounded to 2 decimals.  Per-resource breakdowns
   are NEVER included.
4. `GET /v2/accounts/{ran}/billing-summary` — billing summary
   metadata.  Sanitized to `{currency, total, current_period}`.
5. `GET /v2/accounts/{ran}/invoices/latest` — latest invoice
   metadata.  Sanitized to `{invoice_id_preview, currency,
   total, status, current_period}`.  Invoice line items are
   NEVER included.

Each endpoint produces a sanitized `BillingEndpointResult`
with status `OK` / `AUTH_FAILED` / `PERMISSION_DENIED` /
`NOT_FOUND` / `NOT_CONFIGURED` / `TRANSPORT_ERROR`.  **Raw
billing records are never included** in the summary — only
record counts, currency codes, date ranges, and rounded totals.

If Rackspace returns a genuine permission/account-specific
Billing error (e.g. HTTP 403 with `billing:observer` insufficient
for current-period data), the validator script reports the exact
sanitized HTTP status and meaning instead of fabricating data.

## Cloud Servers API result

`GET /servers/detail` against the validated tenant returns
HTTP 200 with a (potentially empty) `servers` list.  The
aggregate summary reports:

* `total_count`
* `status_counts`
* `regions`
* `flavor_ids`

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
* The application does not log tokens, API keys, passwords,
  RAN values, or support PINs.
* The application refuses to start with write-capable roles
  (see *Role model observed*).
* The `.env` file is `.gitignore`d and the validator verifies
  that its permissions do not allow world-read.

### Phase 8.4 compatibility

Phase 8.4's final security validator was originally written
when Rackspace was not yet approved for production use, and it
explicitly failed on the presence of `RACKSPACE_USERNAME` /
`RACKSPACE_API_KEY` in the host `.env`.  Phase 9A's approval
of the Rackspace integration updates that contract:

* The host `.env` is the canonical deployment surface for the
  three Identity credentials **and** the Billing Account
  Number (RAN).  Phase 8.4 now treats the presence of these
  four variables as PASS, provided:
  * `.env` mode is `600` (or `400`),
  * `.env` is gitignored,
  * no value appears in any TRACKED source file,
  * no value appears in `.env.example`,
  * `.env.example` uses empty placeholders only,
  * no `RACKSPACE_TOKEN` is required (X-Auth-Token is derived
    on demand and never persisted).
* A real Rackspace credential appearing in a TRACKED source
  file is still a HARD FAIL — the secret-protection guarantees
  of Phase 8.4 are preserved.

Two implementation details of the value scan:

* The scan covers TRACKED files only (same scope as the
  AWS / Azure secret checks).  Untracked local artifacts —
  `.kimchi/` session files, `frontend/dist` build output — are
  not part of the release and are out of scope.
* `RACKSPACE_API_KEY` and `RACKSPACE_BILLING_ACCOUNT` (the
  RAN) are high-specificity identifiers, so the bare value
  anywhere in a tracked file is a leak.  `RACKSPACE_USERNAME`
  is checked in a username-assignment context only
  (`RACKSPACE_USERNAME=...`, `"username": ...`): usernames are
  frequently short dictionary-like words that also occur as
  ordinary project vocabulary, and a bare exact-value scan of
  such a word produces false positives against prose.
* The unit tests use SYNTHETIC RAN fixtures (e.g. `020-000001`,
  `020-123456`) — never a real account number.

## Tests

51 unit tests under `backend/tests/test_rackspace_*.py`:

* `test_rackspace_identity.py` — Identity success, invalid
  credentials, role parsing, elevated-role detection, token
  expiration, token caching, service-catalog parsing (both
  layouts), token redaction.
* `test_rackspace_billing.py` — Billing base endpoint is
  external to service catalog, RAN vs tenant ID separation,
  account lookup success, currency success, estimated charges
  success, list-envelope summarization, missing RAN,
  empty RAN, HTTP 401 / 403 / 404, 401 invalidates Identity,
  `probe_all` ordering and structure, token redaction, no
  write methods, `BillingEndpointResult` frozen dataclass.
* `test_rackspace_servers.py` — list / detail, aggregate
  summary, 401 invalidates identity, 403 / 404 / transport,
  write-method surface check.
* `test_rackspace_config_and_client.py` — `get_config_status`
  variants, `get_credentials` missing, `get_billing_account`
  / `billing_account_configured`, credential redaction,
  HTTP-client verb surface, default timeout bound, header
  redaction, pinned identity URL, `.env.example` placeholder
  check (covers all four Rackspace keys), `.gitignore` check.

No test requires real Rackspace credentials.

## Live POC validator

`scripts/phase9a_rackspace_poc_validation.sh` exercises every
gate against the live Rackspace APIs and exits with:

* `0` — POC successful.
* `1` — validation failure (a gate failed).
* `2` — configuration / pre-condition violated.

Mandatory gates (all must PASS):

* Identity PASS
* elevated-role check PASS
* Cloud Servers read PASS
* Billing account GET PASS (when RAN is configured)
* Billing current/estimated charges GET PASS if supported for
  the account
* zero product write requests
* zero credential leakage

## Unsupported / unavailable APIs

The validated tenant's Identity service catalog does not include
Billing entries — and per Rackspace documentation this is
expected for every tenant.  The Billing client therefore
NEVER looks the catalog up for Billing; it always uses the
documented base URL directly.

## Next-step recommendation

1. Wire the Rackspace POC into the canonical cloud provider
   model (`app/services/cloud/registry.py`) only after the
   Billing API is observed working in production.  Until then,
   Billing remains an isolated read-only surface.
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
backend/app/services/rackspace/billing.py       (rewritten — documented URL)
backend/app/services/rackspace/servers.py
backend/app/core/config.py                       (RACKSPACE_BILLING_ACCOUNT)
backend/tests/test_rackspace_identity.py
backend/tests/test_rackspace_billing.py          (expanded — 12 new tests)
backend/tests/test_rackspace_servers.py
backend/tests/test_rackspace_config_and_client.py (updated — RAN helpers)
backend/tests/conftest.py                        (Rackspace env isolation)
scripts/phase8_4_final_security_validation.sh   (15b: Rackspace approval)
scripts/phase9a_rackspace_poc_validation.sh      (rewritten — Billing gates)
scripts/phase9a_rackspace_poc_runner.py          (rewritten — per-endpoint output)
.env.example                                     (RACKSPACE_BILLING_ACCOUNT placeholder)
docs/phase9a-rackspace-api-poc.md                (this file — corrected)
```
