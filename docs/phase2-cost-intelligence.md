# Phase 2 — Cost & Utilization Intelligence

> **Scope.** This document describes the AWS Cost & Utilization Intelligence
> surface delivered in Phase 2: an aggregate Cost Explorer view, a
> CloudWatch utilization per-resource view, an evidence layer that joins
> Phase 1 resource metadata with cost + utilization, and a read-through
> Postgres-backed cache with TTL and probabilistic GC.
>
> **Out of scope.** Compute Optimizer, Cost Optimization Hub, LiteLLM/AI
> analysis of cost data, JWT auth, signup/login, report-history persistence,
> the production dashboard, WebSockets, Terraform/IaC, automated AWS
> remediation, multi-region fanout. These are deferred to later phases.

## 1. Goals

The Phase 2 slice ships five capabilities on top of the Phase 0 platform
foundation and the Phase 1 read-only AWS discovery surface:

1. **AWS Cost Explorer aggregate.** Account-level spend for the chosen
   lookback window (`7 / 30 / 60 / 90` days), with a daily trend, by-service
   breakdown, by-region breakdown, and prior-period comparison.
2. **CloudWatch utilization per resource.** Per-resource metric series
   (CPU / network / DB connections / Lambda invocations / ALB / NLB) for
   the chosen lookback window, with a per-resource `data_quality` bucket
   derived from datapoint coverage.
3. **Evidence layer.** A deterministic, per-resource record that joins
   Phase 1 inventory with Cost Explorer (SERVICE-scope) and CloudWatch
   (per-resource utilization). The wire contract forbids per-resource
   dollar figures.
4. **Read-through cache.** Postgres-backed TTL cache in front of Cost
   Explorer, with in-process deduplication and lazy probabilistic GC.
5. **Operational guardrails.** A read-only guard on every Boto3 call
   site, a sanitized error surface, an Alembic migration story, and
   end-to-end verification (`scripts/phase2_verify.sh`).

## 2. Architecture

```
                       ┌──────────────────────────────────┐
                       │            AWS (read-only)       │
                       │  Cost Explorer  |  CloudWatch    │
                       │  STS            |  (no writes)   │
                       └────────┬────────────────┬────────┘
                                │                │
                  ┌─────────────┴────┐  ┌────────┴───────────┐
                  │ app/services/aws/ │  │ app/services/aws/  │
                  │ cost_explorer.py  │  │ cloudwatch_metrics │
                  └─────────┬─────────┘  └─────────┬──────────┘
                            │                      │
                            ▼                      ▼
                  ┌─────────────────────────────────────────┐
                  │       app/services/cost_cache.py        │
                  │  (asyncio dedup + TTL + lazy GC)        │
                  └─────────────────┬───────────────────────┘
                                    ▼
                       ┌──────────────────────────┐
                       │  PostgreSQL cost_cache   │
                       │  (Alembic 0001_cost_cache)│
                       └──────────────────────────┘
                                    │
                                    ▼
                  ┌─────────────────────────────────────┐
                  │  app/services/cost_evidence_builder │
                  │   joins Phase1 + CE + CW into an     │
                  │   EvidenceResponse                  │
                  └─────────────────┬───────────────────┘
                                    ▼
                       ┌─────────────────────────┐
                       │  FastAPI /api/aws/...   │
                       │  aws_costs / utilization│
                       │  /evidence (read-only)  │
                       └─────────────────────────┘
```

The application layer is the only place where the read-only invariant
is enforced. Every Boto3 call site in `app/services/aws/` is preceded
by `assert_read_only(client, op_name)` from `app/services/aws/guard.py`,
which raises `AwsReadOnlyViolation` *before* the call is dispatched if
the operation name matches a forbidden prefix
(`create_`, `delete_`, `modify_`, `update_`, `terminate`, `stop_`,
`start_`, `attach_`, `detach_`, … — see the file for the full list).

## 3. Module map

| Module | Role |
| ------ | ---- |
| `app/services/aws/guard.py` | `assert_read_only` / `is_read_only_operation` / `FORBIDDEN_PREFIXES` |
| `app/services/aws/cost_explorer.py` | `aggregate_cost_report`, period helpers, `CostExplorerError` |
| `app/services/aws/cloudwatch_metrics.py` | `build_metric_queries`, `batch_query`, `aggregate_results`, `classify_data_quality` |
| `app/services/aws/clients.py` | `get_aws_client(service_name, region)` — Boto3 default credential chain |
| `app/services/aws/resources.py` | Phase 1 multi-service enumeration (EC2, EBS, EIP, NAT, ELBv2, RDS, Lambda, S3) |
| `app/services/cost_cache.py` | `get_or_refresh`, `maybe_collect_garbage`, dedup, TTL |
| `app/services/cost_evidence_builder.py` | `build_evidence(...)` — joins Phase1 + CE + CW |
| `app/schemas/cost.py` | `CostPeriod`, `CostReport`, `CostReportResponse`, `CacheStatus`, … |
| `app/schemas/utilization.py` | `UtilizationRequest/Response`, `ResourceUtilization`, `MetricSeries`, `Warning` |
| `app/schemas/evidence.py` | `CostContext`, `CostScope`, `EvidenceResponse`, `ResourceEvidence` |
| `app/api/aws_costs.py` | `GET /api/aws/costs` |
| `app/api/aws_utilization.py` | `POST /api/aws/utilization` |
| `app/api/aws_evidence.py` | `POST /api/aws/evidence` |
| `app/db/models.py` | `CostCache` ORM + dialect-aware `JSONB` decorator |
| `app/db/session.py` | `engine`, `SessionLocal`, `get_db` (process-wide engine, no `create_all`) |
| `alembic/env.py` + `alembic/versions/0001_cost_cache.py` | First migration; idempotent DDL |

## 4. Cost Explorer wrapper

`aggregate_cost_report(client, days)` composes four retrievers against the
Boto3 `ce.GetCostAndUsage` API:

* `get_total_and_previous(period)` — returns `(current_total, previous_total)`.
* `get_daily_trend(period)` — `[(date, amount, unit), …]` at DAILY granularity.
* `get_by_service(period)` — `[(service_name, amount, unit), …]` grouped by SERVICE.
* `get_by_region(period)` — `[(region, amount, unit), …]` grouped by REGION.

Invariants:

* **Lookback allow-list.** `days` is checked against `(7, 30, 60, 90)` BEFORE
  the AWS call so we never bill for an obviously-bad request.
* **Single metric.** Only `UnblendedCost` is requested. Mixing metrics would
  silently change the meaning of every dollar figure.
* **Pagination.** `NextPageToken` is walked until the response is exhausted.
* **Period semantics.** `start` is inclusive, `end` is exclusive, in UTC.
  Today is excluded (not yet a full day).
* **Error sanitization.** All Boto3 errors are funneled into a `CostExplorerError`
  carrying only the high-level code (`AccessDenied`, `ValidationException`,
  `Throttling`, …). The message is generic so credentials, request payloads,
  and stack traces never leak through FastAPI's default error handler.

The wrapper returns a frozen dataclass; the route layer converts it to a
Pydantic `CostReport` (defined in `app/schemas/cost.py`) for the wire format.

## 5. CloudWatch wrapper

`app/services/aws/cloudwatch_metrics.py` exposes:

* **Per-resource metric specs.** A `METRIC_SPECS_BY_TYPE` dict pins the
  metric set for each Phase 1 resource type (`ec2`, `rds`, `lambda`,
  `alb`, `nlb`). The route layer NEVER accepts an arbitrary AWS operation
  name from the caller — the backend owns the allowlist.
* **Aggregation periods.** `PERIOD_BY_LOOKBACK_DAYS = {7:3600, 30:21600,
  60:43200, 90:86400}` — pinned from the Phase 2 spec.
* **Batched retrieval.** `batch_query(client, queries, …)` chunks the
  flat query list into batches of 500 (the AWS hard limit) and walks
  `NextPageToken` on every call.
* **Result aggregation.** `aggregate_results(...)` maps AWS `MetricDataResult`
  rows back to per-resource `MetricSeries` rows, classifying data quality
  per the spec.
* **Data-quality classifier.** `classify_data_quality(...)` returns
  `high | medium | low | no_data` based on `datapoint_count / expected_count`.
  Missing datapoints are reported as `no_data` — **never** as a metric
  value of 0 — to avoid fabricating low-utilization signals.
* **Error sanitization.** `CloudWatchError(code, message)` is the only
  error type raised to the route layer.

## 6. The evidence layer

`app/services/cost_evidence_builder.build_evidence(...)` is the single
function that joins the three data sources:

| Source            | Input                                          | Output                                  |
| ----------------- | ---------------------------------------------- | --------------------------------------- |
| Phase 1           | `phase1_services` (`{name: ServiceResult}`)    | Resource enumeration (id, type, region) |
| Cost Explorer     | `cost_report` (`CostReport` or None)           | SERVICE-scope spend per resource        |
| CloudWatch        | `utilization_series` (`List[MetricSeries]`)    | Per-resource utilization + data_quality |

Invariants:

* **`CostScope` has no `RESOURCE` member.** The enum in
  `app/schemas/evidence.py` lists only `ACCOUNT | SERVICE | REGION`.
  A unit test (`test_resource_evidence_never_has_resource_scoped_cost`)
  asserts this explicitly.
* **Per-source failures do NOT destroy valid data.** `cost_warning`,
  `utilization_warning`, and `resources_warning` are passed in by the
  route and serialized as `Warning` rows in the response.
* **`status` is computed from the warning count:**
  * `SUCCESS` — no warnings.
  * `PARTIAL_SUCCESS` — at least one source returned data and at least
    one source failed.
  * `FAILED` — every source failed.
* **`sources` lists every AWS API that contributed** to each
  `ResourceEvidence` row, so Phase 3 can attribute every number.

## 7. The cost cache

`app/services/cost_cache.py` provides:

* `get_or_refresh(account_id, cache_key, loader, ttl_seconds=None)` —
  read-through entry point.
* `maybe_collect_garbage(db, probability=None, batch=None)` — bounded
  probabilistic eviction of expired rows.

Three invariants are enforced in code and unit-tested:

1. **Errors are never persisted.** A loader exception propagates to
   the caller; nothing is written to Postgres. The test
   `test_loader_error_is_not_cached` exercises this path.
2. **Concurrent identical requests are deduplicated.** An `asyncio.Lock`
   per `(account_id, cache_key)` is held across the read-modify-write
   cycle. The test `test_concurrent_identical_requests_deduped`
   exercises two concurrent tasks and asserts exactly one loader call.
3. **GC is bounded.** `maybe_collect_garbage` rolls a dice against
   `COST_CACHE_GC_PROBABILITY` (default `0.01`) and on a hit deletes at
   most `COST_CACHE_GC_BATCH` (default `1000`) expired rows. The test
   `test_lazy_probabilistic_gc_runs_and_is_bounded` forces
   `probability=1.0` and asserts exactly `batch` rows are deleted.

TTL defaults to 6 hours (`COST_CACHE_TTL_SECONDS`), matching the Phase 2
spec. The cache row stores `(account_id, cache_key, query_type, period_start,
period_end, payload JSONB, created_at, expires_at)`.

## 8. Alembic migration story

* `backend/alembic.ini` declares `script_location = alembic` and a
  placeholder `sqlalchemy.url`.
* `backend/alembic/env.py` overrides `sqlalchemy.url` from
  `app.core.config.get_settings()` so the DSN is never duplicated.
* `backend/alembic/versions/0001_cost_cache.py` is the first migration.
  It uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
  so the upgrade is idempotent. Re-running `alembic upgrade head` on a
  fully-migrated database is a no-op.
* Phase 2 does NOT call `Base.metadata.create_all()` at runtime. The
  table is created exclusively by Alembic. This is enforced in
  `scripts/phase2_verify.sh`.

## 9. The pytest cache problem (and its fix)

When the backend container is built, `/app` is owned by root. The
container runs as the non-root `app` user (uid 999), which CANNOT
create `/app/.pytest_cache` — pytest's default cache directory.
Without intervention, every `pytest` invocation emits
`PytestCacheWarning: could not create cache path /app/.pytest_cache/...`
and silently disables the cache.

The fix in this phase:

* The Dockerfile **pre-creates** `/app/.pytest_cache` and `chown`s it
  to `app:app` (with `chmod 755`). `/app` itself stays root-owned so
  the running app cannot mutate its own source tree.
* `pytest.ini` leaves `cache_dir` at its default (`<rootdir>/.pytest_cache`)
  so local development and CI share the same configuration.
* `scripts/phase2_verify.sh` upgrades the warning to an error with
  `-W error::pytest.PytestCacheWarning` so a regression on the
  Dockerfile's cache-dir ownership is caught immediately.

This avoids `chmod 777 /app`, which would be the wrong answer for a
production image.

## 10. Live read-only validation (current run)

These were run against a real AWS account via the `make verify-phase2`
script. Numbers are from the Phase 2 verifier's last execution:

| Check                                          | Result                                |
| ---------------------------------------------- | ------------------------------------- |
| `GET /api/aws/identity`                        | 200 with `account=974053642038`       |
| `GET /api/aws/resources?region=us-east-1`     | 200; 88 EC2 + 123 EBS + 50 Lambda + 37 S3 + 2 RDS + 2 NAT; Resource Explorer returns real entries |
| `POST /api/aws/evidence`                       | 200; 140 resources enumerated; status `PARTIAL_SUCCESS` (CE denied, CW partial) |
| `POST /api/aws/utilization`                    | 200 with empty `resources` + a `cloudwatch` warning (Phase 1 IAM role lacks CloudWatch perms in this env) |
| `GET /api/aws/costs?days=7`                    | 502 `CostExplorerError` — sanitized; expected, because the Phase 1 read-only IAM role does not grant `ce:GetCostAndUsage`. Adding `ce:GetCostAndUsage` to the read-only role unblocks this endpoint without code changes. |
| `GET /api/aws/costs?days=15`                   | 422 `InvalidLookbackDays` — sanitized                  |

`scripts/phase2_verify.sh` exits 0 with **21 / 21 checks PASSED**.

## 11. End-to-end verifier

`scripts/phase2_verify.sh` runs in < 60 seconds and exits non-zero on
any failed check. It composes with `scripts/phase1_verify.sh` (which in
turn composes with `scripts/phase0_verify.sh`).

```
[check] Phase 1 regression check (delegated to phase1_verify.sh)
  PASS  phase1_verify.sh exited 0
  PASS  phase 1 verify summary: 14 passed
[check] Alembic configuration + env.py + initial migration present
  PASS  file: backend/alembic.ini
  PASS  file: backend/alembic/env.py
  PASS  file: backend/alembic/versions/0001_cost_cache.py
  PASS  0001_cost_cache creates (and drops) the cost_cache table
[check] alembic upgrade head applies cleanly (idempotent)
  PASS  alembic upgrade head succeeded
  PASS  cost_cache table present in Postgres
[check] CostCache ORM model + dialect-aware JSONB type importable
  PASS  ORM imports + SQLite create_all succeeds
[check] Phase 2 AWS service modules importable from the backend
  PASS  Phase 2 services importable (CE + CloudWatch + Guard + Cache + Evidence)
[check] Phase 2 Pydantic schemas + cost-scope invariant
  PASS  Phase 2 schemas importable + CostScope has no RESOURCE member
[check] Cost cache never hardcodes credentials or DSN
  PASS  no hardcoded DSN in cache / evidence modules
[check] pytest run with cache_dir writable (no PytestCacheWarning)
  PASS  targeted Phase 2 pytest: 98 passed (PytestCacheWarning would have failed the run)
  PASS  default pytest cache provider emits NO PytestCacheWarning
[check] Every Boto3 call site is guarded by assert_read_only
  PASS  guard wiring looks plausible in cost_explorer.py + cloudwatch_metrics.py
[check] No write/mutation AWS APIs in Phase 2 source
  PASS  no write/mutation AWS APIs in Phase 2 source
[check] GET /api/aws/costs responds
  PASS  /api/aws/costs reachable with sanitized error: CostExplorerError
[check] POST /api/aws/utilization responds
  PASS  /api/aws/utilization shape correct
[check] POST /api/aws/evidence responds
  PASS  /api/aws/evidence shape correct
[check] Invalid lookback yields sanitized 422, never 500
  PASS  /api/aws/costs?days=15 returns 422 InvalidLookbackDays
[check] Secret scan (Phase 2 extra-strict)
  PASS  no obvious secrets in tracked files

Phase 2 verify: 21 passed, 0 failed
```

## 12. Test coverage

Phase 2 adds 5 new test modules (98 tests total):

| Module                                       | Count | What it exercises                              |
| -------------------------------------------- | ----- | ---------------------------------------------- |
| `tests/test_cost_explorer_service.py`        | 13    | Period math, pagination, sanitization, summary |
| `tests/test_cloudwatch_batching.py`          | 22    | Batch chunking, query_id safety, classifier, aggregation |
| `tests/test_aws_read_only_guard.py`          | 51    | Forbidden / read-only / unclassified operation handling |
| `tests/test_cost_cache.py`                   | 6     | MISS / HIT / REFRESHED, error-not-cached, dedup, GC bounded |
| `tests/test_cost_evidence_builder.py`        | 6     | Scope invariant, SERVICE-scope spend, status, data_quality, sources attribution |

Combined with the Phase 0 + Phase 1 suite, `make test` runs 134 tests
and exits 0.

## 13. Deferred to later phases

* Compute Optimizer + Cost Optimization Hub (Phase 3).
* LiteLLM/AI cost analysis, prompts, and answer generation (Phase 4).
* JWT auth, signup, login, RBAC.
* Report-history persistence beyond the cost_cache.
* Production dashboard, WebSockets, live updates.
* Terraform/IaC, automated AWS remediation, multi-region fanout.
* SLI/SLO dashboards, audit log of Phase 2 reads (none ship in this slice).

## 14. Files added or changed in Phase 2

```
backend/alembic.ini                                  (new)
backend/alembic/env.py                               (new)
backend/alembic/script.py.mako                       (new)
backend/alembic/versions/0001_cost_cache.py          (new)
backend/app/api/aws_costs.py                         (new)
backend/app/api/aws_evidence.py                      (new)
backend/app/api/aws_utilization.py                   (new)
backend/app/db/__init__.py                           (new)
backend/app/db/models.py                             (new)
backend/app/db/session.py                            (new)
backend/app/schemas/cost.py                          (new)
backend/app/schemas/evidence.py                      (new)
backend/app/schemas/utilization.py                   (new)
backend/app/services/aws/guard.py                    (new)
backend/app/services/aws/cost_explorer.py            (new)
backend/app/services/aws/cloudwatch_metrics.py       (new)
backend/app/services/cost_cache.py                   (new)
backend/app/services/cost_evidence_builder.py        (new)
backend/app/main.py                                  (modified — register Phase 2 routes)
backend/Dockerfile                                   (modified — pytest cache dir)
backend/.dockerignore                                (modified)
backend/pytest.ini                                   (modified — comment only)
backend/requirements.txt                             (modified — Alembic)
backend/tests/test_aws_read_only_guard.py            (new)
backend/tests/test_cloudwatch_batching.py            (new)
backend/tests/test_cost_cache.py                     (new)
backend/tests/test_cost_evidence_builder.py          (new)
backend/tests/test_cost_explorer_service.py          (new)
docs/phase2-cost-intelligence.md                     (this file)
scripts/phase2_verify.sh                             (new)
Makefile                                             (modified — verify-phase2)
README.md                                            (modified — Phase 2 quick-start)
```
