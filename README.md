# AI Cloud Cost Detective — AWS Edition

Phase 0 platform foundation. Secure, reproducible, low-cost Docker Compose stack
on a single Ubuntu host: Nginx (public :80) fronting a FastAPI backend and a
React+Vite+TS frontend, with PostgreSQL and LiteLLM Gateway reachable only on the
internal Docker network.

> **Phase 0 status**: foundation only.
> Phase 0 does **not** analyze AWS resources yet.
> Phase 0 does **not** invoke any LLM.
> Phase 0 does **not** incur LLM API charges.

## Architecture

```
Browser → Nginx (public :80)
            ├── /         → React (internal :5173)
            └── /api/     → FastAPI backend (internal :8000)
                              ├── PostgreSQL (internal :5432, two logical DBs)
                              └── LiteLLM Gateway (internal :4000)
```

Planned future AWS integration (NOT IMPLEMENTED IN PHASE 0): STS, Resource Explorer,
Cost Explorer, CloudWatch, Compute Optimizer, Cost Optimization Hub.

## Prerequisites

- Ubuntu (tested on 24.04)
- Docker Engine
- Docker Compose v2
- `openssl` (for secrets generation)

## Quick start

```bash
# 1. Generate a local .env with strong random secrets (NEVER commit)
make secrets

# 2. Build and start the stack
make build
make up

# 3. Verify the platform end-to-end
make verify

# 4. Open http://localhost (or your server's public IP, port 80)
```

## Health endpoints

| URL                          | Returns                                                  |
| ---------------------------- | -------------------------------------------------------- |
| `GET /`                      | Frontend placeholder page (React build)                  |
| `GET /api/health`            | `{ "status": "ok", "service": "ai-cloud-cost-detective-backend" }` |
| `GET /api/health/ready`      | Component readiness for backend, database, litellm       |

## Useful commands

```bash
make ps        # show running services
make logs      # tail logs
make test      # run backend pytest suite
make down      # stop the stack (keeps volumes)
make clean     # stop AND remove volumes (destructive; 5s grace)
```

## How secrets are generated

`scripts/generate_dev_secrets.sh` uses `openssl rand` to produce cryptographically
strong values for `POSTGRES_ADMIN_PASSWORD`, `COST_DETECTIVE_DB_PASSWORD`,
`LITELLM_DB_PASSWORD`, `LITELLM_MASTER_KEY` (always prefixed with `sk-`),
`LITELLM_SALT_KEY`, and `APP_SECRET_KEY`. Secrets are written to `.env` (mode 600)
and **never** echoed or committed.

`.env.example` contains variable names and safe placeholders only.

## Phase boundary

Phase 0 does **not** include: AWS resource scanning, Cost Explorer, CloudWatch,
Compute Optimizer, Cost Optimization Hub, Resource Explorer, STS, JWT auth,
AI/LLM analysis, the production dashboard, or Terraform. These are documented
under "Deferred to later phase" in `docs/phase0-report.md`.

## Next planned phase

Phase 1 slice (1) — **AWS Identity and Resource Discovery** — is shipped:

- `GET /api/aws/identity` returns STS caller identity (Account / ARN / UserId) via the
  Boto3 default credential chain (no AWS keys in env).
- `GET /api/aws/resources` enumerates EC2, EBS, Elastic IPs, NAT Gateways, ELBv2,
  RDS, Lambda, and S3 bucket metadata, with optional Resource Explorer + Resource
  Groups Tagging API enrichment. Per-service Boto3 errors stay in the response
  body instead of failing the call.
- Region is selectable per request via `?region=` with fallback to `AWS_DEFAULT_REGION`.
- Fully mock-tested; live validation is a documented runbook (`docs/phase1-aws-discovery.md`).

Still deferred: JWT auth / signup / login, report history persistence, Cost Explorer,
CloudWatch, Compute Optimizer, Cost Optimization Hub, AI/LLM analysis, frontend
dashboard, WebSockets, Terraform/IaC, automated AWS remediation, multi-region fanout.
