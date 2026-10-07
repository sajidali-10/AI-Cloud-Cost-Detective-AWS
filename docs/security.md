# Security

This document records the security decisions implemented across phases
0–5C and the threats that are explicitly **out of scope**. Each phase
section is self-contained and additive — older decisions are NOT
overridden by newer phases.

## Network isolation

- **Only Nginx publishes a host port**: `80:80`. Everything else is on the
  internal `internal` Docker bridge network.
- **PostgreSQL** (`:5432`) is reachable only from `backend` and `litellm`.
- **LiteLLM Gateway** (`:4000`) is reachable only from `backend`. Local debug
  access (if needed) is bound to `127.0.0.1:4000`, never `0.0.0.0`.
- **FastAPI backend** (`:8000`) is reachable only from `nginx`.

Verified by `scripts/phase0_verify.sh` (no public PG/LiteLLM ports check).

## Secrets

- No application secrets are baked into Docker images.
- Real secrets are generated locally by `scripts/generate_dev_secrets.sh`
  using `openssl rand`. The `.env` file is written with mode `600` and is
  listed in `.gitignore`.
- `.env.example` ships with placeholder values only (`CHANGE_ME`, `sk-CHANGE_ME`).
- `.env`, `.env.*`, `__pycache__`, `node_modules`, `dist`, `.venv`, and IDE
  files are excluded from Git.

## AWS credentials

- **No `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY`** in `.env.example`
  or anywhere in the codebase.
- Phase 0 does **not** call any AWS service. AWS authentication will use the
  normal AWS credential provider chain in Phase 1, preferring an EC2 IAM
  instance role (read-only).

## LiteLLM Gateway access

- `LITELLM_MASTER_KEY` is injected into `litellm` and `backend` containers
  via environment variables from `.env`. It is **never** exposed to the
  browser.
- The browser reaches the backend via `/api/*`; the backend is the only
  caller of LiteLLM.
- Phase 0 configures zero provider models in `litellm/config.yaml` — no
  OpenAI, no Anthropic, no Google Gemini, no AWS Bedrock, no OpenRouter, no
  Ollama. No API keys. No mock production claims.

## CORS

- The frontend and backend share the same origin (served by Nginx), so
  permissive CORS is unnecessary.
- The `CORS_ALLOWED_ORIGINS` setting is **environment-driven** (comma-separated).
- The combination `allow_origins=["*"]` + credentials is explicitly forbidden
  in the codebase.

## Frontend bundle hygiene

The production build (`frontend/dist/`) must contain **none** of:

- `localhost:8000`
- `127.0.0.1:8000`
- EC2 public IP addresses
- `:4000` (LiteLLM port)
- `:5432` (PostgreSQL port)

The dev-time Vite proxy in `frontend/vite.config.ts` is for local development
only and points at the Compose service name `backend`, never at a public IP.

## Docker security

- **No privileged containers.**
- **No `/var/run/docker.sock` mounts** inside application containers.
- **No host networking** (`network_mode: host`).
- **Non-root users** in the `backend` and `frontend` runtime stages.
- **Named volumes** for PostgreSQL data persistence.
- **Pinned image tags** for all services (no `latest`); LiteLLM is pinned to
  `v1.55.2` and recorded in `docs/phase0-report.md`.
- **`.dockerignore`** files for both `backend/` and `frontend/`.

## Authentication

- **No JWT auth in Phase 0.** Authentication is documented under "Deferred
  to later phase" in `docs/phase0-report.md`.

## Phase 5A additions (Authentication + RBAC)

- **Argon2id password hashing.** All passwords are stored as Argon2id
  PHC strings (defaults `t=3, m=64MiB, p=2`). The Argon2id verifier is
  the only module that ever compares a plaintext password to a stored
  hash; routes never see either the plaintext or the hash.
- **Stateless bearer JWT (HS256 default).** Tokens carry `sub`, `role`
  (hint only), `iat`, `exp`, `iss`, `aud`, `jti`. The auth layer
  re-loads the user from the DB on every request and uses the DB
  role for authorization — a forged role claim cannot escalate.
- **Fail-closed secret validation.** When `AUTH_ENABLED=true`, the
  application refuses to start unless `JWT_SECRET` is at least 32
  characters and not a documented placeholder. The error message is
  sanitized; it never includes the secret value.
- **User-enumeration defence.** Every login failure (unknown email,
  wrong password, inactive account) returns an identical
  `InvalidCredentials` 401 with the same `message` value.
- **AWS credentials are unchanged.** Backend AWS access continues via
  EC2 IAM Role + IMDSv2. JWT claims never translate to AWS IAM
  permissions. Application `ADMIN` is **not** AWS administrator. AWS
  APIs remain read-only — Phase 5A introduces no mutation endpoints.
- **Log safety.** Logs may include user_id, role, and request id, but
  never passwords, JWTs, Authorization headers, password hashes, or
  the JWT signing secret. `phase5a_verify.sh` scans the last 1000
  lines of backend logs and fails on any credential pattern.
- **No auto-generated admin.** The first administrator is created via
  `scripts/create_admin.py`; the application does NOT create a
  default admin on startup.
- **Backward compatibility.** `AUTH_ENABLED=false` keeps the
  Phase 0-4 verification suite working unchanged.

See `docs/phase5a-auth-rbac.md` for the full architecture, role
matrix, and API surface.

## Automated remediation

- **No automated AWS remediation in Phase 0.** All AWS calls (none in
  Phase 0) will be read-only.

## Secret-pattern scanning

`scripts/phase0_verify.sh` runs `git grep` over tracked files for the
following patterns and aborts the verification (without printing the value)
if any match is found:

- `AKIA...` (AWS access key prefix)
- `AWS_SECRET_ACCESS_KEY=`
- `OPENAI_API_KEY=`
- `ANTHROPIC_API_KEY=`
- `GEMINI_API_KEY=`
- `LITELLM_MASTER_KEY=sk-...` (long random keys, not the `sk-CHANGE_ME`
  placeholder)

## Out of scope for Phase 0 (deferred)

- HTTPS / TLS termination at Nginx (planned for a later phase).
- JWT or session authentication.
- WAF, rate limiting beyond Nginx defaults, or DDoS protection.
- Intrusion detection, audit logging at the application layer.
- AWS IAM role provisioning (will be done in Phase 1 on the EC2 host).

## Phase 3 additions

* **No AWS mutation API is ever called.** The Phase 3 verifier
  explicitly greps the source tree for the four forbidden
  optimization operations — `update_enrollment_status`,
  `update_preferences`, `put_recommendation_preferences`,
  `delete_recommendation_preferences` — and any other call site
  whose prefix matches `create_`, `delete_`, `put_`, `update_`,
  `modify_`, `terminate`, `stop_`, `start_`, `attach_`, `detach_`,
  `associate_`, `disassociate_`, `release_`, `enable_`, `disable_`,
  `reboot_`, `restore_`, `reset_`, `cancel_`, `send_`,
  `publish_`, `invoke_`.  Database writes (the Phase 2 cost cache)
  are unaffected.
* **No AI / LiteLLM call paths in Phase 3.** The verifier greps the
  Phase 3 source tree for `litellm`, `openai`, `openrouter`,
  `gemini`, `anthropic`, `LLM`, `completion`.  Phase 3 is purely
  deterministic and AWS-native.
* **Recommendation IDs are stable and never expose AWS keys.** The
  public `recommendation_id` is a SHA-256 of
  `(account_id, region, resource_id, action.value)`; AWS
  `recommendationId` is captured only in the `aws_recommendation_ids`
  list (not used as a primary key) and never persisted.
* **Per-source failures never destroy valid recommendations.** Each
  AWS-native source emits a structured `OptimizationWarning` on
  failure and the orchestrator continues to compose the deduplicated
  list from the remaining sources.
* **Capabilities endpoint is the truth source.** It surfaces the
  enrollment state of every AWS-native source so a caller can
  decide whether deterministic rules are the only signal available.

## Phase 5B additions (Conversation + AI history persistence)

* **Per-user ownership enforced at the SQL layer.** Every query
  that reads or writes a conversation includes
  `WHERE id = :cid AND user_id = :uid`. A conversation that does
  not exist OR is owned by another user returns the same
  `ConversationNotFound` error; the API returns a 404 envelope
  with no information leak about whether the ID exists.
* **IDOR defence.** Tests
  `tests/test_conversation_security.py` and
  `tests/test_conversation_routes.py` assert that cross-user
  read, update, delete, and analyze attempts are all denied with
  404 (read/analyze) / 400 (rename on missing title) envelopes.
* **RBAC centralization.** All conversation routes use
  `Depends(require_role("ADMIN", "ANALYST"))`. VIEWER is denied
  every conversation route — including `/analyze`, which means a
  VIEWER cannot use the conversation layer to bypass Phase 4's
  AI RBAC.
* **AUTH_DISABLED handling.** With `AUTH_ENABLED=false`, every
  durable conversation endpoint returns a controlled
  `503 AuthDisabled` response. There is no anonymous persistent
  ownership; the verifier asserts this end-to-end.
* **Prompt-injection defence for stored history.** Historical
  user/assistant turns are wrapped in
  `<conversation_history>…</conversation_history>` delimiters in
  the user-message body. They are never promoted to the system
  channel. Test
  `test_stored_history_prompt_injection_does_not_override_system_prompt`
  plants an adversarial historical message and asserts the
  message stays inside the history block (data) and does NOT
  appear in the system prompt or in the question block.
* **Savings protection preserved.** Phase 3 null-savings
  protection is unchanged. There is no `AI_ESTIMATE` token
  anywhere in the code, schema, responses, or persisted rows
  (the verifier greps for it).
* **No raw provider payload persisted.** The conversation
  message rows store only safe provenance
  (`operation_type`, `model_alias`, `grounding_metadata`,
  `evidence_references`, `warnings`). `token_usage` is left NULL
  by the analyze path; `LiteLLMClient.raw` is intentionally never
  stored. Test
  `test_no_raw_provider_payload_persisted` asserts that the
  `choices` field and full token-usage strings do not appear in
  any persisted message row.
* **No secrets persisted.** Conversation messages never carry
  Authorization headers, JWTs, AWS credentials, IMDS tokens, or
  provider keys. Test `test_no_jwt_or_authorization_persisted`
  plants the caller's own JWT in the question and asserts the
  JWT does not appear in the assistant row or its grounding
  metadata.
* **Log privacy.** The conversation service logger emits
  `conversation_id`, `user_id`, `message_id`, `role`, and
  `operation` only. It never logs `content`, JWTs, AWS
  credentials, full prompts, or raw provider payloads.
* **Failure semantics.** On a LiteLLM failure, a `SYSTEM_EVENT`
  row is persisted with a sanitized stable code
  (`LITELLM_TIMEOUT`, `LITELLM_UNAVAILABLE`, `LITELLM_RATE_LIMIT`,
  `LITELLM_AUTH`, `LITELLM_QUOTA_EXHAUSTED`,
  `LITELLM_PROVIDER_ERROR`, `LITELLM_MALFORMED_RESPONSE`,
  `LITELLM_EMPTY_COMPLETION`, `AI_UNAVAILABLE` fallback). The raw
  provider message is NEVER persisted. The user message is
  always persisted before the AI call so a LiteLLM failure does
  not lose user input.
* **Hard delete with CASCADE.** `DELETE /conversations/{id}`
  removes the row; the FK cascade removes every message. No soft
  delete in Phase 5B. Archive (`PATCH is_archived=true`) is
  separate from delete.
* **No WebSockets, no streaming, no SSE, no Redis pub/sub.**
  Phase 5B is REST only. The verifier greps `backend/app` for
  `WebSocket`, `StreamingResponse`, `EventSourceResponse`,
  `Socket.IO`, `redis.pubsub`, `aioredis` and fails if any are
  present.
* **No AI_ESTIMATE token.** The verifier greps the source tree
  for `AI_ESTIMATE` and fails if it appears anywhere.

## Phase 5C additions (Secure WebSocket realtime layer)

* **JWT validation reuses Phase 5A.** The WS layer calls
  `SecurityCore.decode_token` from
  `app/core/security.py`; no JWT parsing, signature
  verification, or role decision is duplicated.
* **Token transport is a header, never the URL.** Native browser
  `WebSocket` cannot set the `Authorization` header freely, so
  the bearer token travels in the `Sec-WebSocket-Protocol`
  header (`bearer.<jwt>`). nginx `access_log` records the URL
  on every request, so a token in the query would leak into
  log aggregation. The token is NEVER echoed in any response
  event and is NEVER logged.
* **Authoritative DB role, not JWT role claim.** The
  `resolve_ws_principal` helper loads the user via
  `AuthService.get_active_user` and rejects users whose DB
  role is `VIEWER` (or anything other than `ADMIN`/`ANALYST`)
  even if the JWT `role` claim says `ADMIN`. The Phase 5A
  contract is preserved end-to-end.
* **`AUTH_ENABLED=false` denies durable WS conversations.**
  The handshake closes the upgrade with 1008 (policy
  violation). No anonymous persistent ownership is invented.
* **Cross-user / nonexistent conversations collapse to the
  same 4404 close code.** `ConversationService.get` raises
  `ConversationNotFound` for both cases so a caller cannot
  enumerate IDs by distinguishing "not found" from "not
  yours". Even an `ADMIN` cannot read another user's private
  conversation merely because they are `ADMIN` — the SQL
  guard is `WHERE id = :cid AND user_id = :uid`.
* **Fresh Phase 1–3 evidence remains authoritative.** Every
  `user_message` invokes `AIService.generate_analysis_with_history`
  which re-runs the Phase 1–3 evidence pipeline. Prior turns
  are data, not evidence, and are wrapped in
  `<conversation_history>…</conversation_history>` delimiters.
* **No `AI_ESTIMATE`, no invented savings.** The Phase 3
  null-savings protection is preserved end-to-end. The
  grounding metadata shape is unchanged from Phase 4.
* **No secret / JWT / AWS / provider-credential logging.**
  Every `error.message` is sanitized. Tests
  `test_no_jwt_in_connected_event`,
  `test_no_aws_credentials_in_any_event`, and
  `test_no_jwt_or_provider_keys_in_events` assert the
  absence of `AKIA`, `sk-`, `Bearer `, `secret`, and the
  caller-supplied JWT from every event payload.
* **No raw LiteLLM payload leakage.** The wire envelope
  exposes only `answer`, `model`, `grounding`, `citations`,
  `warnings`. The `choices` array and any other provider
  detail are NEVER emitted. Stable codes
  (`LITELLM_TIMEOUT`, `LITELLM_RATE_LIMIT`, etc.) are used
  for failure surfacing; raw provider messages are
  discarded.
* **One bounded AI operation per connection.** The handler
  uses an `asyncio.Lock` and an `inflight` flag, set on
  entry and reset on every exit path including exceptions.
  Duplicate `request_id` values are deduplicated via a
  bounded FIFO and answered with a sanitized `error`
  (`code=Busy`).
* **Input limits (defense in depth).** Raw WebSocket frames
  are capped at 64 KiB; questions at 2000 chars; regions at
  64 chars; `days` must be one of `(7, 30, 60, 90)`;
  `request_id` must be a UUID; server events are capped at
  1 MiB. Anything beyond these limits is rejected with a
  sanitized `error` event so a buggy client can recover.
* **Normal disconnects handled cleanly.** `WebSocketDisconnect`
  is caught, the database session is closed in a `finally`
  block, and a sanitized log line is emitted. The client
  can reconnect; the prior history is preserved.
* **nginx remains the only publicly exposed service.** A new
  `/api/ws/` location was added with the WebSocket upgrade
  headers and generous AI-friendly timeouts. Verified by
  `scripts/phase5c_verify.sh` (check #7 — no public port
  other than nginx; check #10 — nginx WS config present).
* **No Redis / Kafka / API Gateway WebSockets / SSE /
  Socket.IO introduced.** Verified by
  `scripts/phase5c_verify.sh` (check #11).
