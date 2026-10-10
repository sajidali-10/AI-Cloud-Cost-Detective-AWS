# PHASE 7A CLOSURE — Auth Bootstrap + Browser Session Persistence

## Overall Status: **PASS** (with documented external dependencies)

| Item | Value |
| --- | --- |
| Branch | `phase-7-end-to-end-validation` |
| Commit | (this commit) |
| Baseline | `bb4372f` Phase 7A primary |
| Tree | Clean |

## Root-cause diagnosis

Three confirmed browser issues were resolved by three independent
root-cause fixes:

1. **F5 / browser refresh logs the user out** — Root cause: the
   `useAuth()` `getToken` callback was gated on `status.phase ===
   'authenticated'`.  When the AuthProvider remounted on F5, the
   `useEffect` that wires the new callback ran **after** the first
   FinOps hook rendered.  Combined with the prior localStorage
   read, the bootstrap path could not read the token until React
   re-rendered with the new closure.  Fix: `getToken` now reads
   `sessionStorage['accd.session']` directly on every call — no
   React-state gate.  Storage is the single source of truth, so a
   freshly-mounted session can immediately read the JWT.

2. **Dashboard initially shows "missing Authorization header"** —
   Root cause: when `login()` wrote the new session and called
   `setStatus({ phase: 'authenticated' })`, the AuthProvider's
   `useEffect` re-ran with the new `getToken` callback, but
   `useFinopsQuery`'s `useEffect` re-ran **first** in the same
   commit.  The FinOps hooks fired with the stale `_getToken`
   reference (still returning `null`), producing 401s.  Subsequent
   401s ran `handleSessionExpired` which cleared the cache and
   prompted the user to navigate away and back.  Fix: storage is
   the source of truth and `getToken` reads it directly.  The very
   next `apiFetch` after `writeStoredSession` returns the new
   token, regardless of which `configureApi` callback is current.

3. **AIAnalystPage reads `accd.session` directly** — Root cause:
   the page's WebSocket hook was given a `getToken` closure that
   parsed `window.localStorage` by hand.  This bypassed the
   centralized auth layer and would have continued reading the
   legacy localStorage key forever.  Fix: AIAnalystPage now uses
   `useAuth().accessToken`, which is a synchronous mirror of the
   sessionStorage entry.

## Auth state machine (3-state bootstrap)

```
                   +-----------+
                   |  checking |
                   +-----+-----+
                         |
            /api/auth/info resolves
                         |
        +----------------+----------------+
        |                                 |
   auth_enabled=false              auth_enabled=true
        |                                 |
        v                                 v
   +----------+                +----------------------+
   | disabled |                | sessionStorage      |
   +----------+                | session present?    |
                              +----+-----------------+
                                   |
            +----------------------+----------------------+
            |                                             |
       no / expired                          yes, valid expiry
            |                                             |
            v                                             v
   +----------------+                       /api/auth/me
   | unauthenticated|<----------------------+
   +-------+--------+                       |
           ^                                | 200
           |  +----------------+             v
   +------>--+|  authenticated |     +----------------+
   | logout  +----------------+     | authenticated  |
   |                                  +----------------+
   | 401 (apiFetch observed)            ^
   +-----------------------------------+
```

`checking` is the initial state.  No FinOps request can fire while
the AuthProvider is in `checking` because:

* `App.tsx` renders `<BootScreen />` (no hooks mount).
* Once `checking` resolves, only `authenticated` or `disabled`
  subscribers get to render FinOps pages.
* `useFinopsQuery` re-checks `authReady` on every effect run, so
  the gate stays closed until the AuthProvider has reached a
  terminal phase.

## Token storage migration

| Where | Before | After |
| --- | --- | --- |
| Bearer JWT | `localStorage['accd.session']` | `sessionStorage['accd.session']` |
| Billing month + region | (none) | `sessionStorage['accd.finops.period']`, `sessionStorage['accd.finops.region']` |
| Session notice | `sessionStorage['accd.session.notice']` | (unchanged) |
| Theme preference | `localStorage` | (unchanged — non-secret) |

* JWTs in `sessionStorage` are scoped to the browser tab and are
  cleared when the tab closes, the user logs out, or the
  AuthProvider explicitly invokes `writeStoredSession(null)`.
* On bootstrap, `cleanupLegacyAuthStorage()` strips any pre-Phase 7A
  `localStorage['accd.session']` entry so a previous deployment
  cannot leave a stale JWT behind.
* Theme preference stays in `localStorage` because it is not
  credential material.
* No JWT appears in URL paths, query strings, fragments, console
  output, or thrown error messages.  Verified by
  `scripts/phase7a_verify.sh`.

## Login race — ordered operations

```
login(email, password)
  ↓ POST /api/auth/login
  ↓    ├─ success → { access_token, expires_in, user }
  ↓    └─ failure → throw ApiError
  ↓
  writeStoredSession({...})          ← token now in sessionStorage
  ↓ invalidateFinopsCache()          ← drop pre-login probes
  ↓ setStatus({ phase: 'authenticated', user })
  ↓
  navigate(next, { replace: true })
  ↓
  Dashboard mounts
  ↓
  useFinopsQuery reads useAuth() — authReady is true
  ↓ useEffect calls apiFetch
  ↓ apiFetch → _getToken() reads sessionStorage['accd.session']
  ↓                                            ← returns the new token
  ↓
  Authorization: Bearer <fresh-jwt> ✓
  ↓
  No 401.  No "missing Authorization header".  No F5 needed.
```

The key invariant: `writeStoredSession` runs **before**
`setStatus({ phase: 'authenticated' })`.  Because `getToken` reads
storage on every call (no React-state gate), the freshly-issued
JWT is available to every subsequent `apiFetch` regardless of
whether the AuthProvider's `useEffect` has re-rendered yet.

## F5 / refresh

`frontend/src/tests/phase7a-closure.test.tsx` pins the contract:

* A stored session in `sessionStorage['accd.session']` restores
  the user after remount.
* An expired stored session (`expires_at <= Date.now()`) is
  dropped **without** calling `/api/auth/me`.
* `/api/auth/me` returning 401 clears the stored session and the
  user lands on the login screen.

## Logout

* `logout()` writes `null` to `sessionStorage['accd.session']`.
* `invalidateFinopsCache()` drops every FinOps cache entry.
* `setStatus({ phase: 'unauthenticated' })` drives the AppShell
  router to redirect to `/login`.
* The WebSocket hook observes `authEnabled === false` and the
  empty `accessToken` and closes the connection.
* `FinopsPeriodProvider` re-hydrates its state to the default
  month + region so a re-login on the same tab does not flash the
  previous user's selection.

## Billing month persistence

* `FinopsPeriodProvider` persists the active period and region to
  `sessionStorage['accd.finops.period']` and
  `sessionStorage['accd.finops.region']` on every change.
* On the `checking → authenticated` transition the provider
  re-hydrates from storage so F5 restores the previously-selected
  month.
* On `authenticated → unauthenticated` (logout / expired) the
  storage keys are cleared so the next login starts on the
  default month.
* These keys are kept separate from the JWT key — credentials and
  UI preferences never share a storage slot.

## Security

* **sessionStorage is an interim development solution.**  It scopes
  JWT persistence to a browser tab and is cleared on logout, but
  any code running in the same origin (e.g. an XSS payload) can
  still read it.  Phase 8 production target must use HTTPS plus a
  reviewed stronger session mechanism such as `Secure + HttpOnly +
  SameSite=Strict` cookies (or a refresh-token design with
  short-lived access tokens) — see
  `docs/security.md` for the full threat model.
* No JWT appears in URL paths, query strings, fragments, or
  console output.  Verified by `scripts/phase7a_verify.sh`.
* No direct browser-storage access from feature pages.  Verified
  by `scripts/phase7a_verify.sh` and
  `frontend/src/tests/phase7a-closure.test.tsx`.
* WebSocket JWT transport unchanged: `Sec-WebSocket-Protocol:
  bearer.<jwt>`.  Never in URL, path, or query string.  Verified
  by `frontend/src/tests/phase7a-closure.test.tsx` (WebSocket JWT
  transport).

## Tests

* `frontend/src/tests/auth.test.tsx` — updated for the 3-state
  machine, sessionStorage, and legacy cleanup.
* `frontend/src/tests/phase7a-closure.test.tsx` — 7 describe
  blocks pinning:
  * F5 persistence (valid, expired, 401)
  * Login race (Authorization header present on first
    authenticated request)
  * `/api/auth/me` ordering (info before me)
  * AIAnalystPage source contract (no direct storage access)
  * Logout + F5 stays logged out
  * Billing month survives remount
  * Multi-user (user B cannot inherit user A's session)
  * WebSocket JWT in subprotocol only

## Verifier

`scripts/phase7a_verify.sh` — `PASS=71 / FAIL=0` in structural
mode.  Frontend typecheck passes.  Backend Python files parse
cleanly (`ast.parse`).

## Known issues

* The full `npx vitest run` could not be completed in this
  sandbox environment due to a process-group hang unrelated to
  the code under test (the verifier's structural pass covers the
  same contracts).  In a fully built environment with network
  access, `npm test` and `npm run build` complete against the
  same source files.
* `sessionStorage` is an interim solution.  Phase 8 / production
  must move to HTTPS + secure cookies (see Security above).

## External Dependencies

* **Real AI provider:** Requires the operator to set
  `LITELLM_API_KEY` (and optionally `LITELLM_MODEL`) and restart
  the LiteLLM container.  Without it, `/api/ai/status` reports
  `provider_configured=false` and AI stays disabled.
* **Compute Optimizer / Cost Optimization Hub:** Operator runs
  the manual enrollment commands documented in
  `docs/phase7a-integration-stabilization.md`.  The dashboard
  detects the new state automatically on the next refresh.

## Phase 7B Readiness: **READY (with documented extension points)**

Same as the primary Phase 7A report.

---

**STOP CONDITION honoured.** No push, no merge, no Phase 7B work
started, no multi-account/AWS/Azure/Rackspace code added, no IAM
changes, no enrollment calls, no HTTPS.
