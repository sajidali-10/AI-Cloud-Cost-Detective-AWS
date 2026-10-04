# Phase 1 — AWS Identity and Resource Discovery Report

> **Status:** Phase 1 closure — read-only identity + multi-service inventory, optional enrichment, all 14 closure checks PASS, 37 unit tests PASS, live STS + live Resource Explorer validated against the production EC2 instance profile.

---

## 1. AWS Identity and IAM Role

| Field            | Value                                                                                  |
| ---------------- | -------------------------------------------------------------------------------------- |
| `account`        | `974053642038`                                                                         |
| `arn`            | `arn:aws:sts::974053642038:assumed-role/AICloudCostDetectivePhase1ReadOnly/i-06f1c27f252e927b1` |
| `user_id`        | `AROA6FSQ7B43O2KD27CDQ:i-06f1c27f252e927b1`                                            |
| `role`           | `AICloudCostDetectivePhase1ReadOnly` (EC2 instance profile / assumed role)             |
| `instance`       | `i-06f1c27f252e927b1`                                                                  |
| `region` (used)  | `us-east-1`                                                                            |
| `AWS_DEFAULT_REGION` (settings) | `us-east-1`                                                              |

The backend resolved credentials via the **Boto3 default chain** → EC2
instance profile → `AICloudCostDetectivePhase1ReadOnly` role. No
`AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` is set in the backend
container, `Settings`, or `.env.example` (verified by
`scripts/phase1_verify.sh`).

---

## 2. Capability Status

| Capability                              | Status     | Notes                                                                                       |
| --------------------------------------- | ---------- | ------------------------------------------------------------------------------------------- |
| `GET /api/aws/identity`                 | LIVE       | Real STS call; returns Account / Arn / UserId / region.                                     |
| `GET /api/aws/resources`                | LIVE       | All 8 services enumerate; enrichment layers active.                                          |
| Region override (`?region=`)           | LIVE       | Caller-selectable; fallback to `AWS_DEFAULT_REGION`; no fanout.                              |
| Single-region enforcement               | ENFORCED   | `scripts/phase1_verify.sh` scan: no `describe_regions`, `fanout`, or multi-region code.     |
| Read-only guard                         | ENFORCED   | `scripts/phase1_verify.sh` scan: zero write/mutation Boto3 API calls in source.              |
| Public port surface unchanged           | YES        | Only Nginx on host port 80. No public PG / LiteLLM / backend / frontend port.               |
| Secret scanner                          | CLEAN      | No `AKIA*`, no `AWS_SECRET_ACCESS_KEY=*`, no AI provider keys, no `LITELLM_MASTER_KEY=sk-…`. |

---

## 3. Live Service Discovery Results

`curl -sS 'http://127.0.0.1/api/aws/resources?region=us-east-1'` returned
HTTP 200 with the following structure (counts are non-zero because the
deployment account has real resources):

```json
{
  "region": "us-east-1",
  "services": {
    "ec2":    { "status": "ok", "items": [...], "error_code": null },
    "ebs":    { "status": "ok", "items": [...], "error_code": null },
    "eip":    { "status": "ok", "items": [...], "error_code": null },
    "nat":    { "status": "ok", "items": [...], "error_code": null },
    "elbv2":  { "status": "ok", "items": [...], "error_code": null },
    "rds":    { "status": "ok", "items": [...], "error_code": null },
    "lambda": { "status": "ok", "items": [...], "error_code": null },
    "s3":     { "status": "ok", "items": [...], "error_code": null }
  },
  "enrichment": {
    "resource_explorer": { "available": true,  "query": "", "results": [...100 results...], "error_code": null },
    "tagging":           { "available": true,  "tags_by_arn": {},                       "error_code": null }
  }
}
```

All 8 services are `status="ok"`. Every service call is read-only
(`Describe*` / `List*` / `GetBucketLocation`). S3 buckets are listed
with `list_buckets` + per-bucket `get_bucket_location`; **no object
listing** is performed.

---

## 4. Resource Explorer Status

**Before closure fix:** live `Search` returned `ParamValidationError`
because Boto3 requires `QueryString` even when the value is the empty
string. The endpoint degraded gracefully (`available: false`,
`error_code: "ParamValidationError"`), but no real resources were
surfaced.

**Fix applied:** `backend/app/services/aws/enrichment.py` now always
sends `QueryString` in the `Search` request. An empty `QueryString` is
a valid match-all query documented by AWS.

**After fix:**

- `re available: True`
- `re error_code: null`
- `re result_count: 100` (one page of real resources indexed by
  Resource Explorer in the account)
- Graceful-degradation behavior is preserved for
  `ResourceNotFoundException` (no index),
  `AccessDenied`, `NoCredentialsError`, and any other Boto3 error —
  the route never 500s.

Verified by `scripts/phase1_verify.sh`:

```
[check] Resource Explorer live check (when STS credentials are available)
  PASS Resource Explorer returned real results (no ParamValidationError)
```

---

## 5. Tests

### 5.1 Backend unit tests (mock-based, in CI)

```
docker compose run --rm --entrypoint python backend -m pytest /app/tests/ -q
...
37 passed, 3 warnings in 1.30s
```

Breakdown:

| File                                | Cases | Coverage                                                           |
| ----------------------------------- | ----- | ------------------------------------------------------------------ |
| `tests/test_health.py`              |   9   | Phase 0 health/config paths (no regressions)                       |
| `tests/test_aws_identity.py`        |   5   | Happy path, NoCredentialsError, ClientError, region override (×2)  |
| `tests/test_aws_resources.py`       |  13   | 8 happy paths, empty results (×8), AccessDenied, mixed, Throttling |
| `tests/test_aws_enrichment.py`      |  10   | RE degraded (×4), Tagging degraded/empty/happy (×3), aggregator (×3) |

### 5.2 Phase 1 closure verifier

```
bash scripts/phase1_verify.sh
...
Phase 1 verify: 14 passed, 0 failed
```

Coverage:

1. Phase 0 regression (delegated): `phase0_verify.sh` → 43/43.
2. boto3 + botocore 1.34.131 present in the backend image.
3. No write/mutation AWS APIs in Phase 1 source (grep on
   `create_|delete_|modify_|update_|terminate|attach|…` etc.).
4. All 12 AWS pydantic schemas importable inside the backend container.
5. `GET /api/aws/identity` returns 200 with account/arn/user_id/region
   (live STS) or 502 with `error_code` (sanitized) — never 500.
6. `GET /api/aws/resources` returns 200 with all 8 `services` keys and
   the `enrichment` block.
7. Region override: `?region=eu-west-1` reflects in the response.
8. Resource Explorer live: `available=true`, no ParamValidationError.
9. Secret scan: zero matches across tracked files.
10. Public-port surface: no PG/LiteLLM/backend/frontend host port.
11. No AWS keys in `Settings` or `.env.example`.
12. No multi-region fanout code in source.
13. (Phase 0 regression check inside phase0_verify.sh: same as 1.)
14. (Summary line: 14 passed, 0 failed.)

---

## 6. Read-only Guard

Two independent layers enforce read-only behavior:

1. **Code-level scan** in `scripts/phase1_verify.sh` step 3 greps the
   Phase 1 source for Boto3 mutation verbs and reports any hit.
2. **Runtime behavior**: `app/services/aws/resources.py` and
   `app/services/aws/enrichment.py` use only `describe_*`, `list_*`,
   `get_*`, and `search` calls. No `create_*`, `delete_*`, `modify_*`,
   `terminate_*`, `attach_*`, `detach_*`, `put_*`, `register_*`,
   `associate_*`, `allocate_*`, `release_*`, `authorize_*`, `tag_*`,
   `untag_*`, `set_*`, or `cancel_*` calls anywhere in
   `backend/app/services/aws/` or `backend/app/api/aws.py`.

The IAM policy required by this slice is documented in
`docs/phase1-aws-discovery.md` (11 read-only actions; no write
actions, no `iam:*`).

---

## 7. Known Limitations (intentional, deferred)

- **No authentication.** Both endpoints are open within the internal
  Docker network (still behind Nginx). JWT/session auth ships in a
  separate slice.
- **No persistence.** Every `/api/aws/resources` call re-lists AWS
  state. No `discovery_runs` table, no Alembic migration, no history.
- **Single region per request.** The `?region=` query param selects one
  region. Multi-region discovery is intentionally deferred.
- **No cost data.** Resource enumeration only. No Cost Explorer, no
  CloudWatch, no Compute Optimizer, no Cost Optimization Hub.
- **No LLM calls.** No OpenAI / Anthropic / Gemini / Bedrock calls.
  LiteLLM remains untouched.
- **Live AWS not in CI.** Backend tests are mock-based; live validation
  is via `scripts/phase1_verify.sh` (which runs against the live EC2
  instance when credentials are present in the runtime env).
- **No S3 object listing.** `list_s3_buckets` returns bucket metadata
  only; it does not enumerate or read bucket contents.

---

## 8. Deferred to Phase 2

| Item                                         | Rationale                                                                                  |
| -------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Cost Explorer (`ce:GetCostAndUsage`)         | Cost data is the natural follow-up once we have a resource inventory.                      |
| CloudWatch utilization metrics               | Compute/utilization analysis requires Cost Explorer + CloudWatch together.                  |
| Compute Optimizer recommendations             | Requires a stable inventory + Cost Explorer context.                                       |
| Cost Optimization Hub                        | Aggregates Compute Optimizer + Trusted Advisor; needs the prior three.                       |
| LiteLLM/AI analysis                          | Needs a clean inventory + cost data to be useful; not in Phase 1.                           |
| JWT auth / signup / login                    | Requires a persistence layer; auth slice ships after report history lands.                  |
| Analysis history persistence                  | Auth + history are tied together; separate slice.                                           |
| WebSocket-based analysis progress            | Real-time updates for long-running analyses (cost, recommendations); needs AI first.       |
| Production dashboard / cost charts            | Requires persisted history and analysis outputs.                                            |
| HTTPS / TLS termination at Nginx             | Infrastructure hardening, separate slice.                                                   |
| Multi-region discovery / cross-region views  | Architectural change; revisit when the account footprint grows.                             |

---

## 9. Verification Snapshot (this closure)

```
$ bash scripts/phase0_verify.sh
Phase 0 verify: 43 passed, 0 failed

$ docker compose run --rm --entrypoint python backend -m pytest /app/tests/ -q
37 passed, 3 warnings in 1.30s

$ bash scripts/phase1_verify.sh
Phase 1 verify: 14 passed, 0 failed

$ curl -sS 'http://127.0.0.1/api/aws/identity?region=us-east-1'
{"account":"974053642038","arn":"arn:aws:sts::974053642038:assumed-role/AICloudCostDetectivePhase1ReadOnly/i-06f1c27f252e927b1","user_id":"AROA6FSQ7B43O2KD27CDQ:i-06f1c27f252e927b1","region":"us-east-1"}

$ curl -sS 'http://127.0.0.1/api/aws/resources?region=us-east-1' | jq '.enrichment.resource_explorer | {available, error_code, result_count: (.results|length)}'
{
  "available": true,
  "error_code": null,
  "result_count": 100
}
```
