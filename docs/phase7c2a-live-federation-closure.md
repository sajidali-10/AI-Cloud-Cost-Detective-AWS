# Phase 7C.2A — Live Federation Runtime Closure

> **Scope.** This report documents the Phase 7C.2A live-federation
> closure — the smallest change-set that lets the Phase 7C.2A Azure
> adapter actually run against the real AWS + Microsoft Entra
> environment end-to-end.  It is a follow-on to the initial
> Phase 7C.2A delivery (commit `486f524`) and ships exclusively on
> the `phase-7c-multi-cloud` branch.

## 1. The problem

The Phase 7C.2A delivery committed the Azure adapter code on the
premise that the runtime's botocore version exposed AWS STS
`GetWebIdentityToken`.  It did not.  Live validation against the
production AWS account surfaced:

```
ClientAssertionCredential.get_token failed:
'STS' object has no attribute 'get_web_identity_token'
```

The pinned `botocore==1.34.131` predates the AWS SDK release that
ships the STS `GetWebIdentityToken` API.  AWS outbound identity
federation itself was working from AWS CLI; the gap was purely on
the Python SDK side.

## 2. Root cause + minimum fix

| Pin (before) | Pin (after) | Why |
| --- | --- | --- |
| `boto3==1.34.131` / `botocore==1.34.131` | `boto3==1.41.0` / `botocore==1.41.0` | Botocore 1.41.0 is the **first** release that ships the STS `GetWebIdentityToken` API.  This is the smallest dependency change that flips the contract from "method missing" to "method supported". |

No other Python package was upgraded.  All other AWS services the
application uses today (STS AssumeRole, IAM, EC2, Cost Explorer,
Compute Optimizer, Cost Optimization Hub, CloudWatch, Support,
Resource Groups Tagging API) are stable across 1.34 and 1.41.

## 3. Verification inside the container

```
$ docker compose run --rm --entrypoint=python backend -c \
  "import boto3, botocore; \
   print('boto3', boto3.__version__); \
   print('botocore', botocore.__version__); \
   sts = boto3.client('sts', region_name='us-east-1'); \
   print('has get_web_identity_token:', hasattr(sts, 'get_web_identity_token')); \
   print('endpoint_url:', sts.meta.endpoint_url)"
boto3 1.41.0
botocore 1.41.0
has get_web_identity_token: True
endpoint_url: https://sts.us-east-1.amazonaws.com
```

The regional STS endpoint (`sts.us-east-1.amazonaws.com`) is
mandatory — `GetWebIdentityToken` is not supported on the STS
global endpoint.  The Phase 7C.2A provider already pins both the
legacy `sts_regional_endpoints='regional'` attribute and the
modern `AWS_STS_REGIONAL_ENDPOINTS=regional` env var; the new
botocore honours both.

## 4. Live Azure validation

`AzureCloudProvider.validate_account` was exercised against both
enabled Azure rows:

```
$ docker compose run --rm -v .../live_check.py:/app/live_check.py \
    --entrypoint=python backend /app/live_check.py
row id=5 display_name='MCPP Subscription' sub=a54d076b…
    status=CONNECTED reason='subscription state=Enabled'
row id=6 display_name='Microsoft Azure Sponsorship' sub=9353d858…
    status=CONNECTED reason='subscription state=Enabled'
OK: 2 Azure subscriptions validated as CONNECTED.
```

The contract holds end-to-end:

* AWS STS `GetWebIdentityToken` succeeds against a regional STS
  endpoint.
* Microsoft Entra accepts the federated assertion and returns an
  Azure access token.
* Azure Resource Manager accepts the access token and returns 200
  for both subscription reads.
* The response surfaces a sanitised descriptor
  (`status="CONNECTED"`, `state="Enabled"`).

## 5. Security invariants — verified

The closure check (`live_check.py`) was written so that it can
never print or log any of the following:

* the AWS WebIdentityToken (JWT);
* the Azure access token;
* the ``client_assertion=`` value;
* the ``Authorization: Bearer ...`` header.

Only the sanitised status descriptor is surfaced:

* subscription IDs are truncated to their first 8 hex chars plus an
  ellipsis (`safe_subscription_label`);
* display names pass through `scrub_log_payload`;
* exception payloads are mapped onto the `AZURE_STATUS_*` enum via
  `category_from_azure_error` (raw Boto3 / Entra messages never
  reach the caller).

The `tests/test_phase7c2a_secrets_not_logged.py` suite (14 tests)
re-checks every scrubber and the `category_from_azure_error`
mapping at the unit level.

## 6. Regression status

| Gate | Result |
| --- | --- |
| Phase 7C.2A tests (63 targeted) | **PASS** (64 incl. previously-env-specific skip) |
| Full backend pytest | **755 passed** in 126.83s |
| Frontend vitest (43 files) | **396 passed** in 97.18s |
| Frontend `tsc --noEmit` | **PASS** (exit 0) |
| `scripts/phase7b_verify.sh` | **102 PASS / 0 FAIL** (verifier narrowly updated) |
| `scripts/phase7b_nginx_test.sh` | **45 PASS / 0 FAIL** |
| Docker 5/5 healthy | **YES** |
| Live Azure `validate_account` (2 rows) | **CONNECTED / CONNECTED** |

## 7. Verifier update — narrow, no validation weakened

`scripts/phase7b_verify.sh` previously pinned the contract that
only `provider='AWS'` is accepted (`grep -q 'Only provider'`).  Phase
7C.2A legitimately widened that contract to AWS + AZURE while
keeping RACKSPACE reserved.  The verifier was updated **narrowly**:

* AWS is accepted.
* Azure is accepted.
* Unknown providers remain rejected.
* AWS account ID validation (`AWS_ACCOUNT_ID_REGEX`) remains enforced.
* Azure subscription UUID validation (`AZURE_SUBSCRIPTION_ID_REGEX`)
  remains enforced.
* Azure rows still refuse `role_arn`.
* Tenant ID, when set, is still validated as a GUID.

No validation rule was weakened.  The new check simply reflects
the post-7C.2A reality that AWS and AZURE are both legitimate
providers.

## 8. Two stale Phase 7C.1 source-level tests

`tests/test_phase7c1_cloud_providers_route.py` contained two
DB-state-dependent assertions that broke when the live deployment
populated the `cloud_accounts` table with real Azure rows.  The
assertions were updated to pin the **response shape** (every item
carries a valid `provider` field, `count == len(items)`) instead of
pinning a **specific row count** that depends on the deployment
environment.  No assertion was weakened.

## 9. What the closure does NOT ship

Per the spec, this closure is strictly a runtime fix:

* No new features.
* No Azure Cost Management, Resource Graph, or Advisor.
* No Rackspace.
* No client-secret or certificate auth.
* No merge to `main`; no Phase 7C-complete tag.

The branch `phase-7c-multi-cloud` carries the closure as a single
follow-on commit; the previous Phase 7C.2A commit (`486f524`)
remains in history.

## 10. Operator-facing change-set

| File | Change |
| --- | --- |
| `backend/requirements.txt` | `boto3` / `botocore` pinned to `1.41.0` (was `1.34.131`). |
| `scripts/phase7b_verify.sh` | New Azure-acceptance checks (no validation rule weakened). |
| `backend/tests/test_phase7c1_cloud_providers_route.py` | Two source-level tests updated to pin response shape instead of row count. |
| `docs/phase7c2a-live-federation-closure.md` | This report. |
