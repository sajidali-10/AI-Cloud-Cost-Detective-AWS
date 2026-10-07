# Phase 5C — Secure WebSocket Realtime Conversation Layer

## Purpose

Phase 5C adds a single, secure, single-conversation WebSocket
endpoint on top of the Phase 5B durable conversation surface and
the Phase 4 grounded AI service. Clients can:

1. Open a WebSocket scoped to a conversation they own.
2. Exchange a small, JSON-enveloped event protocol (versioned
   `v1`).
3. Send USER messages; receive, in order, `user_message_accepted`
   (persistence), `ai_processing` (AI started), and either
   `assistant_message` (AI answer) or a sanitized `error`
   event (AI failure).
4. Send `ping` events; receive `pong`.
5. Get a 1008 close code if `AUTH_ENABLED=false`.
6. Get a 4401 close code for missing / malformed / expired /
   signature-invalid tokens.
7. Get a 4403 close code for VIEWER / INACTIVE users.
8. Get a 4404 close code for cross-user or nonexistent
   conversations (indistinguishable from "not found").
9. Recover cleanly from any normal client disconnect, then
   reconnect.

The headline architectural rule is unchanged from Phases 4 and
5B:

> Phase 1–3 evidence remains authoritative. Conversation
> history is untrusted data. JWT validation reuses Phase 5A.
> The DB role is the authoritative role; the JWT `role` claim
> is NEVER trusted for authorization.

## High-level architecture

```
nginx (port 80, public)
    │  WebSocket upgrade: GET /api/ws/conversations/{id}
    │  upgrade headers preserved, /api/ stripped
    ▼
backend (uvicorn 8000, internal only)
    │  FastAPI router: /ws/conversations/{conversation_id}
    │  1. JWT validate (Phase 5A SecurityCore) + active user + RBAC
    │  2. ConversationService.get — ownership-scoped SQL
    │  3. accept(subprotocol=…) → emit "connected"
    ▼
    Per-connection state:
        asyncio.Lock + inflight flag
        bounded request_id FIFO
        per-connection counters
    Main loop:
        receive_text (≤ 64 KiB) → parse → dispatch
            ├─ ping            → pong
            └─ user_message    → persist USER
                                  → fresh AIService call
                                  → persist ASSISTANT (or SYSTEM_EVENT)
                                  → assistant_message (or error)
    Disconnect:
        WebSocketDisconnect → clean close, log
```

The route is mounted at prefix `/ws/conversations` so the
public path is `/api/ws/conversations/{conversation_id}` after
nginx strips `/api/`. Nginx owns the WebSocket upgrade headers
and forwards the request to the backend over the internal
network.

## Transport: JWT in a subprotocol, NOT in the URL

Native browser `WebSocket` cannot set the `Authorization` header
freely, so the Phase 5A bearer token reaches the server via the
`Sec-WebSocket-Protocol` header:

```
Sec-WebSocket-Protocol: bearer.<jwt>
```

The client offers `bearer.<jwt>` as a subprotocol and the server
echoes it back when accepting the connection. This is RFC 6455
§1.9 compliant (the server MAY echo one of the offered values)
and avoids putting the JWT into the URL — nginx `access_log`
records the full URL on every request, so a token in the query
would leak into log aggregation.

Non-browser clients that CAN set `Authorization` headers
continue to work: the same `SecurityCore.decode_token` call
validates either source.

The token is NEVER:

* Read from the query string.
* Logged by the backend.
* Returned in any response event.

Tests verify this in `test_no_jwt_in_connected_event` and
`test_no_jwt_or_provider_keys_in_events`.

## Close codes (RFC 6455 + 4xxx application range)

| Condition                                    | Close code | Constant                          |
|----------------------------------------------|-----------:|-----------------------------------|
| `AUTH_ENABLED=false`                         | 1008       | `WS_CLOSE_AUTH_DISABLED`          |
| Missing / malformed / expired / bad sig JWT  | 4401       | `WS_CLOSE_UNAUTHORIZED`           |
| VIEWER role / inactive user                  | 4403       | `WS_CLOSE_FORBIDDEN`              |
| Cross-user / nonexistent conversation        | 4404       | `WS_CLOSE_NOT_FOUND`              |
| Malformed event JSON                         | 4400       | `WS_CLOSE_PROTOCOL_ERROR`         |
| Too many requests in a tight burst           | 4429       | `WS_CLOSE_TOO_MANY_REQUESTS`      |
| Internal error (defensive)                   | 1011       | (no constant — used inline only)  |

The handshake always checks auth → ownership → accept, so a
VIEWER who tries to connect to a real conversation is rejected
at the HTTP layer (never sees a `connected` event). An
already-accepted socket receives `error` events for in-band
failures (bad JSON, oversized frames, AI failure).

## Event protocol (`protocol_version = "v1"`)

Every client event is a JSON object with a discriminator
`type` field. Sending an unsupported or missing `type`
produces a sanitized `error` event with
`code=ProtocolViolation`.

### Client → server

```json
{ "type": "ping",  "ts": 1700000000 }
```

```json
{
  "type": "user_message",
  "request_id": "<UUID>",
  "question": "Why did EC2 spend go up?",
  "region": "us-east-1",
  "days": 30
}
```

* `request_id` must be a UUID. The server echoes it on every
  correlated response and uses it to deduplicate obvious
  retries (a bounded FIFO per connection).
* `question` is required, non-empty, and capped at
  `MAX_QUESTION_LENGTH` (2000 chars).
* `region` is optional; AWS regions are short, so the cap is
  64 chars.
* `days` must be one of `ALLOWED_LOOKBACK_DAYS = (7, 30, 60, 90)`
  — the same set the REST surface and cost-cache key use.

### Server → client

```json
{
  "type": "connected",
  "protocol_version": "v1",
  "conversation_id": 42,
  "user_id": 7,
  "role": "ADMIN",
  "heartbeat_interval_seconds": 30
}
```

```json
{
  "type": "user_message_accepted",
  "request_id": "…",
  "conversation_id": 42,
  "message_id": 101
}
```

```json
{
  "type": "ai_processing",
  "request_id": "…",
  "conversation_id": 42
}
```

```json
{
  "type": "assistant_message",
  "request_id": "…",
  "conversation_id": 42,
  "message_id": 102,
  "operation": "analyze",
  "model": "gpt-4o-mini",
  "answer": "…",
  "grounding": { "region": "us-east-1", "days": 30, "cost_evidence_used": true, … },
  "citations": [ … ],
  "warnings": [ … ]
}
```

```json
{
  "type": "error",
  "request_id": "…",        // optional — correlates to the client event
  "conversation_id": 42,
  "code": "AIUnavailable",
  "message": "The AI analyst is temporarily unavailable."
}
```

```json
{ "type": "pong", "ts": 1700000000 }
```

### Sanitized `error.code` registry

`WS_ERROR_CODES` (in `app/schemas/websocket.py`):

```
ProtocolViolation, InvalidJSON, UnsupportedEventType, OversizedFrame,
InvalidQuestion, InvalidLookbackDays, InvalidRequestId,
AIUnavailable, AIDisabled, AIUnreachable, Busy, Timeout,
Unauthorized, Forbidden, ConversationNotFound
```

Every `message` field is safe to surface to the end user.
Provider raw payloads, stack traces, JWTs, AWS keys, and
LiteLLM `choices` are NEVER included.

## Persistence flow

The persistence contract is unchanged from Phase 5B:

1. **USER message is persisted BEFORE the AI call.** A crash
   mid-flight never loses user input. (`add_user_message`)
2. **Fresh evidence is gathered on every call.** The handler
   invokes `AIService.generate_analysis_with_history` which
   re-runs the Phase 1–3 evidence pipeline. Prior turns are
   data, not evidence. (`AI_ESTIMATE` is still forbidden.)
3. **ASSISTANT message is persisted on success.** Provenance
   (`model_alias`, `grounding_metadata`, `citations`,
   `warnings`) is the same safe set Phase 4 already exposes.
4. **SYSTEM_EVENT is persisted on AI failure.** The error code
   is a stable identifier (e.g. `LITELLM_TIMEOUT`); the raw
   provider message is NEVER persisted.

## Concurrency: one AI operation per connection

The WebSocket main loop is sequential — `await
websocket.receive_text()` only resumes once the prior handler
returns. A second `user_message` cannot be observed while a
previous one is mid-flight. The handler still defends with:

* `asyncio.Lock` to serialize concurrent tasks if the loop is
  ever refactored to use `asyncio.gather` or background tasks.
* `state.inflight` flag — set on entry, reset on every exit
  path including exceptions. The `finally` clause is mandatory.
* `state.remember_request_id()` — bounded FIFO (128 entries)
  for duplicate suppression; a replayed `request_id` is
  answered with a sanitized `error` event (`code=Busy`) instead
  of triggering a second AI call.

Tests:

* `test_sequential_messages_processed_one_at_a_time` — two
  back-to-back messages produce exactly two complete
  accepted + ai_processing + assistant_message triples.
* `test_inflight_flag_resets_after_handler` — direct unit test
  of `_ConnectionState` lifecycle (lock + flag + FIFO).
* `test_duplicate_request_id_is_rejected` — replayed
  `request_id` returns `error` and does NOT persist a second
  USER row.

## Heartbeat

Client-driven. Clients send `{"type":"ping","ts":…}`; the
server replies with `{"type":"pong","ts":…}`. There is no
outbound heartbeat — that would needlessly wake idle
connections. Browsers detect liveness through the pong
round-trip; nginx drops idle connections after its own
timeout, which is fine because the client will reconnect.

## Prompt injection defense

The system prompt (`app/services/ai_system_prompt.py`) was
extended with a documented contract that history is wrapped in
`<conversation_history>…</conversation_history>` delimiters and
is UNTRUSTED DATA. A historical USER message that asks the
model to "ignore all instructions" must remain inside that
delimited block — never promoted into the system channel.

Tests:

* `test_hostile_history_remains_data` — an injection in turn 1
  stays inside `<conversation_history>` in turn 2's request
  body.
* `test_prompt_injection_request_remains_data` — the current
  question's injection is inside `<user_question>` and never
  appears in the system prompt.

## Input limits (defense in depth)

| Field              | Limit                | Where enforced                      |
|--------------------|----------------------|-------------------------------------|
| Raw WebSocket frame | 64 KiB              | `MAX_FRAME_BYTES` (route guard)     |
| `question` length   | 2000 chars          | `MAX_QUESTION_LENGTH` (Pydantic)    |
| `region` length     | 64 chars            | `MAX_REGION_LENGTH` (Pydantic)      |
| `days` value        | 7/30/60/90 only     | `ALLOWED_LOOKBACK_DAYS` (Pydantic)  |
| `request_id`        | RFC 4122 UUID       | `_validate_request_id` (Pydantic)   |
| Server event bytes  | 1 MiB hard cap      | `_send` helper                      |
| `request_id` FIFO   | 128 entries         | `MAX_RECENT_REQUEST_IDS`            |

Anything beyond these limits is rejected with a sanitized
`error` event — the connection stays open so a buggy client
can recover on the next frame.

## nginx configuration

A new `/api/ws/` location in `nginx/default.conf`:

* Strips `/api/` and forwards to `http://backend:8000`.
* Sets `Upgrade` and `Connection: $connection_upgrade` (where
  `$connection_upgrade` maps `upgrade` → `upgrade` and
  otherwise → `close`).
* Sets `proxy_read_timeout` and `proxy_send_timeout` to 600s
  so a real AI call (≤ `AI_REQUEST_TIMEOUT_SECONDS + slack`)
  has room to complete and the WebSocket can stay open across
  many round-trips.
* Sets `proxy_buffering off` so a slow client does not starve
  the server-side flush.

The WebSocket handshake, frame parsing, and JWT validation
remain entirely in the backend. nginx is responsible only for
the TCP upgrade and HTTP-header rewriting.

## Files added / modified

### Added

* `backend/app/api/ws_auth.py` — JWT extraction from
  `Sec-WebSocket-Protocol` / `Authorization`, token shape
  check, Phase 5A `SecurityCore.decode_token` reuse,
  authoritative DB-role resolution, RBAC, subprotocol
  negotiation, `WSAuthFailure` typed failure.
* `backend/app/api/ws_conversations.py` — WebSocket route at
  `/ws/conversations/{conversation_id}`, main loop, USER +
  ASSISTANT + SYSTEM_EVENT persistence, fresh-evidence AI
  invocation, in-flight / duplicate / heartbeat / disconnect
  handling.
* `backend/app/schemas/websocket.py` — Pydantic v2 schemas
  (`ClientUserMessage`, `ClientPing`, `ServerConnected`,
  `ServerUserMessageAccepted`, `ServerAiProcessing`,
  `ServerAssistantMessage`, `ServerError`, `ServerPong`),
  protocol constants (`PROTOCOL_VERSION`, `MAX_FRAME_BYTES`,
  `ALLOWED_LOOKBACK_DAYS`, …), `parse_client_event` helper,
  and the `WS_ERROR_CODES` registry.
* `backend/tests/test_websocket_security.py` — auth, RBAC,
  ownership / IDOR, AUTH_ENABLED=false, forged-role-claim,
  inactive-user, sanitized-error, no-secret-leakage tests.
* `backend/tests/test_websocket_protocol.py` — happy path,
  schema validation, persistence, fresh-evidence, bounded
  history, prompt-injection defense, duplicate suppression,
  concurrency (sequential + `_ConnectionState` unit),
  heartbeat, disconnect/reconnect, input limits, sanitized
  error events.
* `scripts/phase5c_verify.sh` — Phase 5B delegation, module
  import smoke test, route registration, Phase 5C pytest
  suite, Phase 5B re-run, secret scan, public-port check,
  container health, no-AI_ESTIMATE, nginx WS config presence,
  no-Redis/Kafka/SSE/Socket.IO assertion.

### Modified

* `backend/app/main.py` — mount the WebSocket router under
  `/ws/conversations`.
* `backend/app/services/ai_system_prompt.py` — document the
  `<conversation_history>` untrusted-data contract.
* `backend/pytest.ini` — set
  `asyncio_default_fixture_loop_scope = function` (the
  pytest-asyncio 0.24 future default). No existing test
  overrides the loop scope, so this is purely a
  forward-compatibility setting that also silences
  `PytestDeprecationWarning: asyncio_default_fixture_loop_scope
  is unset`.
* `nginx/default.conf` — `/api/ws/` location with the
  WebSocket upgrade headers.

## What Phase 5C explicitly does NOT do

* No Redis / Kafka / API Gateway WebSockets / SSE /
  Socket.IO. (See `phase5c_verify.sh` check #11.)
* No frontend redesign.
* No raw LiteLLM payload leakage. No `choices` array on the
  wire. No provider tokens / keys / response IDs.
* No `AI_ESTIMATE`. No invented savings. No chain-of-thought.
* No streaming tokens (only a single `assistant_message`
  envelope per accepted user_message).
* No save-of-LiteLLM-payload into JSONB columns — the same
  safe provenance set Phase 4 already exposes is persisted.
* No new mutable AWS / network surface. Only nginx publishes
  a host port.

## Failure modes & test matrix

| Scenario                                     | Server behaviour                                         | Test                                          |
|----------------------------------------------|----------------------------------------------------------|-----------------------------------------------|
| `AUTH_ENABLED=false`                         | 1008 close                                              | `test_auth_disabled_denies_ws`                |
| Missing token                                | 4401 close                                              | `test_missing_token_denied`                   |
| Malformed / expired / bad sig JWT            | 4401 close                                              | `test_malformed_token_denied`, `test_expired_token_denied`, `test_bad_signature_denied` |
| VIEWER role                                  | 4403 close                                              | `test_viewer_connection_denied`               |
| Inactive user                                | 4403 close                                              | `test_inactive_user_denied`                   |
| Token role != DB role                        | 4403 close (DB wins)                                    | `test_forged_role_claim_denied`               |
| Cross-user conversation                      | 4404 close                                              | `test_cross_user_conversation_denied`         |
| Nonexistent conversation                     | 4404 close                                              | `test_nonexistent_conversation_denied`        |
| ADMIN, own conversation                      | accept + `connected`                                    | `test_admin_can_connect_to_own_conversation`  |
| ANALYST, own conversation                    | accept + `connected`                                    | `test_analyst_can_connect_to_own_conversation`|
| Token in `Authorization` header              | accept + `connected`                                    | `test_authorization_header_also_accepted`     |
| Invalid JSON                                 | `error` (InvalidJSON), connection stays open             | `test_invalid_json_returns_sanitized_error`   |
| Unsupported event type                       | `error` (ProtocolViolation)                             | `test_unsupported_event_type`                 |
| Empty question                               | `error` (ProtocolViolation)                             | `test_empty_question_rejected`                |
| Oversized question                           | `error` (ProtocolViolation)                             | `test_oversized_question_rejected`            |
| Unsupported `days` value                     | `error` (ProtocolViolation)                             | `test_unsupported_lookback_rejected`          |
| Oversized raw frame                          | `error` (OversizedFrame)                                | `test_oversized_frame_rejected`               |
| AI call times out                            | `error` (AIUnavailable), USER persisted, SYSTEM_EVENT    | `test_user_message_persisted_before_ai_call_on_failure` |
| AI call fails (any exception)                | `error` (AIUnavailable), USER persisted, SYSTEM_EVENT    | `test_sanitized_error_event_on_ai_failure`    |
| Duplicate `request_id`                       | `error` (Busy), no second AI call, no second USER row    | `test_duplicate_request_id_is_rejected`       |
| Back-to-back messages                        | both processed sequentially                             | `test_sequential_messages_processed_one_at_a_time` |
| `_ConnectionState` lifecycle                 | lock + flag + FIFO behave as designed                    | `test_inflight_flag_resets_after_handler`     |
| Heartbeat ping                               | immediate pong round-trip                               | `test_ping_pong_heartbeat`                    |
| Normal disconnect                            | clean close, no exception                               | `test_normal_disconnect_handled`              |
| Reconnect                                    | new connection accepted, prior history preserved         | `test_reconnect_after_disconnect`             |
| Prompt injection in current question         | stays inside `<user_question>` (data)                   | `test_prompt_injection_request_remains_data`   |
| Prompt injection in prior turn               | stays inside `<conversation_history>` (data)            | `test_hostile_history_remains_data`           |
| Null savings protection                      | grounding does NOT invent savings                       | `test_null_savings_protected`                 |
| JWT / AWS key / provider-key leakage         | absent from every event                                  | `test_no_jwt_in_connected_event`, `test_no_jwt_or_provider_keys_in_events`, `test_no_aws_credentials_in_any_event` |

## Configuration

The WebSocket layer honours the existing Phase 5B / Phase 4
configuration values:

* `AUTH_ENABLED` — when `false`, the route returns 1008.
* `AI_ENABLED` — when `false`, the AI failure path returns
  `AIDisabled` with a sanitized message.
* `AI_REQUEST_TIMEOUT_SECONDS` — the AI call is bounded by
  this + 5 seconds of slack so a client disconnect does not
  leave the LiteLLM call running forever.
* `AI_MAX_HISTORY_MESSAGES`, `AI_MAX_HISTORY_CHARS` — the
  same history budgets Phase 5B already enforces.
* `CONVERSATION_TITLE_MAX_LENGTH`,
  `CONVERSATION_LIST_MAX_LIMIT`,
  `CONVERSATION_MESSAGES_MAX_LIMIT` — inherited from Phase 5B.

No new settings were added in Phase 5C.

## Known limitations (Phase 5C scope)

* No streaming tokens — every accepted `user_message` produces
  exactly one `assistant_message` (or one `error`) envelope.
  LiteLLM still supports streaming; turning it on would be a
  Phase 6 concern (out of scope).
* Single-conversation scope per WebSocket. A client that wants
  to interact with two conversations opens two sockets.
* Client-driven heartbeat only. If a client forgets to ping,
  nginx may drop the idle connection; the client reconnects.
* No reconnection hint server-side. The server emits a clean
  close frame with a 4xxx close code; the client decides how
  to react.

These are deliberate Phase 5C tradeoffs, not oversights.
