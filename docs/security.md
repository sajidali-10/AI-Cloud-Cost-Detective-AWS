# Security — Phase 0

This document records the security decisions implemented in Phase 0 and the
threats that are explicitly **out of scope** for this phase.

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
