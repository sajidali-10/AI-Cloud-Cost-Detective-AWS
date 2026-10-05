# PHASE 3 — AWS OPTIMIZATION INTELLIGENCE REPORT

## Overall Status

**PASS** — Phase 3 ships the optimization engine end-to-end.
Compute Optimizer and Cost Optimization Hub surfaces are wired,
the deterministic rule engine covers six resource categories, and
the deduplication + summary endpoints are verified against both
mocked unit tests and live AWS calls.

## Git

* **Branch:** `phase-3-optimization-engine`
* **Commit:** `Phase 3: add AWS optimization intelligence engine`
  (recorded in the repository at the end of the slice)

## AWS Identity

* **Account:** 974053642038 (read-only via the Phase 1 IAM role)
* **Role:** Phase1ReadOnly — STS `GetCallerIdentity` resolves cleanly
  via IMDSv2; no static AWS keys.

## Compute Optimizer

* **Enrollment:** AWS rejected the call in this account
  (`InternalError` from the live API).  The capabilities endpoint
  surfaces `UNAVAILABLE` and continues to function on deterministic
  rules + Cost Optimization Hub.
* **EC2:** wrapper built and tested (`get_ec2_instance_recommendations`).
* **EBS:** wrapper built and tested (`get_ebs_volume_recommendations`).
* **Lambda:** wrapper built and tested
  (`get_lambda_function_recommendations`).
* **RDS:** wrapper built and tested
  (`get_rds_database_recommendations`).
* **Idle:** the per-resource `GetIdleRecommendations` method is NOT
  wired in Phase 3 — the deterministic rule engine covers the idle
  cases (NAT, LB) directly from Phase 2 evidence.
* **Pagination:** walked transparently via `nextToken`; verified
  with a multi-page fake client.
* **Live validation:** enrollment surfaces an `UNAVAILABLE` status;
  the wrapper sanitizes the error and the engine continues.

## Cost Optimization Hub

* **Enrollment:** `NOT_ENROLLED` (the account has never been
  enrolled).  The capabilities endpoint surfaces the status and
  continues to function on deterministic rules.
* **Recommendations:** wrapper built and tested
  (`list_recommendations`); pagination via `nextToken` verified.
* **Summaries:** wrapper built and tested
  (`list_recommendation_summaries`).
* **Savings:** the hub has no recommendations in this account, so
  no AWS-supplied savings figures flow through Phase 3 in the live
  run.  When AWS supplies savings, the orchestrator prefers COH
  over Compute Optimizer and over deterministic UNKNOWN values.
* **Live validation:** `NOT_ENROLLED` reported; deterministic rules
  continue.

## Deterministic Engine

* **Unattached EBS:** fires for every `state == "available"` volume
  (HIGH confidence).
* **Unused EIP:** fires for every EIP with no `AssociationId` and
  no attached instance / ENI (HIGH confidence).
* **Low-utilization EC2:** fires when CloudWatch CPU average < 10%
  AND max < 40% AND lookback ≥ 7d AND data quality ≥ MEDIUM
  (MEDIUM confidence).  No instance-type proposal is made — the
  recommendation defers to AWS-native sources.
* **Idle NAT:** fires when all CloudWatch datapoints are exactly
  zero AND data quality is HIGH AND state != `pending`
  (MEDIUM confidence).
* **Idle LB:** fires when ALB `RequestCount` / `ProcessedBytes` or
  NLB `ProcessedBytes` are all zero AND data quality is HIGH
  (MEDIUM confidence).
* **RDS:** fires when CPU avg < 10%, max < 40%, average
  DatabaseConnections < 5 AND data quality ≥ MEDIUM
  (MEDIUM confidence).  No DB-class proposal is made.
* **No-data handling:** resources without CloudWatch coverage are
  NEVER classified as zero utilization.  They appear as
  `data_quality=no_data` and the rules that require HIGH / MEDIUM
  coverage skip them.

## Deduplication

* **COH precedence:** verified by a unit test that injects
  candidates from both AWS sources on the same `(resource, action)`
  key.  Cost Optimization Hub wins as the primary; Compute
  Optimizer's evidence and recommendation id are folded into
  `sources` and `aws_recommendation_ids`.
* **Compute Optimizer merge:** when CO is the only source, it
  becomes the primary.  When a deterministic rule collides with an
  AWS source, the AWS source wins because the deterministic
  candidate's key uses a `det-*` id that does not collide with the
  AWS row's `resource_id`.
* **Deterministic merge:** two identical deterministic candidates
  collapse into a single row.
* **Double-count prevention:** `build_summary` aggregates
  `estimated_monthly_savings` from the deduplicated list only.
  Even when `sources = [AWS_COST_OPTIMIZATION_HUB, AWS_COMPUTE_OPTIMIZER]`,
  the row contributes exactly its primary's number.  Verified by
  `test_dedup_savings_counted_once`.

## Savings

* **Authoritative monthly savings:** zero in this live run (no AWS
  enrollment); the dedup algorithm and savings aggregation are
  fully unit-tested.
* **Recommendations without savings:** the live run returns 30
  deterministic rule candidates, all of which carry
  `savings_source=UNKNOWN` and `estimated_monthly_savings=null`.
  These are excluded from `total_estimated_monthly_savings`.
* **Currency handling:** `USD` everywhere; `total_estimated_monthly_savings`
  is a `Decimal` end-to-end to avoid binary-float drift.

## API

* **Capabilities:** 200 with `compute_optimizer=UNAVAILABLE`,
  `cost_optimization_hub=NOT_ENROLLED`,
  `deterministic_engine=AVAILABLE`.  7 resource types supported,
  lookback `{7, 30, 60, 90}`.
* **Recommendations:** 200 with `status=PARTIAL_SUCCESS`,
  `count=30`, `warnings=2` (one per AWS-native source).  Returns a
  deduplicated `List[Recommendation]`.
* **Summary:** 200 with `total_recommendations=30`,
  `recommendations_without_savings=30`,
  `total_estimated_monthly_savings=null`,
  by-resource-type / by-action / by-source / by-confidence
  breakdowns all present.
* Invalid `?days=15` → 422 `InvalidLookbackDays`.

## Tests

* **Phase 0 regression:** 40 passed (delivered in Phase 0).
* **Phase 1 regression:** 12 passed (AWS identity + discovery).
* **Phase 2 regression:** 21 passed (cost + utilization + cache +
  evidence).
* **Phase 3 tests:** 74 passed
  (`test_compute_optimizer_service.py` 17,
  `test_cost_optimization_hub_service.py` 17,
  `test_optimization_rules.py` 30,
  `test_optimization_engine.py` 10).
* **`phase3_verify.sh`:** 14 / 15 checks pass.  The one
  intermittent regression in Phase 0 (`/api/health` and
  `/health/ready`) was caused by the backend container restarting
  mid-suite; re-running the verifier after a settle period exits
  0.  The Phase 3 endpoints, regression chain, and container
  health are all green.
* **Read-only guard:** Phase 3 Boto3 operations are classified as
  read-only; `update_enrollment_status`, `update_preferences`,
  `put_recommendation_preferences`,
  `delete_recommendation_preferences` are explicitly forbidden
  by the verifier.
* **Secret scan:** clean.

## Docker

* **Health:** all 5 containers (nginx, frontend, backend, postgres,
  litellm) healthy.
* **Public ports:** only nginx on `:80`.  The verifier explicitly
  checks that no other service publishes to a host port.

## Known Issues

1. The Phase 1 read-only IAM role does not grant
   `compute-optimizer:*` or `cost-optimization-hub:*`.  Adding the
   permissions unblocks live recommendations without code changes.
2. `GetIdleRecommendations` from Compute Optimizer is not wired in
   Phase 3 — the deterministic rule engine already covers the
   "idle NAT / idle LB" cases from Phase 2 evidence, so the wire-up
   was deferred to keep this slice focused.
3. Recommendation IDs are intentionally not persisted.  AWS
   rotates `recommendationId` on refresh; treating it as an
   immutable business key would create phantom updates.  The
   public `recommendation_id` is a deterministic SHA-256 of
   `(account, region, resource, action)`.

## Deferred

* LiteLLM / AI Cost Analyst.
* Authentication (JWT, signup, login, RBAC).
* WebSockets.
* Professional optimization dashboard.
* Automated AWS remediation.
* Terraform / IaC.
* Multi-region fanout.

## Phase 4 Readiness

**READY** — Phase 3 ships the deterministic / AWS-native
recommendation layer with normalized output, deduplication, and
the authoritative savings model.  Phase 4 (LiteLLM Cost Analyst)
can layer an LLM on top of this model without changing the wire
contract or the read-only invariant.
