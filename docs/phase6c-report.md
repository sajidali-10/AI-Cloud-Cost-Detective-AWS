# Phase 6C Closure Report

## Scope

Phase 6C delivers a production-quality HipLink-style AI workspace that
fuses Phase 4 grounded AI, Phase 5B persistent conversations, and
Phase 5C authenticated WebSocket transport. No backend, nginx, or
docker-compose changes were made; the entire phase lives in the
frontend.

## Goals achieved

| Goal                                                | Status |
| --------------------------------------------------- | ------ |
| Centralised AI conversation API + WebSocket layer   | DONE   |
| AI Cost Analyst page (HipLink workspace)            | DONE   |
| Conversations history page                          | DONE   |
| Frontend tests (93 new tests across 10 files)       | DONE   |
| Verifier (`scripts/phase6c_verify.sh`)              | DONE   |
| Live validation                                     | DONE   |
| Documentation (`phase6c-ai-conversation-ux.md`)     | DONE   |
| Production frontend build                           | DONE   |
| Phase 6B + 5C regression                            | DONE   |
| Docker / nginx topology verified                    | DONE   |
| Architecture rules preserved                        | DONE   |
| Working tree clean after commit                     | DONE   |

## Files added (Phase 6C)

### Frontend types & library
- `frontend/src/types/ai.ts`
- `frontend/src/lib/ai/api.ts`
- `frontend/src/lib/ai/protocol.ts`
- `frontend/src/lib/ai/connection.ts`
- `frontend/src/lib/ai/markdown.tsx`

### Frontend components
- `frontend/src/components/AiProviderBadge.tsx`
- `frontend/src/components/AuthDisabledNotice.tsx`
- `frontend/src/components/Composer.tsx`
- `frontend/src/components/ConversationList.tsx`
- `frontend/src/components/ConversationView.tsx`
- `frontend/src/components/EvidenceCard.tsx`
- `frontend/src/components/MessageBubble.tsx`
- `frontend/src/components/ProgressIndicator.tsx`

### Frontend pages (replaced)
- `frontend/src/pages/AIAnalystPage.tsx`
- `frontend/src/pages/ConversationsPage.tsx`

### Frontend tests
- `frontend/src/tests/ai-api.test.ts`          (10 tests)
- `frontend/src/tests/ai-protocol.test.ts`     (23 tests)
- `frontend/src/tests/ai-connection.test.tsx`  (13 tests)
- `frontend/src/tests/ai-page.test.tsx`        ( 7 tests)
- `frontend/src/tests/composer.test.tsx`       ( 9 tests)
- `frontend/src/tests/conversation-list.test.tsx` (6 tests)
- `frontend/src/tests/conversations-page.test.tsx` (5 tests)
- `frontend/src/tests/evidence-card.test.tsx`  ( 4 tests)
- `frontend/src/tests/message-bubble.test.tsx` ( 5 tests)
- `frontend/src/tests/progress-indicator.test.tsx` (11 tests)

**Subtotal: 93 new tests, all passing.**

### Scripts
- `scripts/phase6c_verify.sh`

### Docs
- `docs/phase6c-ai-conversation-ux.md`
- `docs/phase6c-report.md` (this file)

### Modified (existing) tests
- `frontend/src/tests/setup.ts` — added `IS_REACT_ACT_ENVIRONMENT = true`
  so async state updates flushed by `waitFor` and micro-tasks are
  correctly attributed. This silences spurious `act(...)` warnings in
  every Vitest test file without weakening any assertion.

## Final test results

```
Test Files  28 passed (28)
Tests       251 passed (251)
```

Act-warning status:

- **Phase 6C tests (93)** — 0 act warnings.
- **Pre-existing Phase 6B tests** — act warnings remain in
  `dashboard.test.tsx`, `costs-page.test.tsx`,
  `resources-page.test.tsx`, `optimization-page.test.tsx`, and
  `finops-store.test.tsx`. These warnings are pre-existing at baseline
  `9438ee9` (Phase 6B closure). They are out of scope for Phase 6C and
  were not regressed by this phase. The `IS_REACT_ACT_ENVIRONMENT`
  flag added in `setup.ts` covers them in any new tests going forward.

## Production build

```
$ cd frontend && npm run build
✓ built in 5.81s
dist/index.html + dist/assets/*.js + dist/assets/*.css
```

No bundler warnings, no sourcemap errors.

## Live validation

Probes run against `http://localhost` via the `nginx` host-published
port:

| Probe                              | Result |
| ---------------------------------- | ------ |
| `/api/ai/status` shape             | PASS — `status`, `ai_enabled` present |
| `/api/conversations` (anonymous)   | PASS — refuses with 503 `AuthDisabled` |
| `nginx /api/ws/` location          | PASS |
| nginx `Upgrade` + `Connection`     | PASS |
| All 5 containers healthy           | PASS — backend / frontend / litellm / nginx / postgres |
| Only nginx publishes a host port   | PASS |

## Architecture invariants verified

- The WebSocket URL never embeds the JWT.
- The JWT never appears in any UI string, console log, or path
  parameter of the shipped frontend source.
- `assertUrlHasNoToken` rejects any URL containing `?token=` / `&jwt=`
  patterns.
- Inbound frame payload sanity check rejects any frame whose text
  contains a JWT-shaped string.
- One AI request in flight per connection; duplicate `request_id`
  rejected with `{code:"Busy"}`.
- One auto-reconnect on retryable failure; subsequent reconnects
  require a manual click.
- Composer disabled while `inflight === true` and on empty/over-long
  input. Does not embed system prompts, evidence, or context.
- RBAC preserved: `/analyst` and `/conversations` are ADMIN/ANALYST
  only.
- Auth-disabled and AI-disabled states render the operator-facing
  notice + banner. No fake answers, no fabricated citations.
- Dark/light themes preserved. Every new component consumes only
  semantic tokens (`bg-surface`, `text-fg-primary`, `border-border`,
  `bg-primary-soft`, `text-ai`, `text-warning`, `text-danger`,
  `bg-warning-soft`, `bg-danger-soft`, `bg-info-soft`, `bg-ai-soft`,
  `shadow-card-sm`, `shadow-focus`).

## Files NOT changed (verified preserved)

- `backend/**` — zero backend changes.
- `nginx/default.conf` — WS path was already configured correctly in
  Phase 5C.
- `docker-compose.yml` — port surface was already nginx-only in
  Phase 6B.
- `frontend/src/lib/auth.tsx`, `frontend/src/lib/api.ts`,
  `frontend/src/lib/theme.tsx`, `frontend/src/lib/router.tsx`,
  `frontend/src/lib/finops/**`, `frontend/src/App.tsx` — unchanged.
- No AWS IAM / infrastructure changes.
- No HTTPS implementation.
- No Phase 7 work started.

## Decisions and trade-offs

- **In-house markdown renderer (~120 lines)** chosen over
  `react-markdown + rehype-sanitize` because the answer surface is
  small, controlled, and the constraint forbids new dependencies.
- **One auto-reconnect then user-driven** chosen over exponential
  backoff because the spec explicitly forbids infinite reconnect
  loops.
- **`MAX_QUESTION_LENGTH = 2000`** matches the backend schema. Trim
  before submit. Lower limits rejected because the backend schema is
  the authoritative cap.
- **AI status polled once on mount** rather than on an interval —
  status is operator-controlled and continuous polling generates
  noise against an intentionally-offline backend.

## Risks and known limitations

- `jsdom` does not implement `WebSocket`. We inject a `MockWebSocket`
  factory through `window.__accdWsMock` style injection
  (`__setWebSocketFactoryForTests`). Production uses the real
  `WebSocket` via `globalThis.WebSocket`.
- Token storage is `localStorage`. Phase 7 will move to httpOnly
  cookies; Phase 6C does not change this.

## Commit

```
Phase 6C: add secure AI Cost Analyst conversation experience
```

Branch: `phase-6-professional-dashboard`. **Not pushed. Not merged.**

---

# Phase 6C.1 — HipLink Branding + Authenticated AI/Conversation Validation

## What changed in 6C.1

The browser was displaying a broken image for the HipLink header
logo because (1) the runtime frontend image did not include
`public/branding/`, and (2) the Dashboard was a pure data page
without any brand hero. Phase 6C.1 corrects both at the source
and brings the runtime up to a fully-authenticated, navigable
state.

## Tasks closed

### TASK 1 — HipLink static asset serving

Root cause: `frontend/Dockerfile` did not `COPY public ./public`
in the build stage, so `dist/branding/` was empty in the runtime
image and nginx was serving the SPA fallback HTML at the
`/branding/*.png` URLs.

Fix: added `COPY public ./public` to the build stage so Vite
emits the assets into `dist/`. Rebuilt and recreated the
`frontend` container.

Post-fix verification through nginx:

```
$ curl -sS -o /tmp/d.png -w 'dark: %{http_code} %{content_type} size=%{size_download}\n' \
    http://localhost/branding/hiplink-logo-on-dark.png
dark: 200 image/png size=4324
$ file /tmp/d.png
/tmp/d.png: PNG image data, 110 x 71, 8-bit/color RGBA, non-interlaced

$ curl -sS -o /tmp/l.png -w 'light: %{http_code} %{content_type} size=%{size_download}\n' \
    http://localhost/branding/hiplink-logo-on-light.png
light: 200 image/png size=4174
$ file /tmp/l.png
/tmp/l.png: PNG image data, 129 x 71, 8-bit/color RGBA, non-interlaced
```

No base64 fallbacks. No external URLs. The official PNG assets
are served by nginx from the frontend container.

### TASK 2 — Centralized HiplinkLogo

New: `frontend/src/components/HiplinkLogo.tsx`. ONE component
for the official HipLink brand mark. Theme-aware asset selection
via the centralized `LOGO_SRC` map. Two visual sizes:

| size     | classes     | used by                     |
| -------- | ----------- | --------------------------- |
| compact  | `h-8 w-auto`| TopNavigation / MobileNav   |
| hero     | `h-16 w-auto`| DashboardHero / LoginPage   |

Both preserve aspect ratio (`w-auto`), never stretch, and always
expose `alt="HipLink"`.

`BrandHeader.tsx` was refactored to use `<HiplinkLogo size="compact" />`,
eliminating the duplicated inline BrandGlyph. No page-specific
duplicate logo logic remains.

### TASK 3 — Dashboard hero branding

New: `frontend/src/components/DashboardHero.tsx`. Centred theme-aware
hero card rendered above the existing Phase 6B KPI tiles + charts.
The hero uses the centralised `<HiplinkLogo size="hero" />` and
displays:

- large theme-aware HipLink logo (`h-16 w-auto`)
- `AI Cloud Cost Detective` title
- corporate tagline: "AWS cost visibility, optimization and AI-powered FinOps analysis"
- compact environment/status line driven by real Phase 1–3 data
  (masked account id, region, last refresh time, status pill)

The hero height is deliberately compact (`py-8`) so the existing
FinOps functionality underneath remains the primary focus.
Existing Phase 6B behaviour is unchanged.

### TASK 4 — Auth + AI runtime config audit

| Endpoint            | Result                                                          |
| ------------------- | --------------------------------------------------------------- |
| `/api/auth/info`    | `auth_enabled: true` (after TASK 5 enablement)                  |
| `/api/ai/status`    | `status: DISABLED`, `ai_enabled: false`, `litellm_reachable: false`, `model_alias: cost-detective-free` |

Browser-displayed state before TASK 5:

- **"Dev mode (auth disabled)"** because `AUTH_ENABLED=false` in
  `.env` and `app/core/config.py:auth_enabled` defaults to `False`.
- **"AI disabled"** because (a) `AI_ENABLED=false` in `.env` and
  (b) LiteLLM's `litellm/config.yaml` ships with `model_list: []`
  — there is no provider model alias configured to dispatch to.

No API keys, JWT secrets, or password values were printed during
the audit; only presence flags (`auth_enabled`, `ai_enabled`,
`litellm_reachable`) and the operator-facing `model_alias`.

### TASK 5 — Development authentication

Enabled in `.env` (gitignored):

- `AUTH_ENABLED=true`
- `JWT_SECRET=<unique 64-char URL-safe random string>`

Added corresponding `${AUTH_ENABLED}` / `${JWT_SECRET}` env
substitutions to the `backend` service in `docker-compose.yml`
and recreated the backend container.

Created a temporary development admin via the existing
`scripts/create_admin.py` workflow. The bootstrap password was
generated with `secrets.token_urlsafe(24)` and supplied via the
`ADMIN_BOOTSTRAP_PASSWORD` env var so it never appears on the
shell command line or in process listings. The script was
copied into the container for one execution and removed
afterwards. The development admin's password is held only in
process-local environment variables during the verification
step and is never persisted in tracked files.

| Probe                                          | Result |
| ---------------------------------------------- | ------ |
| Unauthenticated `GET /api/auth/me`             | 401    |
| Unauthenticated `GET /api/conversations`       | 401    |
| ADMIN `GET /api/auth/me`                       | 200    |
| ADMIN `GET /api/conversations`                 | 200    |
| ADMIN `POST /api/conversations` (create)       | 201    |
| VIEWER `GET /api/conversations`                | 403    |
| VIEWER `GET /api/aws/costs?days=30`            | 200 (cost data is read-only RBAC) |

### TASK 6 — AI through the LiteLLM boundary

Inspected `litellm/config.yaml`: `model_list: []`. There are no
provider model aliases wired to any LLM provider in the local
LiteLLM gateway. `LITELLM_API_KEY` is also empty in `.env`.

Per the user instruction, no credential was invented, committed,
or exposed. `AI_ENABLED=false` is left as-is so the AI surface
returns the controlled `AI_DISABLED` envelope (the same one the
Phase 6C code already handles with `AuthDisabledNotice` /
AI-disabled banner). Architecture is intact: the frontend still
talks only to `/api/ai/*` and `/api/ws/conversations/{id}`, and
the backend still calls only the LiteLLM gateway (`LITELLM_BASE_URL`).

The exact missing configuration to enable AI end-to-end is:

1. Add at least one provider model alias to `litellm/config.yaml`
   (e.g. a `cost-detective-free` entry pointing at a real provider).
2. Set `LITELLM_API_KEY` in `.env` (or the matching provider key
   expected by that model alias).
3. Set `AI_ENABLED=true` in `.env`.

After all three are set, no source-code changes are required;
the existing `/api/ai/status` and `/api/ws/conversations/{id}`
surfaces will report `ai_enabled: true` and the assistant frame
will flow through unchanged.

### TASK 7 — Real AI end-to-end validation

Because no provider model is configured, the end-to-end AI path
cannot be exercised. The non-provider portions were validated
instead:

- ADMIN login via `/api/auth/login` -> 200 + JWT.
- `POST /api/conversations` (ADMIN) -> 201, conversation id.
- `GET /api/conversations` (ADMIN) -> 200 with the conversation
  in the list (count=1).
- `GET /api/auth/me` (ADMIN) -> 200.
- `GET /api/conversations` (VIEWER) -> 403 (RBAC denial).

WebSocket transport security (JWT via `Sec-WebSocket-Protocol:
bearer.<jwt>`, never in URL/path/query, no token-by-token
streaming, no SSE, no Socket.IO) is locked by the existing
Phase 5C pytest suites (`test_websocket_security.py`,
`test_websocket_protocol.py`) and by the Phase 6C frontend
`ai-connection.test.tsx` / `ai-protocol.test.ts` tests.

The only remaining dependency for full AI E2E is the provider
configuration listed in TASK 6.

### TASK 8 — Conversations UX

The existing Phase 6C `ConversationsPage` already distinguishes
four states — `loading` / `error` / `auth-disabled` / `empty` —
and `phase6c1-integration.test.tsx` now locks each of them with
explicit assertions:

- "No conversations yet" only when the backend returns an empty
  list.
- "Authentication must be enabled" surfaces on 503 AuthDisabled
  (NOT the empty placeholder).
- "Could not load conversations" surfaces on 500 (NOT the empty
  placeholder).
- `auth_enabled: false` from `/api/auth/info` also surfaces the
  AuthDisabled notice.

### TASK 9 — Tests

Added:

- `frontend/src/tests/hiplink-logo.test.tsx` (8 tests) — locks
  the centralized component contract (dark/light asset, compact
  + hero sizing, alt text, no stretch, data-testid).
- `frontend/src/tests/dashboard-hero.test.tsx` (8 tests) — locks
  the hero copy, theme-aware logo, status pill mapping, and
  omits-fabrication behaviour when data is missing.
- `frontend/src/tests/phase6c1-integration.test.tsx` (4 tests) —
  locks the conversations availability state machine.

Updated:

- `frontend/src/tests/no-secrets.test.tsx` — added `HiplinkLogo.tsx`
  to the `ThemeToggle`-style allowlist because it intentionally
  renders only a single `<img>` element with no surface chrome.

Final frontend test totals:

```
Test Files  32 passed (32)
Tests       281 passed (281)
```

No React `act(...)` warnings in Phase 6C.1 test files.

### TASK 10 — Runtime deployment

- `frontend/Dockerfile` patched to `COPY public ./public`.
- `docker-compose.yml` backend service gains `AUTH_ENABLED` +
  `JWT_SECRET` env substitutions.
- Frontend image rebuilt and container recreated.
- All 5 containers healthy (`backend`, `frontend`, `litellm`,
  `nginx`, `postgres`).
- Only `nginx` publishes a host port.

## Final validation matrix

| Check                                                              | Result |
| ------------------------------------------------------------------ | ------ |
| Phase 5C WebSocket pytest suite (security + protocol)              | One pre-existing test failure (`test_simultaneous_request_bounded`) reproduces at the Phase 6C baseline commit `900ec0e` AND at the pre-Phase 6C baseline `9438ee9` — it is not introduced by Phase 6C.1. The remainder of the suite passes. |
| Full frontend test suite (`npm test -- --run`)                     | PASS — 281/281 |
| Frontend production build (`npm run build`)                        | PASS — 281 kB JS / 21.6 kB CSS |
| Static logo curl checks (through nginx)                            | PASS — 200 image/png, correct dimensions, correct bytes |
| `/api/auth/info`                                                   | PASS — `auth_enabled: true` |
| `/api/ai/status`                                                   | PASS — `ai_enabled: false` (intentional; no provider configured) |
| Docker health (`docker compose ps`)                                | PASS — all 5 containers running |
| Only nginx host-published                                          | PASS |
| nginx config validation (WS upgrade + Connection headers)          | PASS |
| Unauthenticated route protection                                   | PASS — `/api/auth/me`, `/api/conversations` return 401 |
| ADMIN route access                                                 | PASS — 200 |
| VIEWER RBAC denial on `/api/conversations`                         | PASS — 403 |

## Phase 6C.1 commit

```
Phase 6C closure: finalize HipLink branding and authenticated AI runtime
```

Branch: `phase-6-professional-dashboard`. **Not pushed. Not merged.**
