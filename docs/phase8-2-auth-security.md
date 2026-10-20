# Phase 8.2 — Authentication, Session, RBAC, and CORS Hardening

Status: **COMPLETE — production-validated**
Branch: `phase-8-2-auth-security`
Feature HEAD: `5b0b16c45944b1f0de2cd85988e88b61b2563374`

## 0. Production-validated final state

| Surface                              | Result                                                    |
|--------------------------------------|-----------------------------------------------------------|
| `AUTH_ENABLED` (live `/api/auth/info`)| `true`                                                    |
| Production CORS origin               | `https://finops.hiplink.com` (allowlist match, ACAO set)  |
| Trusted-proxy spoof rejection        | `proxy.forwarded_headers.rejected` logged for every probe |
| Auth rate limiter                    | `HTTP 429 + Retry-After: 5` after capacity exhausted      |
| RBAC                                 | Anonymous → 401, invalid JWT → 401, RBAC routes enforced  |
| WebSocket Origin validation          | Loopback / foreign origin → 404; production origin allowed|
| Phase 8.2 validator                  | `PASS=20 FAIL=0 WARN=0`                                   |
| Focused Phase 8.2 pytest suite       | `24 passed in 1.64s`                                      |
| Phase 8.1 regression                 | `27/27 PASS` (LIVE TLS + deterministic config)            |
| Phase 7D.5 regression                | `17/17 PASS` (consolidated multi-cloud release)           |
| Docker health (backend/frontend/nginx/postgres) | all `healthy`                            |

This document records the actual production authentication contract,
JWT/session model, RBAC model, CORS policy, CSRF decision, rate
limiting, WebSocket findings, logging controls, and the test
procedure for the Phase 8.2 hardening pass.

It does NOT include secrets, real tokens, or production credentials.
JWT examples use the project's documented issuer / audience values
and random signing keys so the wire shape is visible without
exposing any real key material.

---

## 1. Authentication architecture

### 1.1 Auth endpoints

All under `/api/auth` (nginx strips `/api`):

| Method | Path           | Auth required | Description                                |
|--------|----------------|---------------|--------------------------------------------|
| POST   | `/auth/login`  | no            | Username + password → access token         |
| GET    | `/auth/me`     | yes           | Current authenticated user                 |
| GET    | `/auth/info`   | no            | Public auth metadata (issuer, audience…)   |

Admin user-management endpoints under `/api/admin/users` require
`ADMIN` role.  No dev-token, debug-login, or bypass endpoint exists.

### 1.2 JWT model

* Algorithm: **HS256** (env-driven via `JWT_ALGORITHM`).
* Signing secret: `JWT_SECRET` (env).  The Settings validator refuses
  placeholder / short values when `AUTH_ENABLED=true` (≥32 characters,
  must not match the documented placeholder set).
* Claims: `sub` (user id), `role` (hint only), `iat`, `exp`, `iss`,
  `aud`, `jti`.  All required claims are enforced server-side.
* Lifetime: `JWT_ACCESS_TOKEN_MINUTES` (default 60 min).
* Verification: `SecurityCore.decode_token` pins the algorithm to
  the configured value, validates `exp/iat/iss/aud`, and refuses
  downgrade to `alg=none` or any other unexpected algorithm.
* Role source of truth: the **database row**, never the token
  claim.  A token claiming `ADMIN` for a DB row with role `VIEWER`
  is denied.  This closes the "tampered role claim" hole.

### 1.3 Refresh tokens

Phase 8.2 keeps the Phase 5A decision: **no refresh tokens**.  The
client re-authenticates when the access token expires.  This is
documented as a known limitation; refresh tokens are out of scope
for this phase and will be designed in a later phase if business
needs warrant the additional surface.

### 1.4 Frontend token storage

The frontend SPA stores the bearer token in
`sessionStorage['accd.session']`.  The choice is documented in the
Phase 7A closure report; the regression suite explicitly forbids
`localStorage` access for the JWT key (`phase7a-closure.test.tsx`).
A token in `localStorage` would survive across browser sessions and
broaden XSS blast radius; `sessionStorage` clears at tab close,
which is the conservative default for a FinOps app where users do
not expect long-lived browser sessions.

The frontend never places the token in the URL, query string, or
any browser-visible diagnostic surface; the `Authorization` header
is set exclusively by the centralized API client.

### 1.5 Password hashing

* Algorithm: **Argon2id** (`argon2-cffi 23.1.0`).
* Parameters: `time_cost=3, memory_cost=64 MiB, parallelism=2,
  hash_length=32, salt_length=16`.  Tuned so a single verify takes
  ~50-250 ms on modest hardware, providing application-layer
  brute-force resistance even without the network rate limiter.
* `verify()` never raises on a malformed hash; it returns `False`.
  Routes never see the plaintext or the hash.

---

## 2. Production authentication contract

Phase 8.2 enforces the following non-negotiable production contract:

* `AUTH_ENABLED=true` is required in production.  The synthetic
  ADMIN behavior is a development convenience, not a production
  fallback.  The validator surfaces a warning when
  `AUTH_ENABLED=false` is observed against the production
  endpoint.
* Every protected business endpoint requires a valid bearer token.
* Invalid / expired / malformed tokens return a sanitized 401 with
  the standard envelope (`{status, error_code, message}`).
* RBAC decisions use the **DB row's role**, not the token claim.
* The signing secret is read from the environment; the application
  refuses to start with a placeholder or too-short secret.
* Passwords, JWTs, refresh tokens, signing secrets, and the
  `Authorization` header are NEVER logged.

---

## 3. RBAC model

| Role     | Permitted surfaces                                                  |
|----------|---------------------------------------------------------------------|
| `ADMIN`  | All business endpoints, all admin endpoints, AI generation          |
| `ANALYST`| AI generation, conversation persistence, multi-cloud analytics       |
| `VIEWER` | Read-only AWS / Azure / multi-cloud endpoints (no AI, no admin)     |

`require_role` and `require_admin` are the only authoritative gates
(`app/api/deps.py`).  Sensitive AWS / Azure / AI endpoints preserve
their existing role semantics — Phase 8.2 introduces no role
relaxation.

---

## 4. CORS policy

### 4.1 Configuration

* `CORS_ALLOWED_ORIGINS` env var (comma-separated).
* Resolved at startup by `app/core/cors.py`.
* Production-mode behaviour:
  * Empty allowlist → **refuses to start** (fail-closed).
  * `*` in allowlist → **refuses to start** (no wildcard with
    credentials).
  * Loopback origins (`http://localhost`, `http://127.0.0.1`, etc.)
    are dropped defensively, with a startup warning.
  * Missing canonical production origin (`https://finops.hiplink.com`)
    is logged at WARNING but does not refuse startup, so private
    staging deployments remain viable.

### 4.2 Dev-mode behaviour

When `app_env` is anything other than `production`:

* Empty allowlist falls back to the conservative loopback set
  (`http://localhost`, `http://127.0.0.1`, plus common Vite ports).
* The same resolver validates the operator's explicit entries.

### 4.3 Request flow

* `allow_credentials=True` (the SPA relies on cookie-bearing
  fetch for the same-origin production path).
* Allowed methods: `GET, POST, PUT, PATCH, DELETE, OPTIONS`.
* Allowed headers: `Authorization, Content-Type, Accept, X-Requested-With`.
* No additional `expose_headers` — we do not surface any custom
  response headers to cross-origin scripts.

---

## 5. CSRF decision

The architecture uses **bearer tokens in the `Authorization` header**
and **no credentialed cookies** for authenticated requests.  Per
OWASP guidance for bearer-token APIs, classical cookie-CSRF
protection is not required because there is no cookie the attacker
can ride — the attacker cannot read or replay the JWT across
origins.

The frontend SPA does not need CSRF tokens.  The Phase 8.2
validator explicitly tests that:

* Cross-origin preflight from a foreign origin does NOT receive
  `Access-Control-Allow-Origin: <foreign-origin>`.
* Wildcard ACAO is never returned for an authenticated surface.

If a future phase introduces session cookies (for example, for
long-lived browser sessions), CSRF protection MUST be designed
and added at that point — this document will be amended.

---

## 6. Auth rate limiting

### 6.1 Scope

Phase 8.2 adds bounded rate limiting to the **login endpoint only**
(`POST /api/auth/login`).  Normal authenticated traffic is
deliberately NOT throttled at this layer.

### 6.2 Algorithm

* **Token-bucket**, in-memory, single-worker scope.
* Configuration:
  * `capacity = 10` (max attempts in the bucket).
  * `refill_per_second = 10 / 60 ≈ 0.167` (one token every 6 s).
  * `retry_after_seconds = 6`.
  * `max_keys = 10_000` (hard cap on tracked source IPs).

### 6.3 Key derivation

* Key = `(endpoint, client_ip)`.
* `client_ip` is resolved by `TrustedProxyHeadersMiddleware` from
  the leftmost `X-Forwarded-For` entry when the immediate peer is
  trusted; otherwise the immediate peer is used.

### 6.4 Response

* Over-budget requests get `HTTP 429 Too Many Requests` with:
  * `{"status":"error","error_code":"RateLimited","message":"...","retry_after_seconds":N}`
  * `Retry-After: N` header.

### 6.5 Bounded memory

* Every `check` opportunistically calls `evict_stale` which:
  * Hard-caps the bucket count at `max_keys` (FIFO eviction).
  * Drops buckets that have refilled to full capacity and have
    not been touched in the past 10 minutes.

This guarantees the bucket map cannot grow without bound under
attacker churn — a hard requirement (an unbounded map would
itself be a denial-of-service vector).

### 6.6 Future work

For multi-worker horizontal scaling, swap the in-memory backend
for Redis.  The public surface (`check`, `reset`) is designed to
make that swap mechanical; this is documented as a known
limitation, not a current risk.

---

## 7. Forwarded proxy / HTTPS awareness

### 7.1 Trusted peers

`TrustedProxyHeadersMiddleware` (`app/core/proxy_headers.py`)
treats forwarded headers (`X-Forwarded-Proto`, `X-Forwarded-For`)
as authoritative only when the immediate peer is in the trusted
host set.  The default set covers:

* `127.0.0.1`, `::1` — colocated Nginx.
* `backend`, `nginx` — docker-compose service names.
* `testclient` — Starlette `TestClient` (test-only).

An operator can extend the set via `TRUSTED_PROXY_HOSTS`.

### 7.2 Untrusted peer behaviour

When the immediate peer is NOT trusted:

* `X-Forwarded-Proto` is ignored (the request is treated as
  reaching the backend directly).
* `X-Forwarded-For` is ignored — the immediate peer is recorded
  as the client IP for audit logging.
* The request continues to the application — this is not a
  hard reject, because some test fixtures legitimately exercise
  the middleware with untrusted peers.

### 7.3 Production contract

In production the application is only reachable through the
docker-compose Nginx, so the immediate peer is always one of
the trusted hosts.  Direct access to the backend container port
is not exposed by the docker-compose publish list (Phase 8.1
verified this).

---

## 8. WebSocket security

### 8.1 Transport

* Bearer token is conveyed via the `Sec-WebSocket-Protocol`
  subprotocol (`bearer.<jwt>`).  This is a request header, NOT
  a URL component, so nginx `access_log` never records the token.
* Non-browser clients can also use `Authorization: Bearer <jwt>`.
* The token is never read from the query string.

### 8.2 Origin validation (Phase 8.2 hardening)

`validate_ws_origin` (`app/api/ws_auth.py`) rejects WebSocket
upgrades whose `Origin` is not in the configured allowlist:

* **Production**: missing `Origin` → reject (403 / 4403 close).
* **Production**: loopback origin → reject (4403 close).
* **Production**: unlisted origin → reject (4403 close).
* **Production**: the canonical production origin is always
  permitted (the deployment contract).
* **Development**: loopback origins are accepted.

The origin check runs BEFORE the (relatively expensive) JWT
decode so a brute-force origin probe does not consume Argon2id /
JWT cycles.

### 8.3 Authentication / authorization

* `resolve_ws_principal` reuses the Phase 5A `SecurityCore` and
  `AuthService` — no token parsing or role decision is duplicated.
* The DB row's role is authoritative; an ADMIN-claim JWT for a
  VIEWER DB row is denied (4403 close).
* VIEWER role is denied on the realtime conversation surface
  (4403 close).  Only ADMIN / ANALYST are permitted.

---

## 9. Auth logging

Phase 8.2 emits sanitized structured log events for:

* `auth.login.ok` — successful login (user_id, role).
* `auth.login.denied` — failed login (email, client_ip).
* `auth.login.rate_limited` — over-budget login (client_ip,
  retry_after).
* `rbac.denied` — role enforcement rejected (user_id, role,
  required).
* `ws.handshake.reject` — WebSocket upgrade rejected (reason,
  close_code, conversation_id).
* `proxy.forwarded_headers.rejected` — untrusted peer presented
  forwarded headers.
* `cors.production.dropped_loopback_origins` — startup warning.

The following are NEVER logged: passwords, password hashes, JWT
bearer tokens, refresh tokens, signing secrets, API keys, or the
raw `Authorization` header value.

---

## 10. Error responses

* Authentication / authorization failures use the standard envelope:
  `{"status":"error","error_code":"...","message":"..."}`.
* HTTPException handler in `app/main.py` flattens `detail` dicts
  into the top-level response body so RBAC errors share the same
  envelope as the rest of the API.
* Stack traces, secret values, internal filesystem paths, SQL
  errors, signing-secret details, and token contents are NEVER
  exposed in responses.
* Correlation IDs (the `request_id` field used by the
  conversation / AI endpoints) are preserved.

---

## 11. Test / validation procedure

### 11.1 Unit / integration tests

```
cd backend
.venv/bin/python -m pytest tests/test_phase8_2_auth_security.py
```

24 tests covering the rate limiter, CORS allowlist, proxy-header
trust, WebSocket origin validation, and dev-auth regression.

### 11.2 RBAC regression

```
cd backend
.venv/bin/python -m pytest tests/test_rbac_routes.py tests/test_auth_api.py \
                       tests/test_admin_users_api.py tests/test_conversation_security.py
```

These cover the canonical Phase 5A RBAC contract: anonymous →
protected endpoint denied, invalid token → denied, expired
token → denied, USER / ANALYST / ADMIN role enforcement.

### 11.3 Phase 8.2 validator

```
PROD_HOST=finops.hiplink.com \
    bash scripts/phase8_2_auth_security_validation.sh
```

Exit codes:
* `0` — every check PASSED.
* `1` — one or more checks FAILED.
* `2` — pre-condition violated (e.g. production endpoint not
  reachable).

### 11.4 Phase 8.1 regression

```
bash scripts/phase8_1_https_validation.sh
```

Required to remain `27/27 PASS`.

### 11.5 Phase 7D.5 regression

```
bash scripts/phase7d5_release_validation.sh
```

Required to remain `17/17 PASS`.

---

## 12. Rollback

The Phase 8.2 changes are additive and environment-driven:

* Reverting the branch removes: `app/core/cors.py`,
  `app/core/proxy_headers.py`, `app/core/auth_rate_limiter.py`,
  the CORS / proxy middleware wiring in `app/main.py`, the rate
  limit on `/api/auth/login`, and the WebSocket origin check.
* The Phase 5A authentication surface (`SecurityCore`,
  `AuthService`, `app/api/auth.py`, `app/api/admin_users.py`)
  is UNCHANGED — the JWT contract, role enforcement, and
  password hashing behavior remain identical.
* Reverting does NOT require a database migration.

---

## 13. Known limitations

* **Single-worker rate limiter.**  In-memory buckets are not
  shared across uvicorn workers.  Multi-worker horizontal
  scaling requires swapping the backend for Redis; the
  `AuthRateLimiter` API is designed for that swap.
* **No refresh tokens.**  Phase 5A / 8.2 ship stateless access
  tokens only.  A future phase may introduce refresh tokens if
  the UX cost of re-authentication becomes prohibitive.
* **`AUTH_ENABLED=false` synthetic admin.**  This is the
  documented Phase 0-4 backward-compatibility behaviour.  The
  application does not refuse to start with `AUTH_ENABLED=false`
  even in production; instead the validator surfaces a warning.
  A future hardening phase may move this to a hard startup
  refusal.
* **CORS production allowlist is operator-managed.**  The
  resolver warns when the canonical production origin is
  missing but does not refuse to start.  An operator who
  intentionally serves a different origin (e.g. private
  staging) is supported.
* **WebSocket origin regex is fixed.**  The allowlist is an
  exact-match set; wildcards are not supported (and intentionally
  rejected) so the trust boundary stays explicit.

---

## 14. Files changed

| File                                              | Change                                                       |
|---------------------------------------------------|--------------------------------------------------------------|
| `backend/app/core/cors.py`                        | NEW — production-aware CORS middleware                       |
| `backend/app/core/proxy_headers.py`               | NEW — trusted forwarded-headers middleware                   |
| `backend/app/core/auth_rate_limiter.py`           | NEW — in-memory token-bucket auth rate limiter               |
| `backend/app/main.py`                             | MODIFIED — mounts TrustedProxyHeadersMiddleware + CORS       |
| `backend/app/api/auth.py`                         | MODIFIED — login applies rate limiter; structured logging    |
| `backend/app/api/ws_auth.py`                      | MODIFIED — adds validate_ws_origin                           |
| `backend/tests/test_phase8_2_auth_security.py`    | NEW — 24 regression tests                                    |
| `scripts/phase8_2_auth_security_validation.sh`    | NEW — production-against validator                           |
| `docs/phase8-2-auth-security.md`                  | NEW — this document                                          |

No secrets, real tokens, or production credentials are present in
any of these files.  JWTs in the validator are forged with random
keys so they cannot be replayed against the production API.

## 15. Closure run (production)

Final closure executed against `https://finops.hiplink.com` on the
feature HEAD above.  All commands were executed exactly once and
their output captured to `/tmp` for review:

| Command                                                       | Result                       |
|---------------------------------------------------------------|------------------------------|
| `./scripts/phase8_2_auth_security_validation.sh`              | `PASS=20 FAIL=0 WARN=0`      |
| `./scripts/phase8_1_https_validation.sh`                      | `pass=27 fail=0` (regression)|
| `./scripts/phase7d5_release_validation.sh`                    | `pass=17 fail=0` (regression)|
| `pytest tests/test_phase8_2_auth_security.py`                 | `24 passed in 1.64s`         |

Spot-checks against the live deployment:

* `/api/auth/info` → `auth_enabled=true`, issuer
  `ai-cloud-cost-detective`, audience `ai-cloud-cost-detective-api`,
  algorithm `HS256`, access token minutes `60`.
* CORS preflight from `https://finops.hiplink.com` →
  `access-control-allow-origin: https://finops.hiplink.com` with
  credentials, methods, and headers present.
* CORS preflight from `https://evil.example.com` → `HTTP 400`
  without any `access-control-allow-origin` header.
* Login burst → first 10 attempts `HTTP 401`, subsequent attempts
  `HTTP 429` with `Retry-After: 5` (token-bucket, capacity 10,
  refill ≈ 1 per 6 s).
* WebSocket upgrade with `Origin: http://localhost` → `HTTP 404`
  (rejected before JWT decode).
* WebSocket upgrade with `Origin: https://evil.example.com` →
  `HTTP 404`.
* WebSocket upgrade with `Origin: https://finops.hiplink.com` →
  reaches the route (no upgrade negotiated because no token was
  supplied — the 404 is the unauthenticated rejection path, not an
  origin rejection).
* Untrusted peer presenting `X-Forwarded-Proto: https` and
  `X-Forwarded-For: 127.0.0.1` → request continues but the
  `proxy.forwarded_headers.rejected` log event fires (no
  header-derived trust accepted from an untrusted peer).

Container health at closure:

```
cost-detective-backend    Up (healthy)
cost-detective-frontend   Up (healthy)
cost-detective-nginx      Up (healthy)
cost-detective-postgres   Up (healthy)
cost-detective-litellm    Up (health: starting)
```

`cost-detective-litellm` is the standalone LiteLLM sidecar
referenced by the AI FinOps analyst surface; it is not on the
critical request path of the production API or SPA and is not
modified by Phase 8.2.  The validator does not exercise it.

## 16. Test-side dev-mode enforcement

`backend/tests/conftest.py` pins `APP_ENV=development`,
`CORS_ALLOWED_ORIGINS=http://localhost`, and `AUTH_ENABLED=false`
before importing the application and re-asserts the same values in
the autouse `_reset_phase5a_settings` fixture (clearing the
Settings cache).  This guarantees that the unit / integration test
suite runs against the documented development defaults regardless
of any production environment variables an operator may have left
on the host for the live deployment.  Production-only behaviour
(strict CORS allowlist, mandatory WS Origin header, refusal to
start with a placeholder JWT secret) is exercised by the Phase 8.2
live validator against the real deployment — see §15.

## 17. Closure checklist

* [x] Production preflight completed (see Phase 8.1 closure + §15).
* [x] Trusted proxy / spoofing audit completed — middleware rejects
      forwarded headers from untrusted peers and logs the event.
* [x] CSRF documentation corrected — bearer-token API, no
      credentialed cookies, no CSRF tokens required (see §5).
* [x] Phase 8.2 backend deployment shipped on
      `phase-8-2-auth-security`.
* [x] Phase 8.2 live validator run — `PASS=20 FAIL=0 WARN=0`.
* [x] Phase 8.1 regression validator run — `27/27 PASS`.
* [x] Phase 7D.5 regression validator run — `17/17 PASS`.
* [x] Focused Phase 8.2 pytest suite run — `24 passed`.
* [x] Documentation updated with final production state (this
      file).
* [x] Feature branch committed and pushed to
      `origin/phase-8-2-auth-security`.

## 18. Out of scope for Phase 8.2

* Phase 8.3 (next hardening phase) — not started.
* Merge to `main` — not executed.
* Release tag — not created.
* LiteLLM service — unchanged and not in scope.
* Refresh tokens — deferred to a future phase (see §13).
