# Phase 1 — AWS Identity & Resource Discovery (Runbook)

> **Status:** Phase 1 (slice 1) — read-only identity + multi-service inventory.
> **Out of scope this slice:** JWT auth, signup/login, report history,
> persistence, Cost Explorer, CloudWatch, Compute Optimizer, LiteLLM/AI,
> frontend dashboard, WebSockets, Terraform/IaC, automated remediation,
> multi-region fanout.

This runbook explains how to validate the Phase 1 discovery slice against
a real AWS account, what each endpoint does, the exact IAM permissions
required, and what the slice intentionally does NOT do.

---

## 1. What ships in this slice

Two unauthenticated FastAPI endpoints, exposed behind Nginx at
`/api/aws/*`:

| Method | Path                  | Purpose                                                  |
| ------ | --------------------- | -------------------------------------------------------- |
| GET    | `/api/aws/identity`   | STS `GetCallerIdentity` (Account / ARN / UserId)         |
| GET    | `/api/aws/resources`  | Per-service resource inventory + optional enrichment     |

Both go through the **Boto3 default credential chain** — there is NO
`AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` in `.env.example`,
`Settings`, or any tracked file.

---

## 2. Credential chain in this deployment

In production, the backend container is expected to run on the same
EC2 host that already hosts Nginx, PostgreSQL, and LiteLLM. AWS
credentials therefore come from the **EC2 instance profile** that the
host's IAM role attaches to the instance.

Resolution order (highest to lowest priority):

1. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the environment of
   the backend container (Boto3 default behaviour — set by the
   `docker-compose.yml` `environment:` block; intentionally left unset
   in `.env.example`).
2. Shared credentials/config files inside the container
   (`~/.aws/credentials`, `~/.aws/config`).
3. **EC2 instance profile / ECS task role / EKS pod identity** — the
   intended source for production.

If none of these are present, Boto3 raises `NoCredentialsError` on the
first API call. The routes translate this into a sanitized
**HTTP 502** response with `error_code: "NoCredentialsError"` — they
never leak the underlying exception text or stack trace.

---

## 3. Endpoints

### 3.1 `GET /api/aws/identity`

Returns the AWS caller's identity as reported by
`sts:GetCallerIdentity`.

**Query parameters**

| Name     | Required | Description                                                                 |
| -------- | -------- | --------------------------------------------------------------------------- |
| `region` | no       | Override the AWS region. When absent, falls back to `AWS_DEFAULT_REGION` from backend settings, then Boto3's own region resolution. |

**Successful response (HTTP 200)**

```json
{
  "account": "111122223333",
  "arn": "arn:aws:iam::111122223333:role/CostDetectiveInstanceRole",
  "user_id": "AROAXXXXXXXXXXXXXXXXX",
  "region": "us-east-1"
}
```

**Credential / STS error response (HTTP 502, sanitized)**

```json
{
  "status": "error",
  "error_code": "NoCredentialsError",
  "region": "us-east-1"
}
```

**Sample invocations**

```bash
# Use the default region from settings.
curl -s http://localhost/api/aws/identity | jq

# Override the region.
curl -s 'http://localhost/api/aws/identity?region=eu-west-1' | jq
```

### 3.2 `GET /api/aws/resources`

Enumerates read-only metadata across eight AWS services and attaches
two optional enrichment layers (Resource Explorer + Resource Groups
Tagging API). Per-service Boto3 errors are surfaced in the response
body as `status: "denied"` or `status: "error"` — they NEVER fail the
whole call.

**Query parameters**

| Name     | Required | Description                                                                 |
| -------- | -------- | --------------------------------------------------------------------------- |
| `region` | no       | Same precedence as `/api/aws/identity`. **Single-region only** — multi-region fanout is out of scope for this slice. |

**Successful response (HTTP 200)**

```json
{
  "region": "us-east-1",
  "services": {
    "ec2":    { "service": "ec2",    "status": "ok", "items": [...], "error_code": null },
    "ebs":    { "service": "ebs",    "status": "ok", "items": [...], "error_code": null },
    "eip":    { "service": "eip",    "status": "ok", "items": [...], "error_code": null },
    "nat":    { "service": "nat",    "status": "ok", "items": [...], "error_code": null },
    "elbv2":  { "service": "elbv2",  "status": "ok", "items": [...], "error_code": null },
    "rds":    { "service": "rds",    "status": "ok", "items": [...], "error_code": null },
    "lambda": { "service": "lambda", "status": "ok", "items": [...], "error_code": null },
    "s3":     { "service": "s3",     "status": "ok", "items": [...], "error_code": null }
  },
  "enrichment": {
    "resource_explorer": {
      "available": true,
      "query": "",
      "results": [
        { "arn": "arn:aws:ec2:us-east-1:111:instance/i-abc", "service": "ec2", "resource_type": "AWS::EC2::Instance", "region": "us-east-1" }
      ],
      "error_code": null
    },
    "tagging": {
      "available": true,
      "tags_by_arn": {
        "arn:aws:ec2:us-east-1:111:instance/i-abc": { "env": "prod", "team": "data" }
      },
      "error_code": null
    }
  }
}
```

**Per-service failure example (HTTP 200, one service denied)**

```json
{
  "region": "us-east-1",
  "services": {
    "ec2": { "service": "ec2", "status": "denied", "items": [], "error_code": "AccessDenied" },
    "rds": { "service": "rds", "status": "ok",     "items": [...], "error_code": null }
  },
  "enrichment": { "...": "..." }
}
```

**Sample invocation**

```bash
curl -s 'http://localhost/api/aws/resources?region=us-east-1' | jq
```

---

## 4. Optional enrichment layers

Both layers **degrade gracefully** — if the account has no Resource
Explorer index, or the role lacks `tag:GetResources`, the response
still comes back HTTP 200 with `available: false` and a sanitized
`error_code`. The rest of the discovery call is unaffected.

| Layer                | Boto3 service           | When `available=false`                                             |
| -------------------- | ----------------------- | ------------------------------------------------------------------ |
| Resource Explorer    | `resource-explorer-2`   | No index configured in the account, or `Search` denied             |
| Resource Groups Tags | `resourcegroupstaggingapi` | `GetResources` denied, or no ARNs to look up                    |

---

## 5. Required IAM permissions

Attach this **read-only** policy to the IAM role that backs the EC2
instance profile. It contains the minimum set needed for the eight
service enumerators and the two enrichment layers.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "sts:GetCallerIdentity",                  "Resource": "*" },

    { "Effect": "Allow", "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "ec2:DescribeNatGateways"
      ], "Resource": "*" },

    { "Effect": "Allow", "Action": "elasticloadbalancing:DescribeLoadBalancers", "Resource": "*" },

    { "Effect": "Allow", "Action": [
        "rds:DescribeDBInstances",
        "lambda:ListFunctions",
        "lambda:GetFunction",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging"
      ], "Resource": "*" },

    { "Effect": "Allow", "Action": [
        "resource-explorer-2:Search",
        "tag:GetResources"
      ], "Resource": "*" }
  ]
}
```

No `*` actions beyond those listed above. No write actions. No
`iam:*`. No `sts:AssumeRole`.

---

## 6. Manual live validation (runbook)

This slice is validated against mocks in CI; live AWS is not exercised
in the automated verify pipeline. To validate against a real account:

1. **Stack is up:** `make up && make verify` — every Phase 0 check plus
   the new identity smoke check passes.
2. **Identity endpoint:**
   - With instance profile attached: `curl -s
     http://localhost/api/aws/identity | jq` returns HTTP 200 with
     `account`, `arn`, `user_id`, `region`.
   - Without credentials: same call returns HTTP 502 with
     `error_code: "NoCredentialsError"` and no exception text leaked.
3. **Region override:** `curl -s
   'http://localhost/api/aws/identity?region=eu-west-1' | jq` —
   response's `region` field matches the query param.
4. **Resource inventory:** `curl -s
   'http://localhost/api/aws/resources?region=us-east-1' | jq` —
   response contains all eight `services` keys and the `enrichment`
   block. The status of each service reflects whether the IAM policy
   covers it (`ok`, `denied`, or `error`).
5. **Enrichment degradation:** in an account with no Resource Explorer
   index, `enrichment.resource_explorer.available` is `false` with
   `error_code: "ResourceNotFoundException"`, but the rest of the
   response is still HTTP 200.
6. **Secret scanner:** `make verify` still reports
   `no obvious secrets in tracked files` — no AWS keys, no AI keys.

---

## 7. Known limitations (intentional, deferred)

- **No authentication.** Endpoints are open within the internal Docker
  network (still behind Nginx). JWT/session auth ships in a separate
  slice.
- **No persistence.** Every `/api/aws/resources` call re-lists AWS
  state. There is no `discovery_runs` table, no Alembic migration, no
  history. Diff/change tracking is out of scope.
- **Single region per request.** The region query param selects one
  region; multi-region fanout across `ec2:DescribeRegions` is out of
  scope this slice.
- **No cost data.** Resource enumeration only. Cost Explorer,
  CloudWatch, Compute Optimizer, and the Cost Optimization Hub are
  deferred to a later phase.
- **No LLM calls.** No OpenAI / Anthropic / Gemini / Bedrock calls.
  LiteLLM remains untouched.
- **Live AWS not in CI.** Tests are mock-based; live validation is
  the runbook above. No moto / LocalStack.
- **No S3 object listing.** `list_s3_buckets` returns bucket metadata
  only (`list_buckets` + per-bucket `get_bucket_location`). It does
  not enumerate or read bucket contents.

---

## 8. Where the code lives

| File                                                  | Role                                                   |
| ----------------------------------------------------- | ------------------------------------------------------ |
| `backend/requirements.txt`                            | `boto3==1.34.131`, `botocore==1.34.131` pinned         |
| `backend/app/services/aws/clients.py`                 | `get_aws_client(service, region=None)` factory          |
| `backend/app/services/aws/identity.py`                | STS `get_caller_identity` + `AwsIdentityError`          |
| `backend/app/services/aws/resources.py`               | 8 per-service enumerators + aggregator                 |
| `backend/app/services/aws/enrichment.py`              | Resource Explorer + Tagging API enrichers              |
| `backend/app/schemas/aws.py`                          | Normalized pydantic models + envelopes                 |
| `backend/app/api/aws.py`                              | `GET /identity`, `GET /resources` FastAPI routes       |
| `backend/tests/test_aws_identity.py`                  | Mock tests for STS path                                |
| `backend/tests/test_aws_resources.py`                 | Mock tests for the 8 enumerators                       |
| `backend/tests/test_aws_enrichment.py`                | Mock tests for enrichment + aggregator route           |
| `scripts/phase0_verify.sh`                            | Existing verify + new `/api/aws/identity` smoke check  |
