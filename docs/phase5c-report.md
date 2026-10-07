# PHASE 5C — WEBSOCKET REALTIME LAYER REPORT

## Overall Status

**READY**

Phase 5B baseline preserved (Phase 5B verify suite green via
`scripts/phase5b_verify.sh`). Phase 5C adds a single, secure,
single-conversation WebSocket endpoint on top of the Phase 5B
durable conversation surface and the Phase 4 grounded AI
service. Phase 1–3 remain authoritative; conversation history
remains untrusted data.

## Branch / Baseline

* Branch: `phase-5-auth-persistence-websockets`
* Baseline: `73870ef` (Phase 5B closure: fix async recommendation
  fallback warning)
* Working tree: final commit only.

## Modules added

| Path                                          | Purpose                                                         |
|-----------------------------------------------|-----------------------------------------------------------------|
| `backend/app/api/ws_auth.py`                  | JWT extraction, Phase 5A SecurityCore reuse, RBAC, subprotocol  |
| `backend/app/api/ws_conversations.py`         | WebSocket route + main loop + persistence + AI integration     |
| `backend/app/schemas/websocket.py`            | Pydantic v2 schemas, protocol constants, error-code registry   |
| `backend/tests/test_websocket_security.py`    | Auth / RBAC / ownership / IDOR / no-secret-leakage             |
| `backend/tests/test_websocket_protocol.py`    | Schema / persistence / fresh-evidence / dedup / heartbeat      |
| `scripts/phase5c_verify.sh`                   | Phase 5B delegation + Phase 5C-specific checks                 |

## Modules modified

* `backend/app/main.py` — mount the WebSocket router under
  `/ws/conversations`.
* `backend/app/services/ai_system_prompt.py` — document the
  `<conversation_history>` untrusted-data contract.
* `backend/pytest.ini` — set
  `asyncio_default_fixture_loop_scope = function` to silence the
  pytest-asyncio 0.24 deprecation warning. No test in the
  project overrides the loop scope, so this is purely a
  forward-compatibility setting.
* `nginx/default.conf` — new `/api/ws/` location with the
  WebSocket upgrade headers and generous AI-friendly timeouts.

## Security invariants (all satisfied)

| Invariant                                                                    | Where enforced                                            |
|------------------------------------------------------------------------------|-----------------------------------------------------------|
| JWT validation reuses Phase 5A                                               | `resolve_ws_principal` → `SecurityCore.decode_token`      |
| Authoritative DB role, never JWT `role` claim                                | `resolve_ws_principal` → `AuthService.get_active_user`    |
| ADMIN and ANALYST allowed                                                    | `role in ("ADMIN", "ANALYST")`                            |
| VIEWER denied                                                                | `role_not_allowed` → 4403                                 |
| AUTH_ENABLED=false denied for durable WS conversations                       | `auth_disabled` → 1008                                    |
| Cross-user conversation access denied                                        | `ConversationService.get` → `ConversationNotFound` → 4404 |
| Admin cannot read another user's private conversation merely because ADMIN    | Same ownership-scoped SQL guard                            |
| Fresh Phase 1–3 evidence remains authoritative                               | `AIService.generate_analysis_with_history` re-runs fresh  |
| Conversation history remains untrusted context                               | `<conversation_history>` delimiters + system prompt note  |
| No `AI_ESTIMATE`                                                             | Phase 4 grounding + saved-protection sentinel preserved   |
| No invented savings                                                          | `grounding_metadata` shape unchanged from Phase 4         |
| No secret / JWT / AWS / provider-credential logging                          | Sanitized error messages + `logger.info` only             |
| No raw LiteLLM payload leakage                                               | Stable `AI_*` codes; no provider `choices` on the wire    |
| One bounded AI operation per connection                                      | `state.lock` + `state.inflight` + sequential main loop    |
| Duplicate `request_id` handled                                               | `state.recent_request_ids` FIFO → `error` (Busy)          |
| Malformed / oversized messages handled safely                                | Pydantic + `MAX_FRAME_BYTES` guard → sanitized `error`    |
| Normal disconnects handled cleanly                                           | `WebSocketDisconnect` catch + `finally` db close          |
| nginx remains the only publicly exposed service                              | Verified by `phase5c_verify.sh` check #7                  |

## Test coverage

* `tests/test_websocket_security.py` — 16 tests
  * ADMIN happy path
  * ANALYST happy path
  * `Authorization` header also accepted
  * VIEWER denied
  * VIEWER with someone else's conversation denied
  * Missing / malformed / expired / bad-signature token denied
  * Inactive user denied
  * Forged role claim denied (DB wins)
  * AUTH_ENABLED=false denied
  * Cross-user conversation denied
  * Nonexistent conversation denied
  * No JWT in connected event
  * No AWS credentials / provider keys in any event
* `tests/test_websocket_protocol.py` — 22 tests
  * Valid user_message persists USER + ASSISTANT
  * Response correlates to request_id
  * Invalid JSON → sanitized error
  * Unsupported event type → sanitized error
  * Empty question rejected
  * Oversized question rejected
  * Unsupported lookback rejected
  * Oversized frame rejected
  * USER persisted BEFORE AI call on failure
  * Fresh AWS evidence used on every call
  * Bounded history rendered in user message body
  * Hostile history remains data
  * Prompt injection in current question remains data
  * Null-savings protection preserved
  * Sanitized error event on AI failure
  * Duplicate request_id rejected (no second USER row)
  * Sequential messages processed one at a time
  * `_ConnectionState` lifecycle (lock + flag + FIFO)
  * ping/pong heartbeat
  * Normal disconnect handled
  * Reconnect after disconnect
  * No JWT / provider keys in events

Total: **38 tests**, all passing in 48s.

## Phase 5B regression

`scripts/phase5b_verify.sh` (delegated) remains green after
Phase 5C. Verified both before and after Phase 5C-specific
changes via `phase5c_verify.sh` checks #1 and #5.

## Full backend pytest

* Command: `docker compose exec -T backend python -m pytest`
* Result: **580 passed in 137s**.
* No new RuntimeWarnings. The only remaining warnings are
  transitive (`starlette.testclient` anyio deprecation,
  `botocore` `datetime.utcnow()`); both are silenced by the
  existing `pytest.ini` `ignore::DeprecationWarning` rule.

## Configuration

No new settings were added in Phase 5C. The WebSocket layer
honours every existing Phase 5A/5B/4 setting:

* `AUTH_ENABLED`
* `AI_ENABLED`
* `AI_REQUEST_TIMEOUT_SECONDS`
* `AI_MAX_HISTORY_MESSAGES`, `AI_MAX_HISTORY_CHARS`
* `CONVERSATION_TITLE_MAX_LENGTH`,
  `CONVERSATION_LIST_MAX_LIMIT`,
  `CONVERSATION_MESSAGES_MAX_LIMIT`

The `pytest-asyncio` `asyncio_default_fixture_loop_scope =
function` setting was promoted from a deprecation warning to a
documented setting in `pytest.ini`. No existing test overrides
the loop scope, so this is purely a forward-compatibility
setting.

## Public surface

* nginx → backend, internal-only
* Backend listens on `:8000` inside the `internal` network
* Single new public path: `ws://<host>/api/ws/conversations/{id}`
* Verified by `phase5c_verify.sh` checks #7 and #10.

## Known limitations (deliberate Phase 5C scope)

* No streaming tokens — every accepted `user_message` produces
  exactly one `assistant_message` (or one `error`).
* Single-conversation scope per WebSocket.
* Client-driven heartbeat only — no outbound ping.
* No server-side reconnection hint; the client decides.

These are deliberate Phase 5C tradeoffs and would be addressed
in subsequent phases (if at all). They do not weaken any
security invariant.

## Phase 5C verify summary

```
[check] Phase 5B regression check (delegated to phase5b_verify.sh)
  PASS phase5b_verify.sh exited 0 (44 passed, 0 failed)
[check] Phase 5C modules importable (ws_auth, ws_conversations, websocket schemas)
  PASS Phase 5C modules importable (ws_auth + ws_conversations + schemas)
[check] WebSocket route /ws/conversations/{conversation_id} registered
  PASS WebSocket route registered: /ws/conversations/{conversation_id}
[check] Phase 5C pytest suite green (security + protocol)
  PASS Phase 5C pytest suite green (38 passed)
[check] Phase 5B regression (re-run after Phase 5C additions)
  PASS phase5b_verify.sh still green after Phase 5C (44 passed, 0 failed)
[check] No secret / JWT / AWS / LiteLLM credential leakage in tracked source
  PASS No obvious secrets in tracked files
[check] Only nginx publishes to a host port
  PASS No service other than nginx publishes to a host port
[check] All containers healthy
  PASS All running containers are healthy
[check] No AI_ESTIMATE token in tracked source
  PASS No AI_ESTIMATE token in tracked source
[check] nginx WebSocket upgrade configuration present
  PASS nginx WebSocket upgrade configuration present (location + Upgrade + Connection)
[check] No Redis / Kafka / API Gateway WS / SSE / Socket.IO introduced
  PASS No Redis / Kafka / SSE / Socket.IO / API Gateway WS introduced

============================================================
Phase 5C verify: 11 passed, 0 failed
============================================================
```

## Verdict

Phase 5C is ready. All Phase 0–5B behaviour is preserved
(verified by `phase5b_verify.sh` + full backend pytest
regression). All Phase 5C security invariants are satisfied.
The WebSocket is mounted behind nginx only; no new public
services were introduced.
