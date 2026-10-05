# Phase 3 — AWS Optimization Intelligence

> **Scope.** This document describes the Phase 3 slice: a trustworthy
> optimization engine that joins AWS Cost Optimization Hub, AWS Compute
> Optimizer, and a deterministic rule engine (Phase 1 inventory + Phase 2
> CloudWatch evidence) into a deduplicated, savings-attributed
> recommendation model.  AI/LiteLLM analysis is intentionally NOT part
> of this phase.

## 1. Goals

Phase 3 ships three capabilities on top of the Phase 0-2 foundation:

1. **AWS Compute Optimizer integration.** Read-only enrollment check
   + per-resource recommendations for EC2, EBS, Lambda, and RDS, with
   deterministic normalization into the Phase 3 schema.
2. **AWS Cost Optimization Hub integration.** Read-only enrollment
   check + paginated recommendation list, treated as the preferred
   authoritative source for standardized savings.
3. **Deterministic rule engine.** Six pure rules covering unattached
   EBS, unused EIPs, low-utilization EC2, idle NAT gateways, idle ALB /
   NLB, and RDS underutilization.  Each rule consumes only Phase 1
   inventory + Phase 2 CloudWatch evidence; no AWS call happens inside
   a rule.

A normalized recommendation model, deduplication algorithm, summary
breakdown, and three FastAPI endpoints (capabilities, recommendations,
summary) round out the surface.

## 2. Architecture

```
                       ┌────────────────────────────────────────┐
                       │         AWS (read-only, Phase 3)       │
                       │ Compute Optimizer   |   Cost Opt Hub   │
                       │  get_enrollment_status                  │
                       │  get_recommendation_summaries           │
                       │  get_ec2_instance_recommendations       │
                       │  get_ebs_volume_recommendations         │
                       │  get_lambda_function_recommendations    │
                       │  get_rds_database_recommendations       │
                       │  list_enrollment_statuses               │
                       │  get_preferences                        │
                       │  list_recommendations                   │
                       │  list_recommendation_summaries          │
                       └─────────────┬──────────────────────────┘
                                     │
                  ┌──────────────────┴──────────────────┐
                  │                                     │
   ┌──────────────▼────────────┐   ┌────────────────────▼────────────┐
   │  app/services/aws/        │   │  app/services/aws/              │
   │  compute_optimizer.py     │   │  cost_optimization_hub.py       │
   └──────────────┬────────────┘   └────────────────────┬────────────┘
                  │                                     │
                  └────────────────┬────────────────────┘
                                   ▼
                  ┌────────────────────────────────────────┐
                  │  app/services/optimization_engine.py   │
                  │  - deduplicate by (resource_id, action) │
                  │  - prefer COH > CO > CALCULATED > UNK   │
                  │  - merge sources list, fold secondary   │
                  │  - exclude UNKNOWN from savings total   │
                  └─────────────────┬──────────────────────┘
                                    │
                  ┌─────────────────┴─────────────────┐
                  │                                   │
   ┌──────────────▼──────────┐       ┌────────────────▼──────────────┐
   │  app/services/          │       │  app/schemas/optimization.py   │
   │  optimization_rules.py  │       │  Recommendation, Capabilities, │
   │  (pure, deterministic)  │       │  Summary, SavingsSource, etc.  │
   └─────────────────────────┘       └───────────────────────────────┘
                                    │
                                    ▼
                  ┌────────────────────────────────────────┐
                  │  app/api/aws_optimization.py           │
                  │  GET /api/aws/optimization/capabilities│
                  │  GET /api/aws/optimization/recommend.  │
                  │  GET /api/aws/optimization/summary     │
                  └────────────────────────────────────────┘
```

## 3. Module map

| Module | Role |
| ------ | ---- |
| `app/services/aws/compute_optimizer.py` | Compute Optimizer wrappers (enrollment + per-resource) |
| `app/services/aws/cost_optimization_hub.py` | Cost Optimization Hub wrappers (enrollment + list) |
| `app/services/optimization_rules.py` | Six deterministic rules + thresholds + ID generator |
| `app/services/optimization_engine.py` | Orchestrator: fetch → normalize → dedup → summarize |
| `app/schemas/optimization.py` | Pydantic wire contract (Recommendation, Summary, Capabilities) |
| `app/api/aws_optimization.py` | FastAPI routes |
| `tests/test_compute_optimizer_service.py` | 17 tests |
| `tests/test_cost_optimization_hub_service.py` | 17 tests |
| `tests/test_optimization_rules.py` | 30 tests |
| `tests/test_optimization_engine.py` | 10 tests |

## 4. Compute Optimizer

`get_enrollment_status(client, account_id=None)` returns the
account-level state.  AWS reports `Active` / `Inactive` / `Pending` /
`Failed`; the wrapper normalizes them to uppercase and maps anything
else to `UNAVAILABLE`.

Per-resource retrievers:

* `get_ec2_instance_recommendations(client, region)` — paginates
  `instanceRecommendations`, captures `instanceArn` /
  `instanceId`, `finding`, `currentInstanceType`, the FIRST
  `recommendationOptions` entry, `lookbackPeriodInDays`,
  `performanceRisk`, `estimatedMonthlySavings`, `reasonCodes`, and
  `recommendationId`.  Missing fields stay `None`.
* `get_ebs_volume_recommendations(client, region)` — same shape;
  volumeType / volumeSize / baselineIOPS are surfaced through
  `current_configuration` and `recommended_configuration`.
* `get_lambda_function_recommendations(client, region)` — captures
  `currentMemorySize` and the recommended memory.
* `get_rds_database_recommendations(client, region)` — captures the
  DB class and the first recommended option.

Pagination walks `nextToken` until empty; missing `nextToken` fields
are tolerated.  All call sites are wrapped by `assert_read_only`.

The service is **single-region** for the resource recommendations
(the API accepts a `region` filter on each call).  Enrollment is
**account-level** and does not take a region.

## 5. Cost Optimization Hub

* `list_enrollment_statuses(client, account_id=None)` — the hub returns
  one row per account.  We map `Active` → `ACTIVE`, `Inactive` →
  `NOT_ENROLLED`, `Pending` → `PENDING`, `Failed` → `FAILED`.  An
  empty response (no enrollment) is treated as `NOT_ENROLLED` so the
  UI can render a distinct message.
* `get_preferences(client, account_id=None)` — surfaces the account's
  hub preferences.  Failures here are sanitized via
  `CostOptimizationHubError`.
* `list_recommendations(client, region, ...)` — paginates
  `ListRecommendations` with `maxResults=100`.  Each row is normalized
  into a `NormalizedHubRecommendation` carrying `currentResourceSummary`,
  `recommendedResourceSummary`, `implementationEffort`,
  `restartNeeded`, `rollbackPossible`, `estimatedMonthlySavings`,
  `savingsPercentage`, and `actionType`.  The orchestrator never calls
  `GetRecommendation` per row (avoiding an N+1 API storm); the list
  endpoint already carries every field we need.
* `list_recommendation_summaries(client, ...)` — paginates the summary
  view; useful for the capabilities endpoint.

We never call `UpdateEnrollmentStatus`, `UpdatePreferences`,
`PutRecommendationPreferences`, or `DeleteRecommendationPreferences`
— those would activate paid features or mutate state.

## 6. Deduplication

AWS-native sources overlap.  Compute Optimizer and Cost Optimization
Hub can both produce a rightsizing recommendation for the same
EC2 instance.  The orchestrator's deduplicator prevents the savings
total from inflating.

Algorithm:

1. Each source emits a `RecommendationCandidate` row with a stable
   `key = (resource_id, action.value)`.
2. When two candidates share a key, the one with the lowest
   `SOURCE_PRECEDENCE` (`AWS_COST_OPTIMIZATION_HUB` <
   `AWS_COMPUTE_OPTIMIZER` < `CALCULATED` < `UNKNOWN`) wins.
3. The loser's `evidence` blocks and `aws_recommendation_ids` are
   merged into the primary.  The savings figure from the loser's
   source NEVER replaces the primary's number — the primary's figure
   (or `None`) is what flows into the summary.
4. Deterministic candidates use a deterministic recommendation id of
   the form `det-<action>-<sha256[:16]>` so they can never collide
   with an AWS-native row that uses the real `resource_id`.  Two
   deterministic candidates for the same resource + action collapse
   into a single row.

The dedup is deterministic: given the same inputs, the orchestrator
returns the same set of recommendations with the same `primary_source`,
`sources`, and `aws_recommendation_ids` lists every time.

## 7. Deterministic rule engine

All six rules live in `app/services/optimization_rules.py`.  They are
pure: no AWS call happens inside a rule.

| Rule | Action | Trigger | Confidence |
| ---- | ------ | ------- | ---------- |
| Unattached EBS | `REVIEW_DELETE_UNATTACHED_EBS` | `state == "available"` | HIGH |
| Unused EIP | `REVIEW_RELEASE_UNUSED_EIP` | `AssociationId` absent | HIGH |
| Low-utilization EC2 | `REVIEW_LOW_UTILIZATION_EC2` | CPU avg < 10% AND CPU max < 40% AND lookback ≥ 7d AND data quality HIGH/MEDIUM | MEDIUM |
| Idle NAT Gateway | `REVIEW_IDLE_NAT_GATEWAY` | All datapoints zero AND HIGH data quality AND state != `pending` | MEDIUM |
| Idle ALB / NLB | `REVIEW_IDLE_LOAD_BALANCER` | All datapoints zero AND HIGH data quality | MEDIUM |
| RDS Underutilization | `REVIEW_LOW_UTILIZATION_RDS` | CPU avg < 10% AND max < 40% AND DB connections avg < 5 AND data quality HIGH/MEDIUM | MEDIUM |

Critical non-behaviors:

* **Missing CloudWatch datapoints are never treated as zero.**  A
  resource with no coverage is reported as `data_quality=no_data` and
  is excluded from the rules that require HIGH or MEDIUM coverage.
* **No local pricing calculations.**  Deterministic rules never
  compute a dollar savings figure.  They set
  `estimated_monthly_savings=None` and `savings_source=UNKNOWN` so
  the UI can render an explicit "no estimate" badge.
* **No instance-type guessing.**  The EC2 / RDS rules never propose a
  replacement class.  They emit `REVIEW_*` candidates and defer the
  concrete recommendation to Compute Optimizer or Cost Optimization
  Hub when those are available.
* **Threshold changes are gated by tests.**  The thresholds
  (`EC2_CPU_AVG_MAX_THRESHOLD`, `RDS_DB_CONNECTIONS_AVG_THRESHOLD`,
  etc.) are centralized at the top of `optimization_rules.py` and
  exercised by `test_optimization_rules.py`, so a tuning change is
  caught by the test suite.

## 8. Recommendation model

Every recommendation that leaves the backend has the same shape
(defined in `app/schemas/optimization.py`):

```json
{
  "recommendation_id": "det-reviewdeleteunattachedebs-7b1f2a90...",
  "resource_id": "vol-0abc123",
  "resource_arn": null,
  "resource_type": "EBS_VOLUME",
  "region": "us-east-1",
  "account_id": "974053642038",
  "action": "REVIEW_DELETE_UNATTACHED_EBS",
  "title": "Unattached EBS volume vol-0abc123",
  "finding": "EBS volume is in the 'available' state with zero attachments.",
  "current_configuration": {"volume_id": "vol-0abc123", "size_gb": 100, "state": "available", "attachment_count": 0},
  "recommended_configuration": {"recommendation": "REVIEW_DELETE_UNATTACHED_EBS"},
  "estimated_monthly_savings": null,
  "currency": "USD",
  "savings_percentage": null,
  "savings_source": "UNKNOWN",
  "primary_source": "UNKNOWN",
  "sources": ["UNKNOWN"],
  "confidence": "HIGH",
  "data_quality": "high",
  "reason_codes": ["EBS_AVAILABLE_STATE"],
  "restart_needed": null,
  "rollback_possible": true,
  "evidence": [
    {
      "source": "UNKNOWN",
      "confidence": "HIGH",
      "data": {"size_gb": 100, "state": "available", "attachment_count": 0},
      "reason_codes": ["EBS_AVAILABLE_STATE"]
    }
  ],
  "aws_recommendation_ids": []
}
```

AWS-native recommendations carry a savings figure, an AWS
recommendation id, and an evidence block whose `data` matches the
shape of the source API response (findings, lookback, performance
risk, recommended options, restart / rollback flags).

## 9. API surface

### GET /api/aws/optimization/capabilities

```bash
curl -s 'http://localhost/api/aws/optimization/capabilities' | jq
```

```json
{
  "region": "us-east-1",
  "account_id": "974053642038",
  "compute_optimizer": {"status": "UNAVAILABLE"},
  "cost_optimization_hub": {"status": "NOT_ENROLLED"},
  "deterministic_engine": {"status": "AVAILABLE"},
  "supported_resource_types": ["EC2", "EBS_VOLUME", "LAMBDA_FUNCTION", "RDS_DB_INSTANCE", "ELASTIC_IP", "NAT_GATEWAY", "LOAD_BALANCER"],
  "supported_lookback_days": [7, 30, 60, 90],
  "warnings": []
}
```

### GET /api/aws/optimization/recommendations?region=&days=30

```bash
curl -s 'http://localhost/api/aws/optimization/recommendations?days=30' | jq '. | {status, count}'
```

Returns a deduplicated `List[Recommendation]` plus structured
`OptimizationWarning` rows.  `days` must be one of
`{7, 30, 60, 90}`; anything else yields a sanitized 422.

### GET /api/aws/optimization/summary?region=&days=30

```bash
curl -s 'http://localhost/api/aws/optimization/summary?days=30' | jq
```

```json
{
  "region": "us-east-1",
  "account_id": "974053642038",
  "days": 30,
  "status": "PARTIAL_SUCCESS",
  "total_recommendations": 30,
  "total_estimated_monthly_savings": null,
  "currency": "USD",
  "by_resource_type": [
    {"key": "EBS_VOLUME", "count": 30, "estimated_monthly_savings": null, "currency": "USD"}
  ],
  "by_action": [
    {"key": "REVIEW_DELETE_UNATTACHED_EBS", "count": 30, "estimated_monthly_savings": null, "currency": "USD"}
  ],
  "by_source": [
    {"key": "UNKNOWN", "count": 30, "estimated_monthly_savings": null, "currency": "USD"}
  ],
  "by_confidence": [
    {"key": "HIGH", "count": 30, "estimated_monthly_savings": null, "currency": "USD"}
  ],
  "recommendations_without_savings": 30,
  "warnings": [
    {"source": "compute_optimizer", "code": "InternalError", "message": "...", "region": "us-east-1"}
  ]
}
```

`savings_source=UNKNOWN` rows do NOT contribute to the savings
aggregate — `total_estimated_monthly_savings` is `None` until AWS
itself supplies a figure.

## 10. Read-only guard (Phase 3 extensions)

`assert_read_only` already covers Phase 3 — every Compute Optimizer
and Cost Optimization Hub operation is in the read-only prefix
whitelist (`get_*` / `describe_*` / `list_*`).  The Phase 3 verifier
greps the codebase for the explicit forbidden operations
(`update_enrollment_status`, `update_preferences`,
`put_recommendation_preferences`,
`delete_recommendation_preferences`) to defend against future drift.

Database writes (the Phase 2 `cost_cache`) are not AWS mutations;
they are unaffected by Phase 3.

## 11. Confidence model

* **HIGH** — AWS-native source supplies a savings figure and a
  concrete resource-level recommendation, OR a deterministic rule
  on a resource with no ambiguity (unattached EBS, unused EIP).
* **MEDIUM** — deterministic rule with HIGH or MEDIUM CloudWatch
  coverage (low-utilization EC2, idle NAT, idle LB, RDS
  underutilization).
* **LOW** — AWS-native source with no savings figure (e.g. an
  `OPTIMIZED` Compute Optimizer row that has nothing actionable).

## 12. Limitations and known issues

* **Compute Optimizer and Cost Optimization Hub need IAM permissions.**
  The Phase 1 read-only role does not currently grant
  `compute-optimizer:Get*` or `cost-optimization-hub:List*`.  When
  the permissions are absent the engine surfaces a sanitized warning
  and the deterministic rules continue to function.
* **Single-region only.** Compute Optimizer recommendations and
  Cost Optimization Hub queries are scoped to a single region.  The
  enrollments are account-scoped.
* **No local pricing.** The engine never calculates EBS / NAT / EIP /
  ELB savings locally.  When AWS supplies a figure we surface it;
  when it does not, `savings_source=UNKNOWN` is set explicitly.
* **Recommendation IDs change.** AWS rotates `recommendationId` on
  refresh, so we deliberately DO NOT persist it as a stable
  business key.  The public `recommendation_id` is a SHA-256
  deterministic id computed from `(account, region, resource, action)`.
* **LiteLLM remains in the stack but Phase 3 never calls it.**
  The Phase 3 verifier greps the source tree for AI/LiteLLM symbols
  to defend against accidental drift.

## 13. AWS API cost implications

* **Compute Optimizer.**  Free to read (no API call charges for
  `Get*` / `List*`); the service itself is free at the API level.
  We never call `UpdateEnrollmentStatus` (which can opt into
  enhanced infrastructure metrics that carry a cost).
* **Cost Optimization Hub.**  Free for the AWS-managed console
  recommendations; `ListRecommendations` and
  `ListRecommendationSummaries` are paginated with a small
  `MaxResults` cap (100) so we stay well under any rate limit.
* **CloudWatch.**  Already paid for by Phase 2.  The
  `_fetch_utilization` helper in the route layer reuses the Phase 2
  `batch_query` path so we do not double-bill for the same metric
  data.

## 14. Deferred to later phases

* LiteLLM/AI cost analysis, prompt templates, and answer
  generation — **Phase 4**.
* JWT auth, signup, login, RBAC.
* Recommendation-history persistence beyond the Phase 2 cost cache.
* The professional optimization dashboard — frontend stays
  untouched in Phase 3.
* WebSockets / live updates.
* Terraform / IaC, automated AWS remediation, multi-region fanout.
* SLI / SLO dashboards, audit log of optimization reads.

## 15. Live validation results (this run)

`scripts/phase3_verify.sh` exercises the optimization engine
end-to-end:

* **Phase 0 → Phase 2 regression chain:** all green.
* **Phase 3 services + schemas:** importable; `SavingsSource`
  carries exactly the four allowed members; `CapabilityStatus`
  includes `AVAILABLE`.
* **Phase 3 pytest:** 74 tests pass (17 Compute Optimizer, 17
  Cost Optimization Hub, 30 deterministic rules, 10
  engine / dedup / summary).
* **No write/mutation AWS APIs in Phase 3 source.**  The grep
  explicitly forbids `update_enrollment_status`,
  `update_preferences`, `put_recommendation_preferences`,
  `delete_recommendation_preferences`.
* **No AI / LiteLLM call paths in Phase 3 source.**
* **HTTP endpoints:**
  * `/api/aws/optimization/capabilities` → 200 with
    `compute_optimizer=UNAVAILABLE`,
    `cost_optimization_hub=NOT_ENROLLED`,
    `deterministic_engine=AVAILABLE`.
  * `/api/aws/optimization/recommendations?days=30` → 200 with
    `status=PARTIAL_SUCCESS`, ~30 deterministic rule
    candidates, 2 AWS-source warnings (no IAM permission).
  * `/api/aws/optimization/summary?days=30` → 200 with the same
    deduplicated total.
  * `?days=15` → 422 `InvalidLookbackDays`.
* **Public port controls:** only nginx (port 80) is published.
* **Docker health:** all containers healthy.
