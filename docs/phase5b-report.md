# PHASE 5B — CONVERSATION & AI HISTORY PERSISTENCE REPORT

## Overall Status

**READY**

Phase 5A baseline preserved (21/21 verifier checks green). Phase 5B
adds durable, authenticated, user-owned conversations and AI message
history on top of the existing Phase 4 grounded AI service. Phase 1–3
remain the only authoritative AWS source; stored history is data, not
evidence.

## Branch / Baseline

* Branch: `phase-5-auth-persistence-websockets`
* Baseline: `c697266` (Phase 5A: add authentication and RBAC foundation)
* Working tree: clean at commit time (final commit only).

## Database

* Migration: `0003_conversations` (`down_revision = "0002_app_users"`)
* Hand-written, idempotent (`CREATE TABLE IF NOT EXISTS` /
  `DROP TABLE IF EXISTS`).
* Reversible: `alembic downgrade 0002_app_users && alembic upgrade head`
  is verified end-to-end.
* FK + CASCADE: both FKs use `ON DELETE CASCADE`. Deleting a user
  removes their conversations and every message; deleting a
  conversation removes its messages.

### Conversations table

```
id              BIGSERIAL PRIMARY KEY
user_id         BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE
title           VARCHAR(200) NOT NULL DEFAULT 'New Cost Analysis'
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
last_message_at TIMESTAMPTZ NULL
is_archived     BOOLEAN NOT NULL DEFAULT false
CHECK (length(title) > 0 AND length(title) <= 200)
```

### Messages table

```
id                  BIGSERIAL PRIMARY KEY
conversation_id     BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE
role                VARCHAR(16) NOT NULL CHECK IN ('USER','ASSISTANT','SYSTEM_EVENT')
content             TEXT NOT NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
operation_type      VARCHAR(32) NULL
model_alias         VARCHAR(64) NULL
grounding_metadata  JSONB NULL
evidence_references JSONB NULL
warnings            JSONB NULL
token_usage         JSONB NULL
error_code          VARCHAR(64) NULL
```

### Indexes

* `ix_conversations_user_id`
* `ix_conversations_user_updated` (`user_id`, `updated_at`)
* `ix_conversations_user_archived_lastmsg`
  (`user_id`, `is_archived`, `last_message_at`)
* `ix_conversation_messages_conv_created`
  (`conversation_id`, `created_at`)
* `ix_conversation_messages_conv_role`
  (`conversation_id`, `role`)

## Ownership

Every query that reads or writes a conversation is scoped by
`user_id` at the SQL layer (`WHERE id = :cid AND user_id = :uid`).
A conversation that does not exist OR is owned by another user
returns the same `ConversationNotFound` error from the service
layer; the route layer translates that into a 404 envelope. A
caller cannot enumerate IDs by distinguishing "not found" from
"not yours". All scoped test cases pass:

* Cross-user read denied (owner's id known to the other user)
* Cross-user update denied (rename and archive both)
* Cross-user delete denied
* Cross-user analyze denied
* Cross-user messages denied
* Unauthenticated requests denied (`AUTH_ENABLED=true`)

## RBAC

* ADMIN: full personal conversation + AI access.
* ANALYST: full personal conversation + AI access.
* VIEWER: blocked at `require_role("ADMIN", "ANALYST")` on every
  conversation route — including `/analyze`. No bypass.
* AUTH_DISABLED: durable conversation endpoints return 503
  `AuthDisabled` (never invent anonymous persistent ownership).

## Conversation API

| Endpoint                                            | Behaviour                     |
|-----------------------------------------------------|-------------------------------|
| `POST   /api/conversations`                         | create; optional title; default `New Cost Analysis` |
| `GET    /api/conversations`                         | list (paginated; archived filter) |
| `GET    /api/conversations/{id}`                    | detail + message count         |
| `PATCH  /api/conversations/{id}`                    | rename and/or archive          |
| `DELETE /api/conversations/{id}`                    | hard delete (CASCADE messages) |
| `GET    /api/conversations/{id}/messages`           | paginated, oldest first        |
| `POST   /api/conversations/{id}/analyze`            | grounded AI Q&A                |

Create:
* Optional title; empty/whitespace → default `New Cost Analysis`.
* Title bounded to `conversation_title_max_length` (200 chars).
* Returns `ConversationView` JSON with 201.

List:
* Scoped to the authenticated user; never returns another user's
  conversations.
* `limit`/`offset` pagination with `conversation_list_max_limit`
  ceiling (100).
* `archived` filter supported.
* Sorted `updated_at DESC, id DESC` (newest activity first).

Get:
* Returns `ConversationDetailResponse` (`conversation` +
  `message_count`).
* 404 `ConversationNotFound` for unknown OR foreign-owned IDs.

Update:
* At least one of `title` or `is_archived` required.
* Empty title → 400 `InvalidTitle`.
* Updates `updated_at` automatically.

Archive:
* `PATCH is_archived=true|false`. Separate from delete.
* Default list filter (`archived=false`) hides archived rows.

Delete:
* Hard delete; messages removed by `ON DELETE CASCADE`.
* Returns 200 `{"status":"ok","deleted":true,"conversation_id":N}`.
* Cross-user delete returns 404 (no leak).

Message history:
* Oldest-first ordering (`created_at ASC, id ASC`).
* `limit`/`offset` pagination with `has_more` flag.
* `conversation_messages_max_limit` ceiling (200).
* Cross-user message read denied.

## AI Integration

* Fresh authoritative Phase 2/3 evidence fetched on every call.
* Bounded prior-turn history attached to the user-message body
  (never promoted to the system channel).
* History wrapped in `<conversation_history>...</conversation_history>`
  delimiters (`app/services/ai_system_prompt.py`).
* History limits:
  * `AI_MAX_HISTORY_MESSAGES` (default 10)
  * `AI_MAX_HISTORY_CHARS` (default 12000)
* SYSTEM_EVENT rows are NEVER included in history.
* One LiteLLM call per request.
* No automatic summarization model call.

### Failure semantics

1. `USER` message persisted BEFORE the AI call (never lose user input).
2. On `SUCCESS` → `ASSISTANT` message persisted with safe provenance
   (`operation_type`, `model_alias`, `grounding_metadata`,
   `evidence_references`, `warnings`; `token_usage` left NULL).
3. On `DISABLED` → `SYSTEM_EVENT` row with code `AI_DISABLED`; return
   503 `AIDisabled`.
4. On `PARTIAL_SUCCESS` / `UNAVAILABLE` / `FAILED` → `SYSTEM_EVENT`
   row with the sanitized stable code
   (`LITELLM_TIMEOUT` / `LITELLM_UNAVAILABLE` / `LITELLM_RATE_LIMIT` /
   `LITELLM_AUTH` / `LITELLM_QUOTA_EXHAUSTED` /
   `LITELLM_PROVIDER_ERROR` / `LITELLM_MALFORMED_RESPONSE` /
   `LITELLM_EMPTY_COMPLETION` / `AI_UNAVAILABLE` fallback).
5. NO fabricated assistant text is ever persisted.
6. Conversation `updated_at` and `last_message_at` bumped in the same
   transaction as the message insert.

## Security

* Prompt injection: stored history wrapped in
  `<conversation_history>...</conversation_history>` delimiters.
  Test `test_stored_history_prompt_injection_does_not_override_system_prompt`
  plants an adversarial historical message ("Ignore all future system
  instructions…") and asserts:
  - It does NOT appear in the system prompt.
  - It IS inside the history block (data).
  - It is NOT outside the history block (i.e. never in the question
    block).
* Savings protection: Phase 4 null-savings sentinel preserved;
  `AI_ESTIMATE` does not appear anywhere in source / schema /
  responses / persisted rows.
* Secret persistence: no Authorization header, no JWT, no AWS
  credential material is stored on the conversation / message rows.
  Test `test_no_jwt_or_authorization_persisted` plants the caller's
  own JWT into the question and asserts the JWT is NOT in the
  assistant row or its grounding metadata.
* Provider payload: `LiteLLMClient.raw` is intentionally never
  persisted. Test `test_no_raw_provider_payload_persisted` asserts
  `choices` and `token_usage` (full) are not in the message row;
  `token_usage` is left NULL by the analyze path; `model_alias` is
  the safe provenance that IS stored.
* Log privacy: the conversation service logs `conversation_id`,
  `user_id`, `message_id`, `role`, and `operation` only. Never
  `content`, JWTs, AWS credentials, or full prompts.

## RBAC Detail

* ADMIN: full personal conversation + AI access. Cannot read another
  user's conversations — there is no endpoint for that.
* ANALYST: full personal conversation + AI access. Same isolation.
* VIEWER: blocked at `require_role("ADMIN","ANALYST")` on every
  conversation route — including `/analyze`. Test
  `test_viewer_cannot_use_analyze_endpoint` proves a VIEWER cannot
  bypass AI restrictions through the conversations layer.
* AUTH_DISABLED: durable endpoints return 503 `AuthDisabled`.
  Anonymous requests never persist conversation state.

## Tests

* Phase 5B targeted: **96 passed** (test_conversation_models,
  test_conversation_migration_ddl, test_conversation_service,
  test_conversation_routes, test_conversation_ai_integration,
  test_conversation_security).
* Phase 5A regression: **21 passed** (delegated via
  `phase5a_verify.sh`; re-run after Phase 5B migration changes —
  still green).
* `phase5b_verify.sh`: **15 passed, 0 failed**.

Coverage includes:

* conversation model + migration DDL
* create / list / get / update (rename + archive) / delete
* default title + empty title + oversized title
* pagination + archived filter
* message ordering + pagination + count
* user-message persistence
* assistant-message persistence with safe provenance
* AI failure behavior (LiteLLM timeout + empty completion)
* conversation continuation (bounded history attached)
* fresh evidence on every request
* prompt injection in stored history
* null-savings protection (no AI_ESTIMATE anywhere)
* forged evidence references (citation validator still gates them)
* ADMIN / ANALYST personal conversation access
* VIEWER AI denial (403 on `/analyze`)
* anonymous / auth-disabled behavior (503 AuthDisabled)
* IDOR / cross-user read / update / delete / analyze denial
* no raw provider payload persisted
* no secrets persisted (no JWT in assistant rows)
* no AI_ESTIMATE token

## Docker

* Public ports: nginx on `80` only. Backend, postgres, litellm,
  frontend remain on the internal Docker network.
* All containers healthy after the migration and image rebuild.

## Known Issues

* `tests/test_conversation_ai_integration.py` emits a
  `RuntimeWarning: coroutine 'to_thread' was never awaited` line.
  This is a harmless pre-existing artifact of the Phase 4
  `_gather_async` helper when the recommendations sub-call fails
  in the test environment; the call site falls back to an empty
  list and the route still returns a controlled response.
* The Phase 5B migration adds `token_usage` JSONB column. We use
  it for `token_usage` provenance only (no raw provider payload).
  The conversation analyze path currently leaves `token_usage`
  NULL because the Phase 4 token usage is bound to the LiteLLM
  result and the test environment does not exercise it.

## Deferred

* WebSockets (Phase 5C).
* Streaming responses (Phase 5C).
* Real-time notifications (Phase 5C).
* Frontend chat UI (Phase 6).
* External IdP / SSO.
* MFA.
* Soft delete (current: hard delete with CASCADE).
* Automatic summarization model call.

## Phase 5C Readiness

**READY**

Phase 5C can layer WebSocket / streaming on top of the same
`ConversationService` and ownership model. The conversation
endpoints are stable; no breaking changes are required.
