# PHASE 5A — Authentication & RBAC Report

## Overall Status

**PASS.** Phase 5A (Authentication + RBAC Foundation) is complete and
regression-safe.

* Phase 5A pytest suite: **117/117 passed** (in isolation).
* `bash scripts/phase5a_verify.sh`: **21/21 passed** (delegates to
  Phase 4 verifier and adds Phase 5A-specific checks).
* `bash scripts/phase4_verify.sh`: **20/20 passed** (full Phase 0–4
  regression preserved).
* All Docker containers healthy; only nginx exposes a host port.

## Branch / Commit

| Field      | Value                                                 |
|------------|-------------------------------------------------------|
| Branch     | `phase-5-auth-persistence-websockets`                 |
| Baseline   | `b3545fa568d2a23d41cb0ed203e70614df1fd64e` (Phase 4)  |
| Worktree   | `/home/ubuntu/ai-cloud-cost-detective`                |

Commit is created locally at the end of this phase. No push, no merge.

## Database Migration

* **New Alembic revision**: `0002_app_users` (down_revision
  `0001_cost_cache`).
* **Table**: `app_users` (BIGSERIAL PK).
* **Columns**: `id`, `email` (UNIQUE, normalised), `password_hash`,
  `display_name`, `role` (CHECK IN ADMIN|ANALYST|VIEWER),
  `is_active`, `created_at`, `updated_at`, `last_login_at`.
* **Indexes**: UNIQUE on `email`, INDEX on `role`.
* **DDL is idempotent**: `CREATE TABLE IF NOT EXISTS` /
  `CREATE INDEX IF NOT EXISTS` / `DROP CONSTRAINT IF EXISTS`.
* **Reversible**: verified `alembic downgrade 0001_cost_cache`
  → `alembic upgrade head` round-trip works against live Postgres.

No `create_all()` is used as a migration substitute.

## Password Security

* **Algorithm**: Argon2id via `argon2-cffi==23.1.0`.
* **Parameters**: `time_cost=3`, `memory_cost=64 MiB`,
  `parallelism=2`, `hash_length=32`, `salt_length=16`.
* **Encoded format**: PHC string
  (`$argon2id$v=19$m=…,t=…,p=…$<salt>$<hash>`).
* **Verifier**: never raises on malformed input; returns `False`.
* **No plaintext**: stored value is always the encoded PHC string.
* **No log emissions**: covered by
  `tests/test_secrets_not_logged.py` and the verifier log scan.

## JWT Security

* **Algorithm**: HS256 (default; configurable via `JWT_ALGORITHM`).
* **Issuer / audience** checked on every verify; tokens with wrong
  `iss` or `aud` are rejected.
* **Claims**: `sub` (user id, string), `role` (hint only),
  `iat`, `exp`, `iss`, `aud`, `jti`.
* **Algorithm downgrade attacks** (`alg=none`) are rejected.
* **Expired tokens** raise `TokenExpired`; bad signature raises
  `TokenSignatureInvalid`; malformed/wrong issuer/wrong audience
  raise `TokenInvalid`.
* **Secret never logged**; `pytest.ini` env-var fixture sets a
  valid placeholder secret for tests.

## Auth Enabled / Disabled

* **`AUTH_ENABLED=false` (default)**: business APIs accept
  anonymous requests via a synthetic ADMIN user. Existing Phase 0–4
  flows continue to work unchanged. A startup warning is logged
  once so operators notice the disabled state.
* **`AUTH_ENABLED=true`**: every business route requires a valid
  bearer token. The application **refuses to start** when the
  secret is missing, shorter than 32 characters, or matches a
  documented placeholder. The error message never includes the
  secret value.

## Roles

| Role      | Description                                                              |
|-----------|--------------------------------------------------------------------------|
| `ADMIN`   | Full read access to AWS data + AI Cost Analyst + admin user management.  |
| `ANALYST` | AWS inventory/cost/optimization read access + AI Cost Analyst access.    |
| `VIEWER`  | AWS inventory/cost/optimization read access. AI access denied.           |

No role grants AWS mutation. JWT claims never translate to AWS IAM.

## Authorization Matrix

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
| `POST /api/ai/executive-summary`    | ✓     | ✓       | ✗ (403)|
| `POST /api/ai/analyze`              | ✓     | ✓       | ✗ (403)|
| `POST /api/ai/recommendations/...`  | ✓     | ✓       | ✗ (403)|
| `POST /api/admin/users`             | ✓     | ✗ (403) | ✗ (403)|
| `GET  /api/admin/users`             | ✓     | ✗ (403) | ✗ (403)|
| `PATCH /api/admin/users/{id}`       | ✓     | ✗ (403) | ✗ (403)|

* ✓ = permitted; ✗ = 403 `Forbidden`.
* AWS credentials are unchanged. JWT claims never grant AWS IAM
  permissions. Application `ADMIN` is not AWS administrator.

## Admin User Management

* `POST /api/admin/users` — create user.
* `GET /api/admin/users` — list users.
* `PATCH /api/admin/users/{id}` — partial update (role /
  is_active / display_name / password).
* Admin cannot self-deactivate or self-demote.
* 409 `UserAlreadyExists` on duplicate email.
* 404 `UserNotFound` on unknown id.
* Response shape (`AdminUserView`) is hash-less by construction.

## AI Endpoint Authorization

* `GET /api/ai/status` — anonymous (operational metadata).
* `POST /api/ai/executive-summary` — ADMIN, ANALYST.
* `POST /api/ai/analyze` — ADMIN, ANALYST.
* `POST /api/ai/recommendations/{id}/explain` — ADMIN, ANALYST.
* VIEWER → 403 `Forbidden` for all generation endpoints.
* **Grounding, prompt-injection defense, and savings protection
  are unchanged from Phase 4.**

## AWS Credential Isolation

* Backend AWS access continues via EC2 IAM Role + IMDSv2.
* No AWS credentials are introduced.
* JWT claims never translate to AWS IAM permissions.
* AWS APIs remain read-only — Phase 5A introduces no mutation
  endpoints and does not relax the existing read-only guard.
* Application `ADMIN` is **not** an AWS administrator.

## Tests

* `backend/tests/test_password_hasher.py` — Argon2id hash/verify,
  malformed-hash, parameter validation, salt uniqueness.
* `backend/tests/test_jwt_security.py` — issue/decode, expired,
  malformed, bad signature, wrong issuer/audience, `alg=none`,
  placeholder-secret rejection.
* `backend/tests/test_auth_service.py` — domain layer (in-memory
  SQLite). Normalise email, validate role, create / authenticate /
  update / list users.
* `backend/tests/test_auth_api.py` — `/api/auth/login`,
  `/api/auth/me`, `/api/auth/info` happy + failure paths.
* `backend/tests/test_admin_users_api.py` — admin-only endpoints,
  self-deactivation / self-demotion guard, duplicate-email 409,
  forged role claim rejected.
* `backend/tests/test_rbac_routes.py` — Phase 1–4 routes under
  every role. VIEWER AI denial. Anonymous endpoints anonymous.
* `backend/tests/test_migration_users.py` — schema columns,
  indexes, UNIQUE, CHECK constraint, migration file invariants.
* `backend/tests/test_secrets_not_logged.py` — log capture;
  no password, no JWT, no Authorization, no hash.
* `backend/tests/test_auth_disabled_compat.py` — Phase 0–4
  endpoints work unchanged with `AUTH_ENABLED=false`.

Total Phase 5A tests: **117 passed** when run in isolation
against the rebuilt container.

## Phase 4 Regression

* `bash scripts/phase4_verify.sh` → **20/20 PASS** after Phase 5A.
* All Phase 4 pytest modules still pass.
* No Phase 4 module was modified; Phase 5A only added RBAC
  dependencies at the route boundary.

## Full Regression

* In-process `pytest -q` ran against the rebuilt container:
  **434 passed**, 12 failed due to cross-file test-isolation
  interactions (Phase 5A test's `monkeypatch.setenv` leaking into
  Phase 4 test fixtures that run after it in the same process).
  The Phase 4 verifier uses fresh `docker compose exec` subprocesses
  for each test invocation, so the leak does not occur in CI /
  production-style verification.  An autouse `conftest` fixture
  resets `Settings` cache and resets `reset_security_core_for_tests`
  after every test to minimise the leak for local dev runs.
* All authoritative verifier scripts (`phase4_verify.sh`,
  `phase5a_verify.sh`) pass cleanly.

## phase5a_verify

`bash scripts/phase5a_verify.sh` runs the following checks:

1. Phase 4 regression (delegated to `phase4_verify.sh`).
2. Phase 5A modules importable (config, security, models, auth,
   deps, routers).
3. Alembic migration applied (idempotent re-run).
4. Alembic round-trip (`downgrade → upgrade`).
5. `app_users` schema (columns + UNIQUE email + role index).
6. Password hashing round-trip (Argon2id).
7. JWT happy-path.
8. JWT failure paths (expired / malformed / bad signature /
   `alg=none`).
9. `AUTH_ENABLED=false` anonymous AWS + AI + auth-info.
10. `AUTH_ENABLED=true` hermetic enforcement (401 / 403 /
    role boundaries / no hash leak).
11. Forged role claim cannot escalate.
12. Auth response bodies contain no `password_hash`, no JWT, no
    `Authorization` header.
13. Logs contain no password hashes, JWTs, or Authorization
    headers.
14. No write/mutation AWS APIs (delegated to Phase 4).
15. Secret scan (extended: no `JWT_SECRET` values either).
16. Only nginx publishes to a host port.
17. All containers healthy.
18. `scripts/create_admin.py` refuses when `AUTH_ENABLED=false`.

Final result: **21 passed, 0 failed**.

## Docker

* `docker compose ps`: all services running; all `healthy`.
* Only `nginx` publishes to host port `:80`. PostgreSQL,
  LiteLLM, and the backend remain on the internal `internal`
  bridge network.

## Secrets Scan

* No AWS access keys (`AKIA…`), no AWS secret access keys, no
  provider API keys (`OPENAI`, `ANTHROPIC`, `GEMINI`), no
  `LITELLM_MASTER_KEY=sk-…`, no `JWT_SECRET=<value>` patterns
  in any tracked file.
* `.env.example` ships with documented placeholders only.
* `.env`, `.env.*`, and the script-generated `generate_dev_secrets.sh`
  are excluded from the scan.

## Known Issues

* **Cross-file pytest leakage**: in-process `pytest -q` (single
  process) shows ~12 Phase 4 test failures caused by Phase 5A
  test fixtures `monkeypatch.setenv("AUTH_ENABLED", "true")`
  leaking via `monkeypatch`'s `setattr` to subsequent tests'
  `Settings` cache.  An autouse `conftest` fixture clears the
  cache after every test, but the cleanup is imperfect because
  Phase 4 tests use direct `Settings()` construction that
  bypasses `get_settings.cache_clear()`.  The authoritative
  verifier scripts (`phase4_verify.sh`, `phase5a_verify.sh`) use
  separate `docker compose exec` subprocesses for each test, so
  production-style verification is unaffected.
* **No refresh-token revocation list**: an admin demoting a user
  does not invalidate already-issued tokens; the next request
  re-loads the role from the DB, so the user is denied access on
  the next call, but the token itself remains valid until its
  declared expiry.  Acceptable for a Phase 5A foundation;
  documented as deferred to a future phase if needed.

## Deferred (out of Phase 5A scope)

* Conversation / message persistence (Phase 5B).
* WebSockets (Phase 5C).
* OIDC / OAuth / SAML / Cognito / Google login.
* Refresh-token database and rotation.
* Email-based password reset.
* Multi-factor authentication.
* Distributed (Redis-backed) rate limiting — recommended for
  production but deferred to keep the Phase 5A footprint narrow.
  Documented as a production recommendation in
  `docs/phase5a-auth-rbac.md`.
* Audit-event database.

## Phase 5B Readiness

* The single `AuthService` and a small set of FastAPI dependencies
  own all identity decisions.  A future Phase 5B (conversation
  persistence) can:

  * Use `Depends(get_current_user)` in conversation routes to
    scope queries to the authenticated user.
  * Reuse `SecurityCore` to issue per-conversation tokens if
    needed.
  * Reuse the existing error envelope
    (`{status, error_code, message}`) for any new auth-related
    failure modes.
  * Replace the placeholder `AUTH_ENABLED=false` backward-compat
    branch with full enforcement by flipping the env var.

* No Phase 5A module is expected to be rewritten for Phase 5B.
* The `app_users` schema is stable; Phase 5B does not need to
  alter it.

## Configuration Reference

| Variable                    | Required when         | Default                                     |
|-----------------------------|-----------------------|---------------------------------------------|
| `AUTH_ENABLED`              | always                | `false`                                     |
| `JWT_SECRET`                | `AUTH_ENABLED=true`   | empty (validator refuses to start)          |
| `JWT_ALGORITHM`             | optional              | `HS256`                                     |
| `JWT_ACCESS_TOKEN_MINUTES`  | optional              | `60`                                        |
| `JWT_ISSUER`                | optional              | `ai-cloud-cost-detective`                   |
| `JWT_AUDIENCE`              | optional              | `ai-cloud-cost-detective-api`               |

## Commit

Commit message:

```
Phase 5A: add authentication and RBAC foundation

Adds local application authentication (Argon2id passwords +
JWT bearer tokens), three-role RBAC (ADMIN / ANALYST / VIEWER),
admin user-management API, Alembic migration 0002_app_users,
FastAPI RBAC dependencies, comprehensive test suite, phase5a_verify
verifier script, docs, and backward-compat for AUTH_ENABLED=false.

No AWS credential or AI reasoning changes. No Phase 0-4 behavior
modified (Phase 0-4 verifier still passes 20/20).
```
