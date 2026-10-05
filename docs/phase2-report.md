# Phase 2 — Cost & Utilization Intelligence Report

> **Status:** Phase 2 closure — Cost Explorer + CloudWatch utilization + read-through cache + evidence layer, **all 21 closure checks PASS**, **144 unit tests PASS** (22 in `test_cost_explorer_service.py`, including 8 new closure-fix regression tests), **live Cost Explorer validated against the production EC2 instance profile**.

---

## 1. AWS Identity and IAM Role

| Field            | Value                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------- |
| `account`        | `974053642038`                                                                              |
| `arn`            | `arn:aws:sts::974053642038:assumed-role/AICloudCostDetectivePhase1ReadOnly/i-06f1c27f252e927b1` |
| `role`           | `AICloudCostDetectivePhase1ReadOnly` (EC2 instance profile / assumed role)                  |
| `instance`       | `i-06f1c27f252e927b1`                                                                       |
| `region` (used)  | `us-east-1`                                                                                 |
| `AWS_DEFAULT_REGION` (settings) | `us-east-1`                                                                  |

Credentials resolved via the **Boto3 default chain** → EC2 instance
profile → `AICloudCostDetectivePhase1ReadOnly` role. No static
`AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` is set in the backend
container, `Settings`, or `.env.example`.

The same IAM role grants `ce:GetCostAndUsage` (verified below) and the
Phase 1 read-only inventory APIs.

---

## 2. Closure Fix — Phase 2 Cost Explorer Patch

Before this closure fix, `aggregate_cost_report()` failed on its first
request against live AWS even though direct `boto3.client("ce").
get_cost_and_usage(...)` succeeded. Two verified issues were the
cause.

### Issue 1 — `GetCostAndUsage` does not accept `MaxResults`

`GetCostAndUsage` accepts `TimePeriod`, `Granularity`, `Filter`,
`GroupBy`, `Metrics`, `BillingViewArn`, and `NextPageToken`. It does
**not** accept `MaxResults`. Boto3's `ParamValidator` rejects the
unknown parameter with a `ValidationException` before the request
ever leaves the host.

The previous code sent `"MaxResults": CE_PAGE_SIZE` on every call
(current total, previous total, daily, service, region) — five
points of failure, all firing on the first request.

**Fix.** Removed `MaxResults` from every `get_cost_and_usage` request.
Pagination is now driven exclusively by `NextPageToken`. The
`CE_PAGE_SIZE` constant is retained as a no-op sentinel for
backward-compatible imports; it is never sent on the wire.

### Issue 2 — `get_total_and_previous` ignored `ResultsByTime[*].Total`

`get_total_and_previous()` issues *ungrouped* `GetCostAndUsage`
requests (`Metrics=["UnblendedCost"]`, no `GroupBy`). On an ungrouped
response every `ResultsByTime` block has `Groups == []` and a single
`Total` field that already reflects the whole account for that
period.

The previous `_sum_unblended_cost()` helper iterated `Groups` first
(always empty on an ungrouped response) and then fell through to
`Total`. Because the `Groups` branch produced zero, the entire
account total silently collapsed to `Decimal("0.00")`.

**Fix.** `_sum_unblended_cost()` now reads
`ResultsByTime[*].Total[UnblendedCost].Amount` directly. The grouped
retrievers (`get_by_service`, `get_by_region`) continue to use
`_aggregate_grouped`, which is the only helper that should ever
touch `Groups`.

### Regression tests added (`backend/tests/test_cost_explorer_service.py`)

Eight new tests under `TestClosureRegression` cover both issues and
the unchanged behavior the fix must preserve:

| Test                                                                                              | What it asserts                                                                       |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `test_get_total_and_previous_never_sends_maxresults`                                              | neither current nor previous request includes `MaxResults`                            |
| `test_get_daily_trend_never_sends_maxresults`                                                      | daily request omits `MaxResults`                                                      |
| `test_get_by_service_never_sends_maxresults`                                                      | service request omits `MaxResults`                                                    |
| `test_get_by_region_never_sends_maxresults`                                                       | region request omits `MaxResults`                                                     |
| `test_ungrouped_response_produces_nonzero_current_and_previous`                                   | non-zero `Total[UnblendedCost].Amount` → non-zero current & previous totals           |
| `test_next_page_token_pagination_still_works`                                                     | three pages with `NextPageToken` walked end-to-end, no `MaxResults` on any page        |
| `test_daily_trend_remains_correct_after_fix`                                                      | DAILY granularity still produces one row per day, Decimal precision, USD unit         |
| `test_by_service_remains_correct_after_fix`                                                       | SERVICE grouping still aggregates across period blocks                                |
| `test_by_region_remains_correct_after_fix`                                                        | REGION grouping preserves `no_region` key, sorted descending                          |

All **22** tests in `test_cost_explorer_service.py` pass; the full
backend suite is **144/144 PASS** with no regressions.

---

## 3. Live Cost Explorer Validation — IAM Works

Direct `boto3.client("ce").get_cost_and_usage(...)` already worked
under the EC2 IAM role. After the closure fix, the same data flows
through `aggregate_cost_report()` and the HTTP layer.

### 3.1 In-process `aggregate_cost_report(client, 7)`

Run inside the backend container, using the EC2 instance profile:

```text
Caller: arn=arn:aws:sts::974053642038:assumed-role/AICloudCostDetectivePhase1ReadOnly/i-06f1c27f252e927b1 account=974053642038

=== aggregate_cost_report(client, 7) ===
period:        2026-09-28 -> 2026-10-05 (7d)
previous:      2026-09-21 -> 2026-09-28
current_total: 629.88
previous_total:555.11
unit:          USD
daily points:  7
  first: (datetime.date(2026, 9, 28), Decimal('79.56'), 'USD')
  last:  (datetime.date(2026, 10, 4), Decimal('71.90'), 'USD')
by_service:    19 rows
  ('EC2 - Other', Decimal('249.31'), 'USD')
  ('Amazon Elastic Compute Cloud - Compute', Decimal('163.32'), 'USD')
  ('AWS Support (Business)', Decimal('88.00'), 'USD')
  ('Amazon Relational Database Service', Decimal('72.28'), 'USD')
  ('Amazon Q', Decimal('21.30'), 'USD')
by_region:     19 rows
  ('us-east-1', Decimal('608.54'), 'USD')
  ('NoRegion', Decimal('14.11'), 'USD')
  ('global', Decimal('6.41'), 'USD')
  ('us-west-1', Decimal('0.82'), 'USD')
  ('ap-northeast-1', Decimal('0.00'), 'USD')
```

### 3.2 Live HTTP — `GET /api/aws/costs?days=7|30|60|90`

All four allowed windows returned 200 with real, non-zero totals:

| `days` | `total_cost` | `previous_period_cost` | `daily_trend` | `by_service` | `by_region` | `cache_status` |
| ------ | ------------ | ---------------------- | ------------- | ------------ | ----------- | -------------- |
| `7`    | `629.88`     | `555.11`               | 7 points      | 19 rows      | 19 rows     | `REFRESHED`    |
| `30`   | `2449.93`    | `2258.84`              | 30 points     | 19 rows      | 19 rows     | `HIT`          |
| `60`   | `4708.76`    | `2537.80`              | 60 points     | 25 rows      | 19 rows     | `REFRESHED`    |
| `90`   | `5227.96`    | `7377.86`              | 90 points     | 27 rows      | 19 rows     | `REFRESHED`    |

Invalid `days=15` returns HTTP **422** with `InvalidLookbackDays` —
never a 500.

### 3.3 Live HTTP — `POST /api/aws/evidence`

```text
region:    us-east-1
days:      7
status:    PARTIAL_SUCCESS
cost_summary.amount:    629.90
cost_summary.by_service:19 rows
  {'service': 'EC2 - Other', 'amount': '249.31', 'unit': 'USD'}
  {'service': 'Amazon Elastic Compute Cloud - Compute', 'amount': '163.32', 'unit': 'USD'}
  {'service': 'AWS Support (Business)', 'amount': '88.00', 'unit': 'USD'}
  {'service': 'Amazon Relational Database Service', 'amount': '72.28', 'unit': 'USD'}
  {'service': 'Amazon Q', 'amount': '21.30', 'unit': 'USD'}
resources: 140 items
warnings:  1 (cloudwatch)
```

`status="PARTIAL_SUCCESS"` is the documented Phase 2 outcome when
CloudWatch utilization is unavailable (pre-existing condition,
unrelated to this closure fix); the cost-summary side is fully
populated.

---

## 4. Capability Status

| Capability                              | Status     | Notes                                                                                  |
| --------------------------------------- | ---------- | -------------------------------------------------------------------------------------- |
| `aggregate_cost_report(client, 7)`      | LIVE       | Real CE call; non-zero current & previous totals.                                      |
| `GET /api/aws/costs?days=7|30|60|90`    | LIVE       | All four windows return 200 with non-zero totals, daily trend, by_service, by_region.  |
| `GET /api/aws/costs?days=15`            | LIVE 422   | Sanitized `InvalidLookbackDays`; never 500.                                            |
| `POST /api/aws/evidence`                | LIVE       | Cost side fully populated; `PARTIAL_SUCCESS` only because of pre-existing CW gap.     |
| Cost cache (TTL + GC)                   | LIVE       | Postgres-backed, JSONB, `cache_status` flipped `REFRESHED` → `HIT` on subsequent calls.|
| Read-only guard                         | ENFORCED   | Every `client.<op>()` call site wrapped by `assert_read_only`.                         |
| No write/mutation AWS APIs              | ENFORCED   | `phase2_verify.sh` scan: zero mutation prefixes in Phase 2 source.                     |
| Secret scanner                          | CLEAN      | No `AKIA*`, no `AWS_SECRET_ACCESS_KEY=*`, no AI provider keys, no `LITELLM_MASTER_KEY=sk-…`. |
| Single-region enforcement               | ENFORCED   | No `describe_regions`, no fanout, no multi-region code.                                |
| Public port surface unchanged           | YES        | Only Nginx on host port 80.                                                           |

---

## 5. Verification Summary

`scripts/phase2_verify.sh`: **21/21 PASS** (delegates to
`phase1_verify.sh` which itself is **14/14 PASS**).

`docker compose exec backend python -m pytest -p no:cacheprovider`:
**144 passed in 1.90s**.

Targeted `test_cost_explorer_service.py`: **22/22 PASS** (14 original
+ 8 new closure-fix regression tests).

PytestCacheWarning check: zero warnings emitted with the default
`cacheprovider` active.

---

## 6. Out of Scope (deferred)

Compute Optimizer, Cost Optimization Hub, AI / LiteLLM analysis of
cost data, JWT auth, signup/login, report-history persistence, the
production dashboard, WebSockets, Terraform/IaC, automated AWS
remediation, multi-region fanout. These belong to later phases and
are explicitly not touched by this closure fix.
