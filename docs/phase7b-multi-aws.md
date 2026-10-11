# Phase 7B — Multi-AWS Account Architecture

> Phase 7B turns the Phase 7A single-AWS-account FinOps platform into
> a secure multi-AWS-account dashboard.  The existing single-account
> behaviour, professional HipLink UI, auth/RBAC, calendar-month
> billing, session persistence, and nginx posture are **preserved
> unchanged**.  New surface area is layered alongside, not on top.

## 1. Goals

* Allow ADMINs to register one or more target AWS accounts alongside
  the existing management account.
* Route every AWS call to the right account via `STS AssumeRole`
  (no static AWS keys anywhere — never in PostgreSQL, never in the
  frontend, never in `.env`, never in browser storage).
* Aggregate cost / resource / optimization data across accounts
  with deterministic math (sum, never average) and per-account
  failure isolation (one denied account cannot blank the dashboard).
* Extend the calendar-month billing, RBAC, and AI context model so
  each evidence row carries explicit account attribution.
* Leave the existing UI on Phase 7A's professional HipLink shell —
  the new selector lives in the existing top bar.

## 2. Architecture

```
Browser
   ↓
React + HipLink account selector (sessionStorage)
   ↓
FastAPI (?account_id=all|acct:<id>|"all" default)
   ↓
AWS account provider (cloud_accounts table)
   ├── Management account → instance profile (default chain)
   └── Target account     → instance profile → STS AssumeRole
                                       → boto3.Session (cached in memory)
   ↓
Phase 2/3/7B services (cost / resources / optimization / AI)
   ↓
Per-account or aggregated report (math = deterministic sum,
partial-success envelope for cross-account fan-out).
```

## 3. Database

New table `cloud_accounts`:

| Column            | Type            | Notes |
|-------------------|-----------------|-------|
| `id`              | `bigserial`     | PK |
| `provider`        | `varchar(16)`   | Always `AWS` in Phase 7B. CHECK constraint. |
| `account_id`      | `varchar(12)`   | 12-digit AWS account ID (validated). |
| `display_name`    | `varchar(120)`  | User-facing label. |
| `role_arn`        | `varchar(2048)` | IAM role ARN to assume. |
| `external_id`     | `varchar(1224)` | Optional; write-only at API surface. |
| `enabled`         | `boolean`       | Soft-disable flag. |
| `is_management`   | `boolean`       | True for the EC2-instance-profile account only. |
| `preferred_region`| `varchar(32)`   | Optional per-account region override. |
| `created_at`      | `timestamptz`   | server default `now()`. |
| `updated_at`      | `timestamptz`   | server default `now()` + on-update. |

Constraints:

* `UNIQUE(provider, account_id)`
* `UNIQUE INDEX (provider) WHERE is_management = true` — exactly one
  management row per provider.
* CHECK constraints on `provider`, `is_management`, `enabled`.

The Phase 7A cost cache's primary key (`account_id, cache_key`) is
preserved; in Phase 7B the *storage* account_id is the scope-prefixed
string `acct:<id>` or `all:<id>` so Account A never satisfies an
Account B request.

The Alembic migration `0004_cloud_accounts.py` creates this table
and is run as part of the standard `alembic upgrade head`.

## 4. Account / session lifecycle

`app.services.aws.account_provider`:

* `list_accounts`, `get_account_by_id`, `get_management_account`,
  `get_account_by_aws_id` — read helpers.
* `create_account`, `update_account`, `disable_account` — registry
  mutations.  The management row is immutable (cannot be disabled,
  cannot have `is_management` flipped).
* `ensure_management_account_row` — idempotent first-boot bootstrap
  that resolves the management identity via the instance profile
  and inserts the management row.  Wired into the FastAPI
  `on_event("startup")` handler.
* `resolve_status` — connectivity test that maps AWS error codes
  onto the public status enum (`CONNECTED`, `ACCESS_DENIED`,
  `ROLE_NOT_FOUND`, `DISABLED`, `ERROR`).  Reason strings are
  sanitized — never raw exception payloads.
* `AccountContext` — a frozen dataclass representing the active
  selector value (`"all"` or `"acct:<id>"`).

`app.services.aws.session_factory`:

* `AssumeRoleError` — sanitized wrapper around STS failures.
* `InMemorySessionCache` — process-local TTL-bounded cache; never
  persisted; thread-safe; per-key lock for in-process dedup.
* `AwsSessionFactory.management_session()` — instance profile.
* `AwsSessionFactory.target_session(role_arn, external_id, region)`
  — STS AssumeRole with refreshable credentials; `DurationSeconds`
  capped at 12 h; refreshes 5 minutes before expiry.
* `get_aws_client_for(service, account_row=...)` — single entry
  point for AWS client construction; all business services route
  through it.
* `RoleSessionName` is randomised per call so CloudTrail audit
  records are distinguishable per request burst.
* All log calls redact the role ARN to `arn:aws:iam::<id>:role/<redacted>`.

## 5. STS security model

* **No static AWS keys** are ever accepted, stored, logged, or
  transmitted by the application.  The only IAM secret that may
  ever reach the platform is an STS `ExternalId` registered via
  `POST /api/cloud-accounts` — it is write-only at the API surface
  and is never echoed back on any GET/PATCH response, never logged.
* Temporary STS credentials live exclusively inside
  `InMemorySessionCache`.  Restarting the backend discards every
  credential.
* The cache TTL is `min(expiry, requested_duration) - 300s` so a
  long-running request never picks up a credential that expires
  mid-flight.
* Every management-row call uses the EC2 instance profile (no
  `AssumeRole` round-trip).  Only target accounts require
  AssumeRole.
* `DurationSeconds` is bounded: minimum 900 s, maximum
  `ASSUMED_ROLE_DURATION_HARD_CAP` (12 h).
* The default `assumed-role` is `cost-detective-<random>` so the
  CloudTrail caller identity is human-recognisable.

## 6. Cache isolation

The cache key now has the shape:

```
v3|<scope>|user=<id>|<region>|<kind>|<signature>
```

* `scope = "all"` for the multi-account aggregate.
* `scope = "acct:<id>"` for a specific account.

The DB row's `account_id` column stores the scope-prefixed string.
A request for Account A therefore never satisfies an Account B
request, even when both use the same `days` / `month` / explicit
range / region.

The user-isolation rules established in Phase 7A are preserved:
cache keys include `user=<id>` so a multi-user deployment cannot
serve Account-A data to a different user.

Account switching triggers a refetch through the frontend API
helper — the in-memory `AccountContext` triggers a re-render
which forces the dashboard / cost / resource / optimization
hooks to call `apiFetch` with the new selection, and the cache
layer naturally produces a MISS for the new scope (or HIT if
the same scope was already fetched in this session).

## 7. Partial failure behaviour

Multi-account responses include an `account_errors` array that
captures per-account failures with sanitized reasons.  The
aggregate `status` is one of:

| Status            | Meaning |
|-------------------|---------|
| `SUCCESS`         | Every queried account returned data. |
| `PARTIAL_SUCCESS` | At least one account failed but the rollup is still usable. |
| `ERROR`           | No account returned data. |

Aggregation uses an `asyncio.Semaphore` of size
`PHASE7B_COST_MAX_CONCURRENCY` (default `4`) so a slow account
cannot serialise the rest of the fan-out.  Each per-account
fetch has a deadline (`PHASE7B_COST_ACCOUNT_TIMEOUT_SECONDS`,
default `30 s`).

## 8. AI attribution

The AI context builder now propagates `account_id` and
`account_display_name` for every evidence row (cost, service,
region, recommendation).  When the selector is `all`, evidence
rows are tagged with the specific account they came from so the
model can reason about per-account deltas.  The system prompt
explicitly forbids the model from inventing accounts, resource
ownership, costs, savings, or utilization values; this contract
is unchanged from Phase 7A.

## 9. RBAC

| Endpoint                                | ADMIN | ANALYST | VIEWER |
|-----------------------------------------|:-----:|:-------:|:------:|
| `GET    /api/cloud-accounts`            |  ✓   |         |        |
| `GET    /api/cloud-accounts/selectable` |  ✓   |    ✓    |   ✓    |
| `POST   /api/cloud-accounts`            |  ✓   |         |        |
| `PATCH  /api/cloud-accounts/{id}`       |  ✓   |         |        |
| `DELETE /api/cloud-accounts/{id}`       |  ✓   |         |        |
| `POST   /api/cloud-accounts/{id}/validate` |  ✓ |         |        |

RBAC is enforced via `require_admin` / `require_role` in
`app.api.cloud_accounts`.  Frontend visibility is informational
only; backend enforcement is the authoritative gate.

## 10. Known limitations / Phase 7C extension points

* `cloud_accounts.provider` is constrained to `AWS` at the
  database level (`CHECK provider IN ('AWS')`).  Phase 7C will
  relax this constraint and add `Azure` / `Rackspace` rows.
* The AWS-specific columns (`role_arn`, `external_id`,
  `preferred_region`) live alongside provider-agnostic columns
  (`display_name`, `enabled`, `is_management`).  When the CHECK
  is relaxed in Phase 7C, the same table will host Azure rows
  with `provider='Azure'` and a future `subscription_id` column
  (added by a later migration).
* `get_default_session_factory()` is the single seam to extend.
  Phase 7C will add `azure_session_factory()` and
  `rackspace_session_factory()` behind the same interface.
* Multi-region discovery is still out of scope.  Each call uses
  one explicit region (per-account `preferred_region` if set,
  otherwise `AWS_DEFAULT_REGION`).

## 11. Real second-account procedure (operator checklist)

The closure tag for Phase 7B (`phase-7b-complete`) is **not**
created automatically.  Once the verifier passes:

1. Pick a target AWS account ID (e.g. `222222222222`).
2. Create the IAM role in that target account per
   `docs/phase7b-cross-account-iam.md`.
3. Copy the role ARN (`arn:aws:iam::222222222222:role/...`).
4. Log in to the dashboard as ADMIN.
5. Open `/accounts` → **Register an account**.
6. Paste the role ARN, supply a display name, and (optionally)
   an external ID and preferred region.
7. Click **Validate** — the dashboard should report `CONNECTED`.
8. Switch the account selector to the new account — every page
   should show that account's data only.
9. Switch the selector back to **All AWS Accounts** — the
   dashboard should report a sum, with `by_account` totals
   shown explicitly in the costs page.
10. If any account is denied, the dashboard reports
    `PARTIAL_SUCCESS` and lists the failing account.

Failure at step 7 means the trust policy on the target role does
not yet allow the management role to `sts:AssumeRole`.  Failure
at step 8 with `AccessDenied` means the role's permission policy
is missing one of the read-only actions listed in
`docs/phase7b-cross-account-iam.md`.  Both are recoverable with no
code change.

The closure tag is created only after this checklist passes on a
real second AWS account.
