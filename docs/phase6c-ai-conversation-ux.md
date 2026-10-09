# Phase 6C — AI Cost Analyst Conversation UX

## Overview

Phase 6C replaces the Phase 6A AI placeholder with a production-quality
HipLink-style AI workspace that fuses three prior layers:

| Backend layer           | Endpoint family                        | Phase |
| ----------------------- | -------------------------------------- | ----- |
| Grounded AI analyst     | `/api/ai/*`                            | 4     |
| Persistent conversations| `/api/conversations/*`                 | 5B    |
| Authenticated realtime  | `/api/ws/conversations/{conversation_id}` | 5C |

The frontend is a **presentation and interaction layer**. AWS data
services remain authoritative for facts; the LLM explains, summarises,
and prioritises trusted Phase 1–3 evidence. The frontend never invents
cost numbers, never invents resource state, never invents
recommendations, and never simulates token streaming.

## Architecture rules (preserved from earlier phases)

- **JWT transport** — the bearer token travels in the
  `Sec-WebSocket-Protocol` subprotocol as `bearer.<jwt>`. **Never** in
  the URL, path, query string, UI surface, or logs.
- **Existing Phase 5B REST** — `/api/conversations` for CRUD,
  `/api/conversations/{id}/messages` for history, and
  `/api/conversations/{id}/analyze` for the REST fallback. No new REST
  routes are introduced in Phase 6C.
- **Existing Phase 5C WebSocket** — `/api/ws/conversations/{id}` with
  the same protocol (`v1`), heartbeat, dedup FIFO, one-in-flight rule,
  and close-code taxonomy.
- **RBAC** — `/analyst` and `/conversations` remain ADMIN/ANALYST only.
  VIEWER triggers the existing inline `AccessDenied` page.
- **Auth-disabled** — the Phase 5B 503 `AuthDisabled` contract is
  honoured. We never fabricate persistent state when auth is off.
- **AI-disabled** — `AIStatusResponse.status === "DISABLED"` shows the
  operator-facing banner. We never fake an answer.
- **Theme** — every new component consumes only semantic tokens
  (`bg-surface`, `text-fg-primary`, `border-border`, `bg-primary-soft`,
  `text-ai`, `text-warning`, `text-danger`, `bg-warning-soft`,
  `bg-danger-soft`, `bg-info-soft`, `bg-ai-soft`, `shadow-card-sm`,
  `shadow-focus`). Both `theme-dark` and `theme-light` palettes already
  exist in `index.css`.
- **No new dependencies** — markdown rendering is an in-house minimal
  subset (~120 lines); React Testing Library + Vitest + jsdom + the
  existing build tooling are unchanged.

## Module map

```
frontend/src/
├── types/
│   └── ai.ts                       # AI / Conversation / WS protocol types
├── lib/ai/
│   ├── api.ts                      # REST conversation CRUD + AI status
│   ├── connection.ts               # useConversationSocket hook
│   ├── protocol.ts                 # frame builders + URL/subprotocol helpers
│   └── markdown.tsx                # in-house markdown-subset renderer
├── components/
│   ├── AiProviderBadge.tsx
│   ├── AuthDisabledNotice.tsx
│   ├── Composer.tsx
│   ├── ConversationList.tsx
│   ├── ConversationView.tsx
│   ├── EvidenceCard.tsx
│   ├── MessageBubble.tsx
│   └── ProgressIndicator.tsx
├── pages/
│   ├── AIAnalystPage.tsx           # HipLink workspace (replaces placeholder)
│   └── ConversationsPage.tsx       # operational history view
└── tests/
    ├── ai-api.test.ts
    ├── ai-protocol.test.ts
    ├── ai-connection.test.tsx
    ├── ai-page.test.tsx
    ├── composer.test.tsx
    ├── conversation-list.test.tsx
    ├── conversations-page.test.tsx
    ├── evidence-card.test.tsx
    ├── message-bubble.test.tsx
    └── progress-indicator.test.tsx
```

## REST surface (frontend → backend)

The frontend never invents endpoints. Every fetch goes through `apiFetch`
which injects the bearer header from the `AuthProvider` and normalises
errors via `ApiError`.

| Operation                              | Method | Path                                              |
| -------------------------------------- | ------ | ------------------------------------------------- |
| AI provider status                     | GET    | `/api/ai/status`                                  |
| List conversations                     | GET    | `/api/conversations?limit&offset&archived`        |
| Create conversation                    | POST   | `/api/conversations`                              |
| Get conversation detail                | GET    | `/api/conversations/{id}`                         |
| List messages                          | GET    | `/api/conversations/{id}/messages?limit&offset`   |
| Rename / archive                       | PATCH  | `/api/conversations/{id}`                         |
| Delete conversation                    | DELETE | `/api/conversations/{id}`                         |

`503 AuthDisabled` surfaces as `ApiError.errorCode === "AuthDisabled"`
and the page renders `<AuthDisabledNotice />` instead of the workspace.

## WebSocket protocol (frontend → backend)

The realtime channel is the existing Phase 5C contract:

```
URL        wss://<host>/api/ws/conversations/{conversation_id}
Subproto   ["bearer.<jwt>"]
Version    "v1"
```

### Client → server

```json
{ "type": "ping", "ts": 1700000000000 }
```

```json
{
  "type": "user_message",
  "request_id": "uuid v4",
  "question": "<= 2000 chars",
  "region": "us-east-1",
  "days": 30
}
```

`region` is optional (`null` accepted). `days ∈ [7, 30, 60, 90]`.

### Server → client

| Frame                       | Trigger / payload                                                   |
| --------------------------- | ------------------------------------------------------------------- |
| `connected`                 | Handshake acknowledgement + protocol version + heartbeat interval   |
| `user_message_accepted`     | Server persisted the user message and queued the AI run            |
| `ai_processing`             | LiteLLM invocation in progress                                     |
| `assistant_message`         | Final answer + grounding + citations + warnings                    |
| `error`                     | Terminal error with `code` and human-readable `message`             |
| `pong`                      | Reply to client `ping`                                             |

The protocol is **not** a token stream. The final answer arrives in one
`assistant_message` frame. The progress indicator surfaces
`connected → ai_processing → completed` for the user.

### Close codes

| Code  | UI state                  |
| ----- | ------------------------- |
| 1000  | `disconnected`            |
| 1006  | `disconnected` (auto-retry once) |
| 1008  | `authorization_failure` (auth disabled) |
| 1011  | `disconnected` (auto-retry once) |
| 4401  | `authorization_failure`   |
| 4403  | `authorization_failure`   |
| 4404  | `authorization_failure` (cross-user or missing) |
| 4xxx* | `retryable_failure` (auto-retry once) |

The hook reconnects at most **once** on a non-auth failure. After that
the user must press the **Reconnect** button — no infinite reconnect
loops.

## Concurrency & dedup

- One AI request in flight per connection.
- Duplicate `request_id` (browser resend) is silently consumed by the
  dedup FIFO (`MAX_RECENT_REQUEST_IDS = 128`).
- Composer is disabled while `inflight === true` and on empty/over-long
  input.

## State management

A `useConversationSocket` hook returns a stable API:

```ts
{
  state: ConnectionState,
  connected: ServerConnectedFrame | null,
  lastAssistant: ServerAssistantFrame | null,
  lastError: ServerErrorFrame | null,
  inflightRequestId: string | null,
  heartbeatSeconds: number,
  closeCode: number | null,
  connect(): void,
  disconnect(): void,
  reconnect(): void,
  sendUserMessage(args): string,
}
```

`AIAnalystPage` wires the hook once per active conversation. Switching
conversations closes the previous socket and opens a fresh one — no
sockets outlive their conversation. Conversation listing uses
`useFinopsQuery` so list refresh and tab re-entry share cache.

## AI status

The page calls `fetchAIStatus()` once on mount. The result drives:

- `<AiProviderBadge />` — shows model alias + reachable flag.
- Disabled banner — when `ai_enabled === false`.
- `<AuthDisabledNotice />` — when the user is ADMIN/ANALYST but auth is
  off.

## Citations / evidence

The backend `citations[]` array is server-validated. The frontend
renders each citation verbatim with sanitised key/value rendering — no
fabricated keys and no `dangerouslySetInnerHTML`.

If the assistant message reports insufficient Phase 1–3 evidence
(`cost_evidence_used=false`, `recommendations_used=0`,
`capabilities_used=false` and `INSUFFICIENT_EVIDENCE` warning), the page
surfaces a small `insufficient-evidence` notice above the answer. We
never transform an insufficient-evidence response into a confident
answer.

## Composer behaviour

- Multiline `<textarea>` with `aria-label`.
- `Enter` submits; `Shift+Enter` inserts a newline.
- Disabled when:
  - question is empty (after trim);
  - question length exceeds 2000 chars;
  - `inflight === true`.
- `aria-describedby` points at the connection state label.
- Never embeds system prompts, evidence, or context into the textarea.

## Markdown renderer

`lib/ai/markdown.tsx` supports a strict subset:

- Headings (h1–h3 only).
- Bullet and numbered lists.
- Fenced code blocks (preserved with semantic bg/code tokens).
- Inline `code`.
- Paragraphs.
- Line breaks.

No raw HTML, no images, no arbitrary link hrefs. User text is rendered
through normal React text nodes, so escape happens at the framework
boundary — never via `dangerouslySetInnerHTML`. Unrecognised tokens are
rendered as plain escaped text.

## Responsive

- `≥1024px` (`lg:`) — two-pane HipLink layout: left rail (260px) +
  right pane (flex-1).
- `<1024px` — left rail collapses into a drawer toggled by a
  `Conversations` button (`aria-expanded`). Composer remains full-width.
  Evidence cards remain single-column on narrow viewports.

## Accessibility

- Composer has `aria-label`.
- Connection indicator uses `role="status" aria-live="polite"`.
- Authorization failure uses `role="alert"`.
- Conversation list is `role="list"` with each row a `role="listitem"`
  + `<button>` for keyboard selection.
- Focus management: opening a conversation focuses the message stream
  container; the composer is reachable via Tab.
- Color is never the sole state carrier — every status also carries
  text.

## Security invariants (enforced by tests)

- The WebSocket URL never embeds the JWT.
- The JWT never appears in any UI string, console log, or path
  parameter of the shipped source.
- `assertUrlHasNoToken` rejects any URL containing `?token=` / `&jwt=`
  patterns.
- Frame payload sanity check rejects any inbound frame whose text
  contains a JWT-shaped string.
- AI/CONV WS frames whose `code` value is unknown are surfaced as
  `protocol_violation`.

## What we did NOT change

- No backend source code changes.
- No new dependencies.
- No changes to `nginx/default.conf`.
- No changes to `docker-compose.yml`.
- No changes to the existing Phase 6A / 6B components outside of the AI
  and Conversations pages.

## Test coverage

| Suite                                      | Purpose                                           |
| ------------------------------------------ | ------------------------------------------------- |
| `ai-api.test.ts`                           | REST conversation wrappers + AuthDisabled         |
| `ai-protocol.test.ts`                      | Frame builders + URL safety + subprotocol         |
| `ai-connection.test.tsx`                   | WebSocket hook state machine                      |
| `ai-page.test.tsx`                         | Page integration (RBAC, disabled, happy path)     |
| `composer.test.tsx`                        | Enter/Shift+Enter, disabled states                |
| `conversation-list.test.tsx`               | Selection, empty, archived                        |
| `conversations-page.test.tsx`              | Empty / loading / failure / auth-disabled         |
| `evidence-card.test.tsx`                   | Citation rendering                                |
| `message-bubble.test.tsx`                  | USER / ASSISTANT / SYSTEM_EVENT                   |
| `progress-indicator.test.tsx`              | Connection state → labelled, color-token pill     |

All 10 Phase 6C test files together contribute **93** passing tests and
zero React `act(...)` warnings.

## Verifier

`scripts/phase6c_verify.sh` enforces the structural, transport,
security, test, and live-integration checks. Run order:

1. Delegate to `phase6b_verify.sh` (which delegates up the chain to
   `phase5c_verify.sh`).
2. Phase 6C structural surface (types, REST wrappers, WS subprotocol
   transport, components, pages).
3. Phase 6C-specific frontend test files exist on disk.
4. Live probes: `/api/ai/status` shape, `/api/conversations` anonymous
   refusal (401/403/503), nginx WS location + Upgrade + Connection
   headers.
5. Docker / nginx topology — only nginx publishes to a host port; all
   five containers are healthy.
