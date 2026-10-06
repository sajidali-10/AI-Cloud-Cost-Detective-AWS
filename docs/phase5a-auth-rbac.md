# Phase 5A — Authentication & RBAC Foundation

## Overview

Phase 5A introduces **application-local authentication and
role-based access control** to the AI Cloud Cost Detective
backend.  After this phase:

* Every business HTTP request can be authenticated with a bearer
  JWT (when `AUTH_ENABLED=true`).
* Users are partitioned into three roles — `ADMIN`, `ANALYST`,
  `VIEWER` — with an explicit authorization matrix applied at the
  route layer.
* Administrator-managed user CRUD (create / list / patch) is
  available behind a dedicated `/api/admin/users` API.
* Phase 0-4 development flows continue to work unchanged when
  `AUTH_ENABLED=false`.

Phase 5A does **not** introduce:

* Conversation history or message persistence (Phase 5B).
* WebSockets (Phase 5C).
* External identity providers (OIDC, SAML, Cognito, Google login).
* Refresh-token database or token rotation.
* Email-based password reset.
* Multi-factor authentication.

The architecture is intentionally extensible: a single
`AuthService` and a small set of FastAPI dependencies own all
identity decisions, so a future OIDC integration can replace the
token issuer without rewriting business APIs.

## Architecture

```
                      +------------------------------+
   HTTP request  -->  |  FastAPI dependency layer    |  <-- get_current_user
                      |  (app/api/deps.py)           |       require_role(...)
                      +---------------+--------------+       require_admin
                                      |
                                      v
                      +------------------------------+
                      |  AuthService                 |
                      |  (app/services/auth_service) |
                      +---------------+--------------+
                                      |
            +-------------------------+-------------------------+
            v                         v                         v
   Argon2id PasswordHasher    SecurityCore (PyJWT)        SQLAlchemy ORM
   (password hashing)         (issue / decode tokens)     (app_users table)
```

* `app/api/deps.py` is the **only** module that parses JWTs and
  enforces roles.  Business routes depend on `require_role(...)`
  or `require_admin`.
* `app/services/auth_service.py` owns the domain logic:
  authenticate, resolve current user, issue token, CRUD users.
* `app/services/password_hasher.py` is the **only** module that
  ever compares a plaintext password to a stored hash.
* `app/core/security.py` owns JWT issuance and verification.

## User model

The `app_users` table (Alembic migration `0002_app_users`):

| Column          | Type             | Notes                                      |
|-----------------|------------------|--------------------------------------------|
| `id`            | BIGSERIAL PK     | stable internal id                         |
| `email`         | VARCHAR(320)     | NOT NULL UNIQUE; normalised lower-case     |
| `password_hash` | VARCHAR(255)     | Argon2id PHC string; never returned        |
| `display_name`  | VARCHAR(120)     |                                            |
| `role`          | VARCHAR(16)      | CHECK IN (`ADMIN`,`ANALYST`,`VIEWER`)      |
| `is_active`     | BOOLEAN          | DEFAULT TRUE                               |
| `created_at`    | TIMESTAMPTZ      | DEFAULT now()                              |
| `updated_at`    | TIMESTAMPTZ      | DEFAULT now()                              |
| `last_login_at` | TIMESTAMPTZ NULL | stamped on successful login                |

Email is normalised (lower-case + trim) by `normalise_email()`
before insertion; the DB-level `UNIQUE` constraint is a
belt-and-braces invariant.  The role `CHECK` prevents a
programming bug from inserting an unrecognised value.

No conversation / message table is added in Phase 5A — that
belongs to Phase 5B.

## Roles & authorization matrix

| Endpoint                            | ADMIN | ANALYST | VIEWER |
|-------------------------------------|:-----:|:-------:|:------:|
| `GET /health`, `GET /health/ready`  | ✓     | ✓       | ✓      |
| `POST /api/auth/login`              | ✓     | ✓       | ✓      |
| `GET /api/auth/info`                | ✓     | ✓       | ✓      |
| `GET /api/auth/me`                  | ✓     | ✓       | ✓      |
| `GET /api/aws/identity`             | ✓     | ✓       | ✓      |
| `GET /api/aws/resources`            | ✓     | ✓       | ✓      |
| `GET /api/aws/costs`                | ✓     | ✓       | ✓      |
| `POST /api/aws/evidence`            | ✓     | ✓       | ✓      |
| `POST /api/aws/utilization`         | ✓     | ✓       | ✓      |
| `GET /api/aws/optimization/*`       | ✓     | ✓       | ✓      |
| `GET /api/ai/status`                | ✓     | ✓       | ✓      |
| `POST /api/ai/executive-summary`    | ✓     | ✓       | ✗      |
| `POST /api/ai/analyze`              | ✓     | ✓       | ✗      |
| `POST /api/ai/recommendations/...`  | ✓     | ✓       | ✗      |
| `POST /api/admin/users`             | ✓     | ✗       | ✗      |
| `GET  /api/admin/users`             | ✓     | ✗       | ✗      |
| `PATCH /api/admin/users/{id}`       | ✓     | ✗       | ✗      |

* ✓ = permitted; ✗ = 403 `Forbidden`.
* "Application ADMIN does NOT mean AWS administrator": the role
  controls only what the user can do via the Cost Detective API.
  Backend AWS access continues via the EC2 IAM Role + IMDSv2 and
  is read-only.

## Token format

Stateless bearer JWT, HS256 by default:

```json
{
  "sub": "42",
  "role": "ADMIN",
  "iat": 1730000000,
  "exp": 1730003600,
  "iss": "ai-cloud-cost-detective",
  "aud": "ai-cloud-cost-detective-api",
  "jti": "abcdef0123456789..."
}
```

* `sub` is the user id (string-encoded for JWT serialisation).
* `role` is the role at issuance time.  The auth layer treats
  this as a HINT only and re-loads the user from the DB on every
  request to authorise — a forged claim cannot escalate.
* `exp` defaults to 60 minutes from issue.
* Algorithm is configurable via `JWT_ALGORITHM` so a future OIDC
  integration can switch to RS256/ES256 without code changes.

## Configuration

| Variable                    | Required when         | Default                                     |
|-----------------------------|-----------------------|---------------------------------------------|
| `AUTH_ENABLED`              | always                | `false`                                     |
| `JWT_SECRET`                | `AUTH_ENABLED=true`   | empty (validator refuses to start)          |
| `JWT_ALGORITHM`             | optional              | `HS256`                                     |
| `JWT_ACCESS_TOKEN_MINUTES`  | optional              | `60`                                        |
| `JWT_ISSUER`                | optional              | `ai-cloud-cost-detective`                   |
| `JWT_AUDIENCE`              | optional              | `ai-cloud-cost-detective-api`               |

When `AUTH_ENABLED=true`, the application **refuses to start**
unless `JWT_SECRET` is at least 32 characters and is not one of
the documented placeholders.  The error message is sanitized — it
never includes the secret value.

## Password security

* Algorithm: **Argon2id** via `argon2-cffi` (OWASP-recommended).
* Defaults: `time_cost=3`, `memory_cost=64 MiB`,
  `parallelism=2`, `hash_length=32`, `salt_length=16`.
* Verification is intentionally slow (typically 50–250 ms) — this
  is the application-layer brute-force defence.  See
  "Rate / brute-force safety" below.

## API endpoints

### `POST /api/auth/login`

Anonymous.  Request:

```json
{"email": "admin@example.com", "password": "..."}
```

Success → 200 with:

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user": {"id": 1, "email": "...", "display_name": "...",
           "role": "ADMIN", "is_active": true,
           "created_at": "...", "updated_at": "...",
           "last_login_at": null}
}
```

Failure → 401 with `{status: "error", error_code:
"InvalidCredentials", message: "invalid credentials"}`.  The
message is identical for unknown email, wrong password, and
inactive user (user-enumeration defence).

### `GET /api/auth/me`

Authenticated.  Returns the current user (no `password_hash`).
401 if the token is missing / invalid / expired.

### `GET /api/auth/info`

Anonymous.  Returns:

```json
{"auth_enabled": true, "issuer": "...",
 "audience": "...", "algorithm": "HS256",
 "access_token_minutes": 60}
```

Never returns the JWT secret.

### `POST /api/admin/users`

ADMIN only.  Create a user.  Body:

```json
{"email": "...", "password": "...", "display_name": "...",
 "role": "ANALYST"}
```

Returns 201 with the user (no hash), 409 on duplicate email,
400 on invalid role.

### `GET /api/admin/users`

ADMIN only.  Lists all users (no hashes).

### `PATCH /api/admin/users/{user_id}`

ADMIN only.  Partial update.  Supports `role`, `is_active`,
`display_name`, `password` (admin-set new password).  Refuses to
deactivate or demote the calling admin.

## First-admin creation

A controlled CLI is provided at `scripts/create_admin.py`:

```bash
docker compose exec backend python scripts/create_admin.py
```

* Prompts for email / display name / password (hidden).
* Refuses to run when `AUTH_ENABLED=false`.
* Refuses to create a user with a well-known default email
  (`admin@admin`, `admin@localhost`, `admin@example.com`) or
  password (`admin`, `admin123`, `password`, `changeme`).
* Refuses passwords shorter than 12 characters.
* Uses the same `AuthService.create_user` as the API, so the new
  admin can log in immediately.

The application does **not** create an administrator on startup.
There is no auto-generated default account.

## AWS credential isolation

* Backend AWS access continues via EC2 IAM Role + IMDSv2.  No
  change to AWS credentials handling in Phase 5A.
* JWT claims never translate to AWS IAM permissions.
* AWS APIs remain read-only — Phase 5A introduces no mutation
  endpoints and does not relax the existing read-only guard.
* Application `ADMIN` is not an AWS administrator; the backend's
  AWS access is determined entirely by the IAM role on the EC2
  instance, not by the calling user.

## Rate / brute-force safety

Phase 5A does not add distributed (Redis-backed) rate limiting.
It relies on two layers:

1. **Argon2id verification cost** — the time-cost and memory-cost
   parameters of the hash deliberately slow each failed login
   attempt.  An attacker who steals the `app_users` table cannot
   brute-force passwords at network speed.
2. **Generic `InvalidCredentials` responses** — every login
   failure returns the same 401 body, removing the user-
   enumeration oracle.

For production deployments, **a Redis-backed rate limiter is
strongly recommended** at the reverse-proxy or application layer
(e.g. limit `/api/auth/login` to 5 attempts per IP per minute,
plus an exponential back-off per account).  This is documented
as deferred in the Phase 5A report.

## Error envelope

Authentication and authorization errors reuse the existing
project envelope:

```json
{
  "status": "error",
  "error_code": "<StableCode>",
  "message": "<sanitized>"
}
```

Stable codes used in Phase 5A:

| HTTP | error_code        | When                                     |
|------|-------------------|------------------------------------------|
| 401  | `Unauthenticated` | missing/invalid/expired token             |
| 401  | `InvalidCredentials` | login failure (no enumeration)         |
| 403  | `Forbidden`       | role does not satisfy `require_role`     |
| 400  | `InvalidRole`     | admin request carries unknown role        |
| 400  | `InvalidInput`    | admin request payload is malformed        |
| 400  | `SelfDeactivation`| admin tries to deactivate themselves     |
| 400  | `SelfDemotion`    | admin tries to demote themselves          |
| 404  | `UserNotFound`    | admin patch on unknown user id            |
| 409  | `UserAlreadyExists` | duplicate email                         |
| 503  | `AuthDisabled`    | reserved for future use                  |

The error envelope never contains: the password, the password
hash, the JWT secret, the token, or the Authorization header.

## Logging safety

Allowed log fields (sanitized):

* `auth.login.ok user_id=<id> role=<role>`
* `auth.login.denied email=<normalised-email>`
* `auth.login.failed user_id=<id> reason=...`
* `admin.users.create user_id=<id> role=<role>`
* `admin.users.update user_id=<id> role=<role> is_active=<bool>`
* `rbac.denied user_id=<id> role=<role> required=<set>`

Never logged:

* passwords
* JWTs
* Authorization headers
* password hashes
* JWT signing secret value

`phase5a_verify.sh` scans the last 1000 lines of backend logs for
`password_hash`, `argon2id$v=`, JWT-shaped strings, and
`Authorization: Bearer` and fails the verifier on any hit.

## Testing

`backend/tests/`:

* `test_password_hasher.py` — Argon2id correctness + edge cases.
* `test_jwt_security.py` — issue / decode / failure modes.
* `test_auth_service.py` — domain layer (in-memory SQLite).
* `test_auth_api.py` — HTTP layer with `TestClient`.
* `test_admin_users_api.py` — admin user management + RBAC.
* `test_rbac_routes.py` — Phase 1-4 routes under all roles.
* `test_migration_users.py` — schema + migration file.
* `test_secrets_not_logged.py` — log capture; assert no
  password / JWT / hash / Authorization.
* `test_auth_disabled_compat.py` — `AUTH_ENABLED=false` smoke
  tests covering Phase 0-4 endpoints.

The privileged-escalation negative test forges a JWT with
`role=ADMIN` for a VIEWER and asserts the request is rejected.

## Verification

```bash
bash scripts/phase5a_verify.sh
```

Delegates to `phase4_verify.sh` and adds the Phase 5A-specific
checks above.  Exits non-zero on any failure.

## Deferred (not in Phase 5A)

* Conversation / message persistence (Phase 5B).
* WebSockets (Phase 5C).
* OIDC / OAuth / SAML / Cognito / Google login.
* Refresh-token database and rotation.
* Email-based password reset.
* Multi-factor authentication.
* Distributed rate limiting (recommended for production but
  deferred to keep the Phase 5A footprint narrow).
* Audit-event database.

## Operational notes

* **Production**: set `AUTH_ENABLED=true`, generate a unique
  `JWT_SECRET` with `openssl rand -hex 32`, store it in your
  secret manager, inject it at runtime.
* **Development**: leave `AUTH_ENABLED=false` to keep the
  Phase 0-4 verification flow working without modification.
* **First admin**: run `scripts/create_admin.py` after enabling
  auth.  Never commit the resulting credentials to source
  control.
