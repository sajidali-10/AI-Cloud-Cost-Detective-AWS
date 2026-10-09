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
