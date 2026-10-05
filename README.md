# AI Cloud Cost Detective — AWS Edition

Phase 2 cost & utilization intelligence on top of a secure, reproducible,
low-cost Docker Compose stack: Nginx (public :80) fronting a FastAPI backend
and a React+Vite+TS frontend, with PostgreSQL (Alembic-migrated) and LiteLLM
Gateway reachable only on the internal Docker network.

> **Phase 2 status**: cost & utilization intelligence is shipped.
> Cost Explorer aggregation, CloudWatch utilization, an evidence layer that
> joins Phase 1 resource metadata with cost + utilization, and a read-through
> cache with TTL + probabilistic GC are all live. The implementation is
> **strictly read-only**: no Boto3 call site mutates AWS state, and every
> call site is wrapped by an application-layer guard.

## Architecture

```
Browser → Nginx (public :80)
            ├── /         → React (internal :5173)
            └── /api/     → FastAPI backend (internal :8000)
                              ├── PostgreSQL (internal :5432, two logical DBs)
                              │     └── cost_cache (Alembic-managed)
                              └── LiteLLM Gateway (internal :4000)

Phase 2 reads from AWS (no writes):
   /api/aws/identity     → STS GetCallerIdentity
   /api/aws/resources    → EC2/EBS/EIP/NAT/ELBv2/RDS/Lambda/S3 + Resource Explorer + Tagging
   /api/aws/costs        → Cost Explorer GetCostAndUsage (UnblendedCost)
   /api/aws/utilization  → CloudWatch GetMetricData (batched)
   /api/aws/evidence     → Phase 1 + Cost Explorer + CloudWatch, composed
```

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

# 3. Apply the Alembic migrations (creates cost_cache)
make migrate

# 4. Run the Phase 3 verifier end-to-end (Phase 1 -> Phase 2 -> Phase 3)
make verify-phase3

# 5. Open http://localhost (or your server's public IP, port 80)
```

## Health endpoints

| URL                          | Returns                                                  |
| ---------------------------- | -------------------------------------------------------- |
| `GET /api/health`            | `{ "status": "ok", "service": "ai-cloud-cost-detective-backend" }` |
| `GET /api/health/ready`      | Component readiness for backend, database, litellm       |

## Phase 2 endpoints

All endpoints are read-only and live under `/api/aws/`.  They degrade
gracefully — per-source failures surface as structured `Warning` rows,
never as a 5xx that destroys valid data from other sources.

| Method | Path                  | Body / Query                              | Purpose                                                  |
| ------ | --------------------- | ----------------------------------------- | -------------------------------------------------------- |
| GET    | `/api/aws/identity`   | `?region=us-east-1` (optional)            | STS caller identity (Account / ARN / UserId)             |
| GET    | `/api/aws/resources`  | `?region=us-east-1` (optional)            | Phase 1 resource inventory + Resource Explorer enrichment |
| GET    | `/api/aws/costs`      | `?days=7\|30\|60\|90`                     | Cost Explorer aggregate (total / daily / by-service / by-region) with read-through cache |
| POST   | `/api/aws/utilization`| `{region, lookback_days, resource_types?}` | CloudWatch utilization per resource with data_quality    |
| POST   | `/api/aws/evidence`   | `{region, days, resource_types?}`         | Phase 1 + Cost Explorer + CloudWatch, composed deterministically |
| GET    | `/api/aws/optimization/capabilities`  | `?region=us-east-1` (optional)            | Compute Optimizer + Cost Optimization Hub enrollment state + supported resource types / lookbacks |
| GET    | `/api/aws/optimization/recommendations` | `?region=&days=7\|30\|60\|90`           | Deduplicated recommendations from CO + COH + deterministic rules |
| GET    | `/api/aws/optimization/summary`       | `?region=&days=7\|30\|60\|90`           | Aggregated counts + savings by resource type / action / source / confidence |

### `/api/aws/costs` example

```bash
curl -s 'http://localhost/api/aws/costs?days=30' | jq
```

Returns:

```json
{
  "report": {
    "account_id": "974053642038",
    "period": {"start": "2026-09-05", "end": "2026-10-05", "days": 30},
    "previous_period": {...},
    "total_cost": "123.45",
    "previous_period_cost": "98.76",
    "change_amount": "24.69",
    "change_percent": "0.25",
    "by_service": [{"service": "Amazon EC2", "amount": "78.90", "unit": "USD"}],
    "by_region":  [{"region": "us-east-1", "amount": "100.00", "unit": "USD"}],
    "daily_trend": [{"date": "2026-09-05", "amount": "4.12", "unit": "USD"}],
    "source": "AWS_COST_EXPLORER"
  },
  "cache_status": "HIT",
  "cached_at": "2026-10-05T11:04:23+00:00",
  "expires_at": "2026-10-05T17:04:23+00:00"
}
```

## Useful commands

```bash
make ps              # show running services
make logs            # tail logs
make test            # run backend pytest suite
make migrate         # apply Alembic migrations (Phase 2 cost_cache)
make verify          # Phase 0 verifier (platform foundation)
make verify-phase1   # Phase 1 verifier (AWS identity + resource discovery)
make verify-phase2   # Phase 2 verifier (cost + utilization + evidence + cache)
make verify-phase3   # Phase 3 verifier (optimization intelligence)
make down            # stop the stack (keeps volumes)
make clean           # stop AND remove volumes (destructive; 5s grace)
```

## How secrets are generated

`scripts/generate_dev_secrets.sh` uses `openssl rand` to produce cryptographically
strong values for `POSTGRES_ADMIN_PASSWORD`, `COST_DETECTIVE_DB_PASSWORD`,
`LITELLM_DB_PASSWORD`, `LITELLM_MASTER_KEY` (always prefixed with `sk-`),
`LITELLM_SALT_KEY`, and `APP_SECRET_KEY`. Secrets are written to `.env` (mode 600)
and **never** echoed or committed.

`.env.example` contains variable names and safe placeholders only.

## Phase 2 invariants (enforced in code)

1. **No per-resource dollar figures.** `CostScope` is an enum whose only
   members are `ACCOUNT | SERVICE | REGION`. There is no `RESOURCE` member
   and the cost evidence builder is unit-tested to never produce one.
2. **Read-only AWS, enforced at every call site.** Every Boto3 call site in
   `app/services/aws/cost_explorer.py` and `app/services/aws/cloudwatch_metrics.py`
   is preceded by `assert_read_only(client, op_name)` from
   `app/services/aws/guard.py`. A grep-based check in
   `scripts/phase2_verify.sh` flags any future drift.
3. **Errors are never persisted.** The cost cache layer's `get_or_refresh`
   propagates loader exceptions to the caller and writes nothing to
   Postgres on failure. A unit test exercises this path explicitly.
4. **Cache deduplication.** Concurrent identical requests share a single
   loader invocation via an `asyncio.Lock` keyed by `(account_id, cache_key)`.
5. **Dialect-aware JSONB.** `CostCache.payload` is `JSONB` on Postgres
   (production) and `JSON` on SQLite (tests); the dialect-aware
   `TypeDecorator` lets the same ORM run against both backends.
6. **Pytest cache directory is writable by the non-root `app` user.**
   The Dockerfile pre-creates `/app/.pytest_cache` and chowns it to `app`
   so pytest never has to chmod `/app` globally writable.

## Phase boundary

Phase 2 does **not** include: Compute Optimizer, Cost Optimization Hub,
LiteLLM/AI analysis of cost data, JWT auth, signup/login, report-history
persistence, the production dashboard, WebSockets, Terraform/IaC, automated
AWS remediation, or multi-region fanout. These are documented under
"Deferred to later phase" in `docs/phase2-cost-intelligence.md`.

## Next planned phase

Phase 3 will add the Compute Optimizer + Cost Optimization Hub surfaces
on top of the Phase 2 evidence layer.  Phase 3 is **not** part of this
slice.
