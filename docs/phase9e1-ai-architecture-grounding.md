# Phase 9E.1 — AI Architecture + Grounding Contract

**Status:** DESIGN GATE (architecture only — no runtime code modified)
**Branch:** `phase-9e1-ai-architecture-grounding` (from `main` @ `3670b6c8205577074044ebbe88ded4052b77793e`)
**Date:** 2026-10-30
**Runtime state at time of writing:** `AI_ENABLED=false`, LiteLLM stopped, no LiteLLM container running, all FinOps surfaces operational.

---

## 1. Executive summary

The repository already contains a **real, fully-implemented, security-hardened AI stack** — not a mock. Two parallel AI generations exist:

1. **Phase 4 "AI Cost Analyst"** (`/api/ai/*`) — AWS-only, evidence-grounded Q&A / executive summary / recommendation explanation, backed by real LiteLLM gateway calls, citation validation, and sanitized failure envelopes. Powers the WebSocket conversation experience (Phases 5B/5C/6C).
2. **Phase 7D.4 "Multi-cloud AI FinOps Analyst"** (`/api/cloud/ai/*`) — the canonical path: it reuses the **same provider registry, adapters, and envelope builders as the unified dashboard**, builds a bounded deterministic grounding context (AWS + Azure), renders prompt-safe evidence, calls the same LiteLLM client, and returns **server-side deterministic evidence cards** alongside the model's prose. Failure and disabled states degrade to model-free deterministic answers.

Everything is wired and tested end-to-end (with mocked transports in tests), but **no real LLM has ever been reachable in this deployment**:

- `AI_ENABLED=false` (`.env`) — every generation endpoint returns a controlled DISABLED envelope without contacting LiteLLM or cloud APIs (verified live: `/ai/status` → `{"status":"DISABLED","ai_enabled":false,...}`).
- `LITELLM_API_KEY` is **unset** — no upstream provider credential exists.
- `LITELLM_MODEL=cost-detective-free` — not a resolvable LiteLLM provider string; also a **config drift**: the gateway config only defines the alias `cost-analyst`, while the backend now sends `model=cost-detective-free`.
- The `ai` Docker network is `internal: true` (Phase 8 anti-exfiltration control) — **LiteLLM cannot initiate outbound internet connections**, so no public LLM provider is reachable even with a key, until an explicit egress design is chosen.

Phase 9E.1 therefore establishes: the grounding principle, a provider-neutral grounding envelope (extending the already-canonical Phase 7D.4 context builder to Rackspace), a structured response contract with layered enforcement, provider/account isolation rules, AI_ENABLED state behaviour, cost controls, audit design, and prompt-injection mitigation — and defines the exact Phase 9E.2 implementation scope.

**The single most important architectural conclusion:** the grounding half of the target architecture already exists and is production quality (`FinOpsContextBuilder` + canonical adapters). Phase 9E.2 is an *extension and hardening* effort (Rackspace grounding, structured response schema, post-response validation, audit, per-user limits, config alignment) — not a rewrite.

---

## 2. Current-state AI architecture

### 2.1 Components (all paths verified this session)

| Layer | File(s) | Role |
|---|---|---|
| HTTP routes (legacy AWS) | `backend/app/api/ai.py` | `/api/ai/status`, `/api/ai/executive-summary`, `/api/ai/analyze`, `/api/ai/recommendations/{id}/explain` |
| HTTP routes (canonical) | `backend/app/api/cloud_ai.py` | `/api/cloud/ai/status`, `/api/cloud/ai/analyze` |
| Service (legacy AWS) | `backend/app/services/ai_service.py` | `AIService`: disabled state, evidence gathering (Phase 2/3 services), prompt assembly, LiteLLM call, citation validation, sanitized failures |
| Context builder (AWS) | `backend/app/services/ai_context_builder.py` | `AIContextBuilder`: bounded evidence view (top services/regions/recs), limitations, citation index, `render_context_text` |
| System prompt (AWS) | `backend/app/services/ai_system_prompt.py` | Grounding contract, savings-protection sentinel, injection defence, `<aws_evidence>` / `<user_question>` / `<conversation_history>` delimiters |
| Service (multi-cloud) | `backend/app/services/cloud/finops_ai_service.py` | `FinOpsAIService`: canonical context → prompt → LiteLLM → answer + deterministic evidence cards; deterministic fallback answers |
| Context builder (multi-cloud) | `backend/app/services/cloud/finops_context.py` | `FinOpsContextBuilder`: bounded slices (top-10 services, top-8 regions, top-10 recs, 30-point trend), freshness/status classification, `None` preservation |
| System prompt (multi-cloud) | `backend/app/services/cloud/finops_prompt.py` | `<finops_context>` grounding contract, multi-cloud savings-period rules, injection defence |
| Schemas | `backend/app/schemas/ai.py`, `backend/app/schemas/cloud_ai.py` | `AIResponse`/`AIStatusResponse`; `FinOpsAnalyzeRequest`/`FinOpsAIAnswer`/`FinOpsEvidence`/`FinOpsAIStatus` |
| LLM transport | `backend/app/services/litellm_client.py` | `LiteLLMClient`: httpx → `POST /v1/chat/completions`, health probe, sanitized exception taxonomy, secret redaction |
| Conversations (persistence) | `backend/app/services/conversation_service.py`, `backend/app/api/conversations.py` | Bounded history (`render_history_block`, 10 msgs / 12 000 chars), ownership-scoped CRUD |
| Conversations (realtime) | `backend/app/api/ws_conversations.py`, `backend/app/api/ws_auth.py` | WS protocol at `/api/ws/conversations/{id}`: token via subprotocol, role checks, per-connection AI serialization, FIFO `request_id` dedup, persistence before/after AI call |
| Gateway config | `litellm/config.yaml` | Alias `cost-analyst` → `os.environ/LITELLM_MODEL` + `os.environ/LITELLM_API_KEY`; `drop_params`, telemetry off, request timeout 30 s, 1 retry; Postgres-backed spend tracking |
| Gateway deployment | `docker-compose.yml` (service `litellm`, image `ghcr.io/berriai/litellm:v1.104.0`) | Internal-only (`ai` network, `internal: true`); no published ports; `depends_on: service_started` (not healthy) so LiteLLM outage cannot block the dashboard |
| Backend AI settings | `backend/app/core/config.py` (fields `ai_*`, `litellm_*`) | `ai_enabled` (default False), timeout 30 s, max output 800 tokens, context caps (20 recs / 15 services / 10 regions / 2 000-char question / 10 history msgs / 12 000 history chars) |
| Frontend (conversation) | `frontend/src/pages/AIAnalystPage.tsx`, `frontend/src/lib/ai/{api,connection,protocol,markdown}.tsx?` | WS-based chat UX over the Phase 4 service |
| Frontend (canonical) | `frontend/src/pages/CloudAIPage.tsx`, `frontend/src/lib/cloudAi.ts` | Provider/scope/period-following single-shot Q&A with evidence cards + status badges |
| Tests | `backend/tests/test_ai_*.py`, `test_litellm_client.py`, `test_finops_ai.py`, `test_conversation_*.py`, `test_phase7a_ai_status_safety.py`, `test_websocket_*.py` | Grounding, safety, sanitization, protocol, RBAC coverage (all passing at Phase 9D.2 gate) |

### 2.2 Real-vs-mock classification

| Component | Class | Evidence |
|---|---|---|
| `LiteLLMClient` (httpx → `/v1/chat/completions`) | **LIVE** (code path real; currently unreachable at runtime) | Real HTTP client; no provider SDK anywhere in the backend |
| Phase 4 `AIService` + context builder + system prompt | **LIVE** | Real evidence from Phase 2/3 services; real gateway call; citation validation |
| Phase 7D.4 `FinOpsAIService` + `FinOpsContextBuilder` | **LIVE** | Reuses `ProviderRegistry` + adapters; same envelope helpers as `/api/cloud/{costs,resources,recommendations}` |
| LiteLLM gateway (`litellm/config.yaml`) | **PARTIAL** | Deployment exists and is hardened, but no upstream model/credential resolves; image pinned `v1.104.0`; not running |
| Conversations + WS realtime | **LIVE** | Real persistence, real auth, real protocol; AI call reuses Phase 4 service |
| Frontend AI pages | **LIVE** | Real API/WS clients consuming the real envelopes; graceful disabled states |
| Mock/demo AI data in production paths | **NONE** | No mock generators, canned answers, or fake "AI" text exist in `app/` — the only mocks are test transports (`httpx.MockTransport`, fakes), which is correct practice |
| Rackspace AI grounding | **STUB (absent by design)** | `FinOpsAIProvider` enum = {AWS, AZURE}; context builder logs `rackspace_out_of_scope_9b1` and returns `None` for the Rackspace cost slice |
| Upstream LLM reachability | **DEAD (deliberate)** | `internal: true` AI network + no API key + non-resolvable `LITELLM_MODEL` value |

### 2.3 Explicit answers (required by the phase brief)

- **Does the current AI Cost Advisor invoke a real LLM?**
  The code path is a real LLM invocation path (`LiteLLMClient.complete` → gateway → provider), and **nothing in the response chain is fabricated**. In this deployment it has never completed against a real upstream: no credential, no egress, `AI_ENABLED=false`. When invoked today it returns controlled `DISABLED`/`UNAVAILABLE` envelopes — never fake analysis.
- **Which model/provider?** None configured. The gateway resolves `os.environ/LITELLM_MODEL` + `os.environ/LITELLM_API_KEY`; both are effectively unset (`LITELLM_MODEL=cost-detective-free` is a placeholder that matches no gateway alias and no LiteLLM provider string). The backend's `model_alias` has drifted to `cost-detective-free` while the gateway serves only `cost-analyst`.
- **Through what path?** Browser → nginx (`/api/...` strip) → FastAPI route (`require_role("ADMIN","ANALYST")`) → `AIService` / `FinOpsAIService` → deterministic context builder → `LiteLLMClient` → `http://litellm:4000/v1/chat/completions` (OpenAI-compatible) → upstream provider (unreachable today).
- **What happens when `AI_ENABLED=false`?** (verified live)
  - `/api/ai/status` → 200 `{"status":"DISABLED","ai_enabled":false,"litellm_reachable":false,"provider_configured":false,"model_alias":"cost-detective-free","required_env_var":"LITELLM_API_KEY",...}`.
  - `/api/ai/*` generation → 200 `AIResponse(status=DISABLED, warnings=["AI_DISABLED"])`; AWS and LiteLLM are deliberately **not contacted** (disabled path avoids importing the AWS evidence stack).
  - `/api/cloud/ai/analyze` → canonical context **is still gathered** (deterministic, read-only) and the response carries **real evidence cards + a model-free deterministic answer** with `status=DISABLED`. This is honest degradation, not mock AI.
  - Dashboard, costs, resources, recommendations, conversations UI: fully operational; LiteLLM outage cannot cascade (`depends_on: service_started`).
- **What happens when LiteLLM is unavailable?** Health probe fails → status `DEGRADED` (Phase 4) / `UNAVAILABLE` (7D.4); generation → sanitized `UNAVAILABLE` envelope (`litellm_unavailable`/`timeout`/... codes); WS → `error` event `code=AIUnavailable` with the user message persisted and **no fabricated assistant text**.
- **Is any current AI answer generated entirely from mock/static data?** **No.** All answers are either (a) grounded model output over canonical evidence, or (b) explicitly-labelled deterministic/status answers. There is no path that presents mock data as AI analysis.

---

## 3. Current request flow (end to end)

### 3.1 Canonical single-shot Q&A (`POST /api/cloud/ai/analyze`)

```text
Browser (CloudAIPage.tsx)
  │  POST /api/cloud/ai/analyze {provider, scope, question, period?, region?}
  │  bearer JWT (useFinopsQuery / apiFetch)
  ▼
nginx  (strips /api/ ; TLS termination)
  ▼
cloud_ai.ai_analyze  (require_role("ADMIN","ANALYST"), get_db)
  ▼
FinOpsAIService.analyze
  ├─ FinOpsContextBuilder.build
  │    ├─ period_resolver.resolve_period   → canonical period (+previous)
  │    ├─ period_resolver.resolve_rows     → provider accounts from scope selector
  │    ├─ ProviderRegistry.require(kind)   → AWS/Azure adapter (NO Rackspace slice today)
  │    ├─ adapter.get_cost_summary(...)    → canonical CloudCostSummary
  │    │    └─ cost_envelope.build_{aws,azure}_envelope → same CloudCostReport the UI renders
  │    ├─ adapter.get_resource_summary(...)→ CloudResourceSummary (aggregates only)
  │    └─ adapter.get_recommendations(...) → CloudRecommendation[] (savings-preserving)
  ├─ _render_context(context)              → bounded, prompt-safe text (tags: <finops_context>)
  ├─ _build_evidence(context)              → DETERMINISTIC evidence cards (server-side)
  ├─ [AI_ENABLED=false → return DISABLED answer + evidence, stop]
  ├─ LiteLLMClient.complete(system=SYSTEM_PROMPT, user=ctx+question, temperature=0.2)
  │    └─ http://litellm:4000/v1/chat/completions  (alias cost-analyst)
  └─ FinOpsAIAnswer{answer, status, provider, scope, period, data_freshness,
                    evidence[], warnings[], model, duration_ms}
```

Failure behaviour per step: period/scope/adapter failures → `UNAVAILABLE` context with explicit `unavailable_sources`; gateway errors → typed `LiteLLMError` subclass → `UNAVAILABLE` answer + deterministic evidence (HTTP 200); malformed/empty completion → `UNAVAILABLE`. No step can fabricate a financial value: evidence cards are computed from canonical envelopes **after** grounding and independently of the model.

### 3.2 Conversation realtime (`WS /api/ws/conversations/{id}`)

```text
Browser (AIAnalystPage.tsx → lib/ai/connection.ts)
  │  WS upgrade + token via `bearer.<jwt>` subprotocol (NEVER query string — nginx logs URLs)
  ▼
ws_auth.resolve_ws_principal  → 4401 invalid token / 4403 inactive·VIEWER·forged role /
  │                              1008 AUTH_ENABLED=false / 4404 not-owner-or-missing
  ▼
ws_conversations (per connection: asyncio.Lock + FIFO request_id dedup)
  ├─ parse_client_event (MAX_FRAME_BYTES cap; protocol violations → sanitized error event)
  ├─ ConversationService: persist USER message FIRST (crash-safe)
  ├─ AIService.generate_analysis_with_history (Phase 4 — AWS evidence, region+days only)
  │    └─ history rendered by render_history_block → <conversation_history> untrusted-data block
  ├─ ConversationService: persist ASSISTANT message on success
  └─ on AI failure: SYSTEM_EVENT persisted + error event AIUnavailable (no fake assistant text)
```

One AI request at a time per connection; duplicate `request_id` → `Busy`/silent-dedup; heartbeats client-driven.

### 3.3 Does the AI path have access to canonical data today?

| Input | Phase 4 (`/api/ai/*`, conversations) | Phase 7D.4 (`/api/cloud/ai/*`) |
|---|---|---|
| Canonical costs | No — legacy Phase 2 `CostReport` via Cost Explorer cache | **Yes** — same envelopes as the Costs page |
| Canonical resources | Partial (Phase 1 service list for utilization) | **Yes** — aggregated `CloudResourceSummary` |
| Canonical recommendations | Legacy Phase 3 engine output | **Yes** — canonical `CloudRecommendation[]` |
| Provider | AWS only | AWS + Azure (Rackspace excluded) |
| Account/subscription selector | No (region+days only) | **Yes** (`acct:` / `sub:` / `all` via `resolve_rows`) |
| Billing period | Lookback days (7/30/60/90) | **Yes** (month / days / explicit start-end) |
| Region filter | Yes (AWS region) | Yes (AWS region param) |

---

## 4. Problems / gaps (ranked)

1. **No reachable upstream model.** No `LITELLM_API_KEY`; `LITELLM_MODEL=cost-detective-free` matches neither the gateway alias (`cost-analyst`) nor any LiteLLM provider string; and the `ai` network is `internal: true`, so LiteLLM **cannot reach any public provider** even with a key. Enablement requires an explicit egress decision (§12) — blocked until Phase 9E.5.
2. **Config drift.** Backend alias (`cost-detective-free`) ≠ gateway `model_name` (`cost-analyst`). Any future `AI_ENABLED=true` would fail with "model not found" before reaching a provider.
3. **Rackspace not grounded.** Dashboard supports AWS/Azure/Rackspace (Phase 9D.1); AI supports AWS/Azure only. Selecting Rackspace on the dashboard and asking Cloud AI sends `provider=AWS` with a Rackspace scope (frontend coercion `provider === 'AZURE' ? 'AZURE' : 'AWS'`) — the backend resolves this honestly into an unavailable/mismatched context, but the UX is wrong and the grounding contract is incomplete.
4. **Dual AI stacks.** Conversations (WS) use the legacy AWS-only Phase 4 service with no provider/scope/period awareness; the canonical page uses 7D.4. Two grounding contracts, two prompts, two response shapes to maintain.
5. **Unstructured output.** Both prompts demand prose-only answers; there is no validated response schema, so savings/cost figures in prose are unverifiable at runtime (mitigated, not eliminated, by deterministic evidence cards displayed alongside).
6. **No post-response validation (7D.4).** Phase 4 validates citations against the evidence index; 7D.4 does not validate the prose beyond empty-completion handling.
7. **No AI audit trail.** Only in-process logs (operation, duration, token counts). No persisted request record (user, scope, model, grounding hash, latency, status). LiteLLM's own spend DB exists but is gateway-scoped and not joined to app identity.
8. **No per-user AI rate limits.** RBAC restricts to ADMIN/ANALYST and the WS serializes per connection, but REST `/api/cloud/ai/analyze` and `/api/ai/*` have no per-user quota.
9. **Minor:** Phase 4 `AIStatusResponse.provider_model` reads `LITELLM_MODEL` from the process env, which now reports the drifted placeholder as if it were a provider model.

---

## 5. Target architecture

The grounding principle (Phase 9E contract): **AI is a reasoning layer over deterministic FinOps data already produced by the platform. The LLM never talks to cloud providers, the browser never sends cloud credentials, and the model only ever sees normalized, server-approved context.**

```text
AWS / Azure / Rackspace APIs
        ↓
existing canonical backend services (ProviderRegistry → adapters → envelopes)
        ↓
normalized FinOps grounding envelope  (FinOpsContextBuilder v2 — server-side)
        ↓
AI Advisor service (prompt assembly + validation + audit)
        ↓
LiteLLM gateway (internal, alias-stable)
        ↓
configured LLM (via operator-supplied credential + approved egress path)
        ↓
validated advisor response (schema-checked, evidence-attributed)
        ↓
frontend (evidence cards + prose + status)
```

Non-negotiables carried forward from the existing implementation:

- The LLM does **not** query AWS/Azure/Rackspace (adapters do; the model receives text only).
- The browser does **not** send cloud credentials (it sends only selector strings validated server-side).
- The LLM receives **only** the normalized envelope — never raw SDK payloads, never credentials, never role ARNs / external IDs / tenant secrets.

---

## 6. Grounding envelope (canonical, provider-neutral)

**Decision: extend `FinOpsContextBuilder` / `FinOpsContext` (Phase 7D.4) rather than inventing a new model.** It already satisfies most of the brief's candidate shape, reuses canonical types (`CloudCostReport`, `CloudResourceSummary`, `CloudRecommendation`), preserves `None` (never fabricates zero), and is bit-identical to what the dashboard renders. Phase 9E.2 adds Rackspace support and explicit metadata.

```text
FinOpsGroundingEnvelope (v2 — target shape; ≈ current FinOpsContext + additions)

scope
  provider                 AWS | AZURE | RACKSPACE        (enum extension)
  provider_scope           acct:<id> | sub:<id> | rs:<account> | all   (existing selectors)
  account_display_name     from canonical by_account rows
  billing_period           kind (month|days|explicit), start, end, days   (FinOpsAIPeriod)
  region_filter            optional AWS region

costs                     → FinOpsCostSlice (exists)
  total_cost, currency, previous_period_cost, change_amount, change_percent
  previous_period_unavailable            (failed fetch ≠ genuine zero)
  top_services (≤10), top_regions (≤8), daily_trend (≤90; ≤30 in prompt)
  by_account, account_errors, successful_accounts, failed_accounts, status
  data_freshness, completeness           (LIVE|CACHED|STALE|PARTIAL|UNAVAILABLE + % succeeded)

resources                 → FinOpsResourceSlice (exists; aggregates ONLY — never raw rows)
  total, by_type, by_region, by_subscription, status

recommendations           → FinOpsRecommendationSlice (exists)
  count, estimated_savings (None-preserving), currency, savings_period
  top_items (≤10, ranked by savings desc): recommendation_id, title, resource_id,
  resource_type, service, estimated_savings, currency, source, region, category
  sources

metadata                  (NEW in 9E.2)
  generated_at            UTC ISO-8601
  data_sources            per-domain provenance (e.g. AWS_COST_EXPLORER, AZURE_ADVISOR,
                          RACKSPACE_BILLING_API) + per-source freshness
  unavailable_domains     ["cost"|"resources"|"recommendations", ...]
  grounding_hash          sha256 over the rendered envelope text (audit §15)
  warnings                context-level warnings (existing)
```

Rules reaffirmed in the envelope: `None` savings/costs stay `None` end-to-end; `savings_period` is explicit per provider (AWS `MONTHLY`, Azure `ANNUAL`, Rackspace `MONTHLY` — Rackspace figures are schema-parity only today, `savings_status=EMPTY` with `None` per Phase 9C); currencies are never silently converted or summed across providers.

---

## 7. Response contract (structured advisor response)

**Decision: hybrid.** Keep human-readable prose (it works well for FinOps narration and the existing UX) **plus** a required, schema-validated structured payload for every checkable claim. Structured output is enforced by the application, not trusted from the model.

```text
AdvisorResponse (pydantic, Phase 9E.2)

answer:        str            # prose narrative (grounded)
status:        SUCCESS | PARTIAL_SUCCESS | UNAVAILABLE | DISABLED
scope:         {provider, provider_scope, account_display_name, period}
summary:       str            # ≤ N chars, 1–3 sentence executive read
findings[]:    {title, detail, evidence_refs[], confidence}
recommendations[]:
  title, reason, evidence_refs[]
  recommendation_id          # MUST exist in envelope.top_items (validated)
  estimated_savings: float|None   # MUST equal the envelope value when cited
  savings_status:  AVAILABLE | UNAVAILABLE   # UNAVAILABLE ⇒ estimated_savings is null
  savings_period:  MONTHLY | ANNUAL
  action_type:     REVIEW_ONLY               # the ONLY allowed value in Phase 9E
  requires_human_review: true                 # constant in Phase 9E
risks[]:       {title, detail}
estimated_savings: {amount: float|None, currency, period, savings_status}
data_limitations[]: str          # mirrors envelope unavailable_domains / warnings
sources[]:     str               # canonical source identifiers actually used
confidence:    HIGH | MEDIUM | LOW
```

**Validation approach with the current stack (LiteLLM v1.104.0, OpenAI-compatible `/chat/completions`):**

- Do **not** rely on provider-native JSON mode / `response_format` (support varies by upstream provider behind LiteLLM and by model; `drop_params: true` currently masks unsupported params).
- Instead: instruct JSON in the prompt (one fenced JSON object), **parse + pydantic-validate server-side**, retry **once** with a repair instruction on parse failure, then degrade to the deterministic evidence-only answer (`status=PARTIAL_SUCCESS`, structured part empty). The prose answer is never the sole carrier of a financial claim — deterministic evidence cards are rendered from the envelope regardless.
- Hard validator rules (9E.2): every `recommendation_id` must exist in the envelope; any cited `estimated_savings` must equal the envelope value for that id; `savings_status=UNAVAILABLE` ⇒ savings null and the fixed sentinel phrasing; `action_type` ∈ {`REVIEW_ONLY`}; unknown/extra JSON fields dropped with a warning; validator failures recorded in the audit row.

The model must not be able to create executable infrastructure actions: `action_type` is a closed enum with a single safe value, validated server-side; anything else → rejected/repair.

---

## 8. Multi-cloud behaviour and scoping

AI scope **must follow the active dashboard selection** (provider + account + period from `useProviderContext` / `useAccountContext` / `PeriodSelector`). The request echoes the selection; the server re-resolves and re-validates it against the registry — the client's strings are never trusted as authorization.

| Dashboard selection | AI grounding scope |
|---|---|
| AWS aggregate | AWS adapter, all registered AWS accounts (`scope=all`) |
| AWS individual (`acct:<id>`, e.g. QA-Dev) | AWS adapter, only that account — no other account's rows, costs, or recs enter the envelope |
| Azure aggregate | Azure adapter, all federated subscriptions (`scope=all`) |
| Azure individual (`sub:<guid>`, e.g. Sponsorship) | Azure adapter, only that subscription |
| Rackspace aggregate / individual (`rs:708274`) | **Rackspace adapter (9E.2 addition)** — billing/resources/optimization envelopes for that RAN/tenant only |
| Cross-provider | **Not performed in normal mode.** No cross-provider totals, no currency mixing, no savings-period mixing (AWS monthly ≠ Azure annual ≠ Rackspace monthly-schema). A question about "all clouds" is answered per-provider with explicit refusal to sum. |

Isolation enforcement is server-side: `period_resolver.resolve_rows` resolves the selection to account rows **before** any adapter call, and only those rows are passed to the adapters — an account outside the selection cannot appear in the envelope even if the prompt asks for it. A future "compare providers" mode must be an **explicit** request type that fetches separate per-provider envelopes and presents them side-by-side without summing — it is out of scope for 9E.2 and must never be the implicit default.

---

## 9. Safety / correctness contract

The twelve rules from the brief map to enforced layers (not just prompt text):

| # | Rule | Enforcement layer(s) |
|---|---|---|
| 1 | Never fabricate cost values | Envelope-only grounding; system prompt; deterministic evidence cards; post-validator cross-checks structured claims |
| 2 | Never fabricate savings | Same as (1) + savings-protection sentinel + `savings_status` enum |
| 3 | Never convert unknown savings → $0 | `None`-preserving slices end-to-end; validator rejects `savings_status=UNAVAILABLE` with a number; prompt mandates exact sentinel sentence |
| 4 | Never recommend destructive actions | `action_type=REVIEW_ONLY` closed enum; prompt forbids imperative destructive commands (existing rule, kept) |
| 5 | Deterministic evidence outranks model opinion | Evidence cards rendered server-side from the envelope; displayed with the answer; validator ties rec claims to envelope ids |
| 6 | Unavailable ⇒ say unavailable | `unavailable_domains` / freshness in envelope; prompt mandates explicit unavailability statements; `previous_period_unavailable` distinguishes failure from zero |
| 7 | Never claim "safe to delete" | No such determination exists anywhere in the platform; prompt forbids it; validator allows only REVIEW_ONLY actions |
| 8 | Preserve provider/account scope | Server-side scope resolution before adapter calls; envelope carries scope; answer echoes it |
| 9 | Never mix providers/accounts | Per-provider envelopes; resolve_rows row-scoping; prompt-level no-mixing rules (existing, kept) |
| 10 | Never expose credentials/secrets/ARNs/tokens | Envelope excludes `CloudAccount.external_ref`/`metadata` by construction; rendered text contains no credential fields; redaction layer in `litellm_client` scrubs `sk-`/`Bearer`/`AKIA…` from any error text; secrets-not-logged tests exist |
| 11 | Advisory only, never execution | Application is read-only (verified guard tests exist for AWS read-only); response contract has no action-execution channel |
| 12 | Cloud actions out of scope in 9E | No such endpoints exist; 9E.2 adds none |

Layered enforcement order: **(1) envelope construction** (bad data never enters the prompt) → **(2) system prompt** (behavioural rules) → **(3) response schema + pydantic validation** (structure) → **(4) post-response validator** (claim-vs-envelope cross-check) → **(5) deterministic rendering** (evidence cards the UI can trust regardless of the model). Prompt-only enforcement is explicitly rejected.

---

## 10. AI_ENABLED behaviour (state machine)

| State | Condition | `/status` | Generation behaviour | Dashboard impact |
|---|---|---|---|---|
| **DISABLED** | `AI_ENABLED=false` (current) | `DISABLED` + reason | Controlled DISABLED envelope; canonical analyze still returns real deterministic evidence cards (no model call). No false "AI analysis" is ever presented. | None — fully operational |
| **READY (Live)** | enabled + gateway healthy + provider configured | `READY` | Full grounded path | AI surfaces live |
| **DEGRADED / UNAVAILABLE** | enabled + gateway unreachable (or upstream failing) | `DEGRADED` (legacy) / `UNAVAILABLE` | Sanitized UNAVAILABLE envelope + deterministic evidence; conversations persist the user turn + honest SYSTEM_EVENT | None — rest of app unaffected (compose `service_started`, not `service_healthy`) |
| **Upstream model error** | gateway up, provider 401/402/429/5xx | `READY` (gateway-level) | Typed sanitized error → UNAVAILABLE answer with category code | None |
| **Malformed model response** | unparseable / empty completion | `READY` | `LiteLLMMalformedResponse`/`EmptyCompletion` → UNAVAILABLE; (9E.2: one JSON repair retry, then deterministic fallback) | None |

Design rule: **every non-READY state must be visibly labelled in the UI** (Live / Disabled / Unavailable / Degraded) — the frontend contract (§17) makes status a first-class element, and no disabled/unavailable state may render model-looking text.

---

## 11. LiteLLM / model design

**Current configuration (inspected, nothing changed):**

- `litellm/config.yaml`: single alias `cost-analyst` → `model: os.environ/LITELLM_MODEL`, `api_key: os.environ/LITELLM_API_KEY`; `drop_params: true`, `telemetry: false`, `request_timeout: 30`; router `num_retries: 1`, `timeout: 30`; Postgres-backed metadata + spend tracking (`litellm` logical DB, isolated role, no app-DB grants).
- Deployment: `ghcr.io/berriai/litellm:v1.104.0` (pinned), internal-only network, no published ports, master key + salt key from protected host `.env`.
- **No real upstream model is configured.** `LITELLM_API_KEY` unset; `LITELLM_MODEL=cost-detective-free` is not a valid provider string and does not match the gateway alias. **State this plainly: there is no working model behind the gateway today.**
- **Egress constraint:** the `ai` network is `internal: true` (Phase 8 anti-exfiltration control — deliberately prevents LiteLLM from exfiltrating conversation context to arbitrary endpoints). Enabling any public LLM provider requires one of:
  1. **Egress proxy (recommended):** a minimal allow-list proxy on the AI plane forwarding only to the chosen provider FQDN(s); LiteLLM keeps `internal: true` and points at the proxy.
  2. Self-hosted / open-weights model reachable on the internal plane (no external egress at all).
  3. Removing `internal: true` (weakest — requires explicit security review; not recommended).
  This is a Phase 9E.5 enablement decision; 9E.2 builds everything provider-agnostic behind the alias.

**Recommended production model strategy (no changes now):**

- Primary: a cost-efficient general chat model via LiteLLM (the alias pattern already supports provider swap without app changes — e.g. an `openai/gpt-4o-mini`-class model). Rationale: advisory FinOps Q&A over a compact envelope does not require a frontier model; grounding + validation carry correctness.
- Fallback (optional): one secondary provider model via LiteLLM router fallbacks; if absent, the existing sanitized UNAVAILABLE degradation is acceptable.
- `temperature: 0.2` (existing; low-variance analytical task), `max_tokens` 800 (existing; 9E.2 may raise to ~1200 for the structured payload — measured, not assumed), `timeout` 30 s (existing), `num_retries: 1` (existing).
- Fix the alias drift at enablement: set backend `LITELLM_MODEL=cost-analyst` (restore the stable internal alias) and keep provider/model resolution entirely in the gateway env.

---

## 12. Token / cost controls

Already implemented and kept:

- Envelope bounding: ≤10 services, ≤8 regions, ≤10 recommendations, ≤30 trend points in-prompt (≤90 stored), aggregates only — **raw resource lists are never sent** (server-side summarization is inherent to the slice design).
- Request bounding: question ≤2 000 chars; history ≤10 messages / 12 000 chars; single system + single user message; completion ≤800 tokens; timeout 30 s; 1 gateway retry; WS: 1 in-flight AI request per connection.
- Frontend: 30 s answer cache keyed by provider/scope/period/question-hash (no duplicate spend for identical asks).

Added in 9E.2 (design targets, tuned during implementation):

- Envelope byte budget: rendered context target ≤ ~12 KB (hard cap 16 KB) before invocation; overflow drops lowest-ranked items first (services/regions beyond top-5, recs beyond top-10) and records the truncation in `data_limitations`.
- Per-user rate limit on AI generation endpoints (e.g. 10 requests / rolling minute, 429 with sanitized envelope; config-driven) — applies to REST; WS already serialized per connection and its messages pass through the same service (rate limit applied at service layer so both paths share it).
- Daily per-user token ceiling surfaced from LiteLLM usage (`prompt_tokens`/`completion_tokens` already parsed by `LiteLLMClient`); exceeded → UNAVAILABLE with a distinct error category.
- Timeout/retry unchanged (30 s / 1) — retrying structured-output repairs counts as the single application-level retry.

---

## 13. Authentication / authorization

Current (verified) and retained:

- HTTP generation endpoints: `Depends(require_role("ADMIN", "ANALYST"))` — VIEWER and anonymous denied; same JWT/bearer boundary as every other business route (`auth_enabled` semantics unchanged).
- WebSocket: `bearer.<jwt>` subprotocol (token never in query string — nginx logs URLs), same `SecurityCore.decode_token`; closes 4401 (bad token), 4403 (inactive / VIEWER / forged role), 4404 (not owner — indistinguishable from nonexistent), 1008 (auth disabled); per-message ownership verified before upgrade.
- Session expiry: standard JWT (60-min default access token) — an expired token fails exactly like any other API call.
- Status endpoints (`/api/ai/status`, `/api/cloud/ai/status`) are public by design: they expose only operational booleans/alias names, never keys, headers, or credential-bearing URLs.

9E.2 additions: none required for auth; per-user rate limiting (§12) keys off the authenticated principal. **No unauthenticated LLM endpoint exists or will be added.**

---

## 14. Audit design

New `ai_request_audit` table (9E.2, additive migration) — one row per AI generation invocation, **no secrets ever**:

```text
id, request_id (uuid), user_id, created_at (utc)
provider, provider_scope, billing_period (kind+start+end), region_filter
model_alias, provider_model (as reported), operation (analyze|summary|explain|conversation)
grounding_hash (sha256 of rendered envelope), grounding_bytes, freshness, unavailable_domains
response_status (SUCCESS|PARTIAL_SUCCESS|...), validator_warnings (jsonb)
latency_ms, prompt_tokens, completion_tokens, total_tokens
estimated_cost (from gateway spend data when available; null otherwise)
error_category (litellm_timeout | litellm_unavailable | validator_failed | ...)
```

Prompt/response storage policy (recommended for this project):

- **One-shot endpoints:** metadata only — prompts and responses are **not stored**. The grounding hash + evidence cards give reproducibility without retaining user text.
- **Conversations:** messages persist as today (existing user-visible feature, explicitly not "AI logs"); audit rows carry metadata only and link by conversation/message id.
- Never stored: bearer tokens, cloud credentials, `external_ref`, tenant metadata secrets. (Complies with the established secrets-not-logged posture and its tests.)

LiteLLM's own spend DB remains the billing source of truth; the audit table joins app identity to it via request correlation, not by duplicating keys.

---

## 15. Prompt-injection / data safety

Threat surface: resource names/tags/descriptions, recommendation titles/descriptions, account display names, prior conversation turns, and the user question — all attacker-influencable text that reaches the model.

Existing defences (kept, tested):

- Delimited, labelled untrusted blocks: `<finops_context>` / `<aws_evidence>`, `<user_question>`, `<conversation_history>`; system prompt declares everything inside them DATA, never instructions; explicit anti-"ignore previous instructions" clauses; history never promoted to the system channel (Phase 5B contract).
- Structural containment: the model cannot act — there are no tool calls, no function calling, no exec path; the app is read-only with guard tests.
- Deterministic evidence cards are computed server-side and are immune to injection.

9E.2 hardening:

- **Delimiter escaping at render time:** any envelope string (resource names, titles, descriptions) containing `<finops_context>`, `</finops_context>`, `<user_question>`, `<aws_evidence>`, `<conversation_history>` (open or close variants) is escaped/neutralized before rendering so user-controlled metadata cannot close the evidence block early and speak as the system.
- Control-character and zero-width-character scrubbing on envelope strings.
- Post-response validator (§7): fabricated `recommendation_id`s, savings not equal to envelope values, or non-REVIEW_ONLY action types cause repair/rejection — injected instructions that alter claims are caught structurally even when the prompt defence fails.
- Keep output plain text/JSON with no markdown-execution features on the frontend (existing renderer already treats model output as text).

---

## 16. Failure handling (summary matrix)

| Failure | Detection | User-visible result | Data integrity |
|---|---|---|---|
| LiteLLM down | connect error / health probe | `UNAVAILABLE` envelope + deterministic evidence cards | User WS message persisted; SYSTEM_EVENT recorded |
| Timeout (>30 s) | `httpx.TimeoutException` | `UNAVAILABLE` (`litellm_timeout`) | same |
| Provider auth/quota/rate-limit | status mapping 401/402/429 | `UNAVAILABLE` with sanitized category | same |
| Malformed / empty completion | parser guards | `UNAVAILABLE` (`malformed` / `empty`) — 9E.2: one JSON repair retry first | same |
| Validator rejection (9E.2) | pydantic + cross-check | `PARTIAL_SUCCESS` deterministic answer (structured part dropped), warning recorded | audit row `validator_failed` |
| Scope/period invalid | `resolve_period` / `resolve_rows` | `UNAVAILABLE` context, honest "not available" answer | no adapter calls made |
| Partial source failure | per-source try/except | answer continues; `data_limitations` + freshness `PARTIAL` name the gap | `unavailable_domains` recorded |

In every row: no fabricated financials, no 5xx leakage of provider internals, no crash of non-AI surfaces.

---

## 17. Frontend contract (design only — implementation in 9E.3)

The professional AI Advisor UI extends the existing system (SectionCard / MetricCard / StatusBadge / ProviderSelector / AccountSelector / PeriodSelector / AiProviderBadge / EvidenceCard / MessageBubble — no new visual language):

- **Scope bar:** Provider + Account/subscription + Period — the same selectors as the dashboard, shared context state; selection changes reset any displayed answer (already implemented on CloudAIPage; keep).
- **Advisor summary:** executive prose block with grounding freshness badge.
- **Key findings:** structured `findings[]` cards (9E.3 renders the 9E.2 schema).
- **Optimization opportunities:** recommendations with id, source badge, savings (or explicit "Authoritative savings are not available"), `savings_period`, REVIEW_ONLY action chip, `requires_human_review` flag.
- **Risks / limitations:** `risks[]` + `data_limitations[]` (never hidden).
- **Evidence / data sources:** the deterministic evidence cards (existing component) + `sources[]`.
- **Conversation area:** existing WS chat (AIAnalystPage) remains for threaded follow-ups; 9E.3 aligns it to the canonical grounding (or explicitly labels it AWS-scoped until migrated).
- **AI status:** first-class badge with exactly four states — Live / Disabled / Unavailable / Degraded — from `/status`; disabled/unavailable states render the deterministic evidence, never fake "AI analysis".

---

## 18. Phase 9E.2 implementation scope (precise)

**Goal:** backend grounding + LiteLLM advisor service, provider-complete and validated. No frontend redesign, no enablement, no AI_ENABLED change.

1. **Rackspace grounding:** extend `FinOpsAIProvider` with `RACKSPACE`; add Rackspace slices to `FinOpsContextBuilder` (cost/resources/recommendations via the existing `services/cloud/rackspace/*` paths and `cloud_envelope` helpers); enforce `savings_period=MONTHLY` schema-parity semantics with `None`/EMPTY savings preserved. File anchors: `backend/app/schemas/cloud_ai.py`, `backend/app/services/cloud/finops_context.py`, `backend/app/services/cloud/finops_ai_service.py`.
2. **Envelope v2 metadata:** `generated_at`, `data_sources`, `unavailable_domains`, `grounding_hash`, completeness; delimiter/control-char escaping in the renderer (`finops_ai_service._render_context` or a new `grounding_renderer.py`).
3. **Advisor response contract:** new `AdvisorResponse` pydantic module (`backend/app/schemas/` or `backend/app/services/cloud/`), prompt update to emit the JSON payload, server-side parse → validate → single repair retry → deterministic fallback; post-response cross-check of recommendation ids/savings vs envelope; `action_type` closed enum.
4. **Audit:** `ai_request_audit` table + SQLAlchemy model + writer service (migration additive only); metadata-only policy per §14.
5. **Rate limits / budget:** per-user AI rate limit + daily token ceiling at the service layer (config-driven, sane defaults), shared by REST and WS paths.
6. **Config alignment (documentation + code defaults only):** restore alias `cost-analyst` as the backend default expectation; treat `LITELLM_MODEL`/`LITELLM_API_KEY` strictly as gateway-side operator inputs; no credential changes, no compose changes, no LiteLLM start.
7. **Tests:** unit tests for rackspace slices + envelope metadata + escaping; validator tests (fabricated id / savings mismatch / UNAVAILABLE-with-number / bad action_type all rejected); audit writer test; rate-limit test; mocked-LLM end-to-end for all six scopes (AWS all/individual, Azure all/individual, Rackspace all/individual) asserting scope isolation (no cross-account fields in prompt or evidence); AI_ENABLED=false regression suite stays green.

**Explicit non-goals for 9E.2:** frontend rework (9E.3), cross-provider validation harness (9E.4), egress decision + `AI_ENABLED=true` (9E.5), any cloud-action execution, conversations-stack migration decision (documented, decided before 9E.3).

Post-9E breakdown (confirmed as sensible): **9E.3** professional frontend (scope bar + structured rendering + status states + Rackspace support on CloudAIPage, fixing the AWS-coercion gap), **9E.4** multi-cloud grounding/safety validation (cross-page consistency: AI evidence vs Costs/Resources/Optimization pages per provider; injection red-team cases; validator adversarial suite), **9E.5** production enablement (egress design, alias/credential wiring, AI_ENABLED flip, final acceptance) — matching the preferred breakdown; no repository evidence supports a different division.

---

## 19. Risks

1. **Egress decision delay** — grounding/validation work (9E.2) is provider-agnostic, but nothing live ships until 9E.5 picks an egress path (proxy vs self-hosted vs network change). Mitigation: keep the alias boundary; decide early in 9E.5.
2. **Structured-output fragility** — smaller models may not reliably emit the JSON payload; the repair-retry + deterministic fallback bounds the blast radius (worst case: today's behaviour with evidence cards only).
3. **Token-budget growth** — the structured payload increases completion size; envelope input stays bounded by design; measure and tune `max_tokens` rather than assuming.
4. **Dual-stack divergence** — conversations (Phase 4, AWS-only) vs canonical (7D.4) can drift; 9E.3 must either unify or explicitly label scopes; until then the WS path is understood to be AWS-scope only.
5. **Rackspace savings semantics** — Rackspace recommendations currently carry no savings figures (schema parity, EMPTY status); the AI must present "authoritative savings are not available" for Rackspace rather than implying any estimate. Validator rule (3) covers this.
6. **LiteLLM version pin** — `v1.104.0` is older; JSON-mode/tool features vary by provider; design deliberately avoids depending on them.
7. **Audit table growth** — metadata-only rows are small but unbounded; add retention guidance in 9E.2 (e.g. prune > 90 days) without storing payloads.

---

## 20. Acceptance criteria

### Phase 9E.1 (this phase)
- Architecture document complete at `docs/phase9e1-ai-architecture-grounding.md`.
- **Zero runtime changes**: no backend/frontend/docker/env/LiteLLM/migration/test/CI modifications; `git diff` touches the doc only.
- LiteLLM not started; `AI_ENABLED` unchanged (`false`); no containers rebuilt.

### Phase 9E.2 (definition-of-done reference)
- All six scope combinations ground through canonical adapters; each answer/evidence references only the selected provider+account data (isolation tests prove it).
- Every AI generation writes an audit row with grounding hash, status, latency, tokens — and never a secret.
- Validator demonstrably rejects fabricated recommendation ids, mismatched savings, unknown→$0 conversions, and non-REVIEW_ONLY actions (test-proven).
- `AI_ENABLED=false` behaviour unchanged and fully covered by the existing green suite; deterministic (non-model) answers remain honest and labelled.
- No new provider SDK imports; all model traffic still flows through `LiteLLMClient` → gateway alias.
