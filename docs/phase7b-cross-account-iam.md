# Phase 7B — Cross-Account IAM Setup

> Operator-facing reference for the IAM resources required to
> connect a second AWS account to AI Cloud Cost Detective in
> Phase 7B.  **The application never modifies AWS IAM
> automatically.**  Every step below is performed manually via
> the AWS console, CLI, or your existing infrastructure-as-code
> pipeline.
>
> All example values below use **placeholder account IDs** only.
> Replace `<MGMT_ACCOUNT_ID>` with the management account (where
> the EC2 instance profile lives) and `<TARGET_ACCOUNT_ID>` with
> the target account you want to register.

## 0. Concepts

```
Management account (<MGMT_ACCOUNT_ID>)
   └── EC2 instance running the backend
         └── Instance role: ai-cloud-cost-detective-management
                ↓ sts:AssumeRole
Target account (<TARGET_ACCOUNT_ID>)
   └── Role: ai-cloud-cost-detective-readonly
              ├── trust policy: allow <MGMT_ACCOUNT_ID>:ai-cloud-cost-detective-management
              └── permission policy: read-only across STS / CE / CW / CO / COH / resources
```

The application never holds the target account's access keys.  It
assumes the role fresh on every cache-miss via `STS AssumeRole`
and discards the temporary credentials immediately.

## 1. Management account — instance profile

The existing Phase 1 / 2 / 3 / 7A instance profile must additionally
be allowed to call `sts:AssumeRole` against the target account's
read-only role.

### 1.1 Trust policy (unchanged)

The EC2 instance role's trust policy continues to allow
`ec2.amazonaws.com` to assume it.  No change is required for
Phase 7B unless the management account has been rebuilt.

### 1.2 Permission policy — append AssumeRole

Append the following statement to the management role's existing
inline / managed policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Phase7BAssumeTargetRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": [
        "arn:aws:iam::<TARGET_ACCOUNT_ID>:role/ai-cloud-cost-detective-readonly"
      ],
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "<operator-chosen-external-id>"
        }
      }
    }
  ]
}
```

* Replace `<TARGET_ACCOUNT_ID>` with the real 12-digit ID.
* Replace `<operator-chosen-external-id>` with a value the
  operator also sets in the target role's trust policy.  When
  the application is configured with an external ID at
  registration time, it must match exactly.
* If the operator prefers to omit the external ID for dev /
  staging environments, drop the `Condition` block.

### 1.3 Why `sts:AssumeRole` only

The application **never** writes to the target account and
**never** requests anything beyond what AWS already supports
read-only.  The only IAM action the management role requires
against the target account is `sts:AssumeRole` against the
specific target role ARN.  Listing it as the only `Action`
prevents an attacker who compromises the management account
from escalating to other target-account roles.

## 2. Target account — read-only role

Create the following role in the target account:

* **Role name**: `ai-cloud-cost-detective-readonly`
* **Trusted entity type**: AWS account
* **Trusted account ID**: `<MGMT_ACCOUNT_ID>`
* **Require external ID**: yes, set to `<operator-chosen-external-id>`
  (must match the management account's `Condition` block above)
* **Require MFA**: no (this is a backend service role, not a
  human)

### 2.1 Trust policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::<MGMT_ACCOUNT_ID>:role/ai-cloud-cost-detective-management"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "<operator-chosen-external-id>"
        }
      }
    }
  ]
}
```

### 2.2 Permission policy — read-only

The target role's permission policy must grant the **same** set of
read-only actions Phase 7A already requires from the management
account.  Do **not** add mutation actions.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Phase7BIdentity",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Phase7BResourceDiscovery",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "ec2:DescribeNatGateways",
        "ec2:DescribeRegions",
        "elasticloadbalancing:DescribeLoadBalancers",
        "rds:DescribeDBInstances",
        "lambda:ListFunctions",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "tag:GetResources",
        "resource-explorer-2:Search"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Phase7BCostExplorer",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Phase7BCloudWatch",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Phase7BComputeOptimizer",
      "Effect": "Allow",
      "Action": [
        "compute-optimizer:GetEnrollmentStatus",
        "compute-optimizer:GetEC2InstanceRecommendations",
        "compute-optimizer:GetEBSVolumeRecommendations",
        "compute-optimizer:GetLambdaFunctionRecommendations",
        "compute-optimizer:GetRDSDatabaseRecommendations",
        "compute-optimizer:GetRecommendationSummaries"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Phase7BCostOptimizationHub",
      "Effect": "Allow",
      "Action": [
        "cost-optimization-hub:GetEnrollmentStatus",
        "cost-optimization-hub:ListEnrollmentStatuses",
        "cost-optimization-hub:GetRecommendation",
        "cost-optimization-hub:ListRecommendations",
        "cost-optimization-hub:ListRecommendationSummaries"
      ],
      "Resource": "*"
    }
  ]
}
```

### 2.3 Forbidden actions (must NOT be present)

The following actions are **forbidden** in the target role's
permission policy.  The application never needs them, and their
presence indicates a misconfiguration:

```text
iam:* (except iam:Get* / iam:List* read-only)
organizations:*
account:*
ce:UpdatePreferences
ce:UpdateCostAllocationTagsStatus
compute-optimizer:UpdateEnrollmentStatus
cost-optimization-hub:UpdateEnrollmentStatus
cost-optimization-hub:UpdatePreferences
cost-optimization-hub:PutRecommendationPreferences
cost-optimization-hub:DeleteRecommendationPreferences
ec2:RunInstances
ec2:TerminateInstances
ec2:DeleteVolume
ec2:ReleaseAddress
ec2:DeleteNatGateway
ec2:DeleteNetworkInterface
rds:DeleteDBInstance
rds:StopDBInstance
lambda:DeleteFunction
s3:DeleteBucket
s3:PutBucketPolicy
```

In particular, **`UpdateEnrollmentStatus`,
`UpdatePreferences`, `PutRecommendationPreferences`,
`DeleteRecommendationPreferences`** must not be granted to this
role.  Compute Optimizer and Cost Optimization Hub enrollment is
intentionally manual — the platform never enrolls an account
automatically.  If you want to enable either capability in the
target account, do it via the AWS console or your existing
infrastructure-as-code pipeline.

## 3. Region requirements

* Cost Explorer is a single global endpoint per account — no
  region selection needed for CE calls.
* Compute Optimizer is also a single account-level API.
* Resource discovery (EC2, EBS, ELBv2, RDS, Lambda, NAT
  Gateway) is region-scoped.  The application calls these in
  the per-account `preferred_region` (if registered) or
  `AWS_DEFAULT_REGION` (otherwise).
* If the target account uses a non-default region (e.g.
  `eu-west-1`) for its primary workloads, set
  `preferred_region` when registering the account.  Otherwise
  the platform will fall back to `AWS_DEFAULT_REGION` and may
  return an empty inventory if the target account has no
  resources in that region.

## 4. Multi-account fan-out caveats

* The application issues one AssumeRole per cache miss per
  account.  In the steady state this is rare — credentials are
  cached in process memory for the requested `DurationSeconds`
  window (default 1 h).
* `STS:GetCallerIdentity` is rate-limited at 100 TPS per
  account by default.  The platform never exceeds that ceiling
  because the cost aggregator uses bounded concurrency
  (`PHASE7B_COST_MAX_CONCURRENCY=4` by default).
* `Cost Explorer` is rate-limited per account.  The cache
  layer (6 h TTL by default) keeps the call rate low.  If the
  operator needs a shorter TTL, set `COST_CACHE_TTL_SECONDS`
  — but be aware that each fresh fetch costs a CE API call
  per account.

## 5. Operator-side checklist (real second account)

| # | Step | Verification |
|---|------|--------------|
| 1 | Create the target role with the trust policy above. | `aws iam get-role --role-name ai-cloud-cost-detective-readonly --profile <target>` |
| 2 | Attach the permission policy above. | `aws iam list-attached-role-policies --role-name ai-cloud-cost-detective-readonly --profile <target>` |
| 3 | Capture the role ARN. | `arn:aws:iam::<TARGET_ACCOUNT_ID>:role/ai-cloud-cost-detective-readonly` |
| 4 | Verify the management role can assume it. | `aws sts assume-role --role-arn <arn> --role-session-name cd-verify --external-id <id> --profile <mgmt>` (must return temporary credentials) |
| 5 | Log in to the dashboard as ADMIN. | Open `/accounts`. |
| 6 | Click **Register an account**. | Form accepts the role ARN. |
| 7 | Click **Validate**. | Returns `CONNECTED`. |
| 8 | Switch the selector to the new account. | `/api/aws/identity` returns the target account ID. |
| 9 | Open the Costs page. | Returns the target account's CE data only. |
| 10 | Open Resources. | Returns the target account's resource inventory. |
| 11 | Open Optimization. | Returns the target account's CO / COH state. |
| 12 | Switch back to **All AWS Accounts**. | Returns a deterministic sum. |
| 13 | Disable the new account via the dashboard. | Status flips to `DISABLED` and the rollup no longer includes it. |
| 14 | Re-enable the new account. | Status flips back to `CONNECTED` and the rollup includes it again. |

When every step above passes, Phase 7B is ready for the
`phase-7b-complete` closure tag.

## 6. Security properties

* The application **never** writes the target account's role
  ARN to its logs in full — only the account-id segment of the
  ARN is logged (`arn:aws:iam::<id>:role/<redacted>`).
* The application **never** writes the External ID to its logs.
* The application **never** returns the External ID through any
  HTTP response.
* The application **never** persists STS temporary credentials
  to the database, the log stream, the response, or any HTTP
  header.
* Restarting the backend discards every cached STS credential.
* `RoleSessionName` is randomised so two simultaneous
  containers in the same management account do not overwrite
  each other's CloudTrail records.

## 7. Example Terraform (optional)

```hcl
# Target account: <TARGET_ACCOUNT_ID>
resource "aws_iam_role" "cost_detective_readonly" {
  name = "ai-cloud-cost-detective-readonly"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        AWS = "arn:aws:iam::<MGMT_ACCOUNT_ID>:role/ai-cloud-cost-detective-management"
      }
      Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "sts:ExternalId" = "<operator-chosen-external-id>" }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cost_detective_readonly_inline" {
  role       = aws_iam_role.cost_detective_readonly.name
  policy_arn = aws_iam_policy.cost_detective_readonly_inline.arn
}

resource "aws_iam_policy" "cost_detective_readonly_inline" {
  name        = "ai-cloud-cost-detective-readonly-inline"
  description = "Phase 7B read-only permissions for Cost Detective"
  policy      = file("${path.module}/policies/cost_detective_readonly.json")
}
```

```hcl
# Management account: <MGMT_ACCOUNT_ID>
resource "aws_iam_role_policy_attachment" "mgmt_assume_target" {
  role       = "ai-cloud-cost-detective-management"
  policy_arn = aws_iam_policy.mgmt_assume_target.arn
}

resource "aws_iam_policy" "mgmt_assume_target" {
  name        = "ai-cloud-cost-detective-assume-target"
  description = "Allow management role to assume the target read-only role"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "Phase7BAssumeTargetRole"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = "arn:aws:iam::<TARGET_ACCOUNT_ID>:role/ai-cloud-cost-detective-readonly"
      Condition = {
        StringEquals = { "sts:ExternalId" = "<operator-chosen-external-id>" }
      }
    }]
  })
}
```
