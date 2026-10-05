# Cost Controls — Phase 0

The Phase 0 platform is intentionally designed to be the cheapest viable
foundation that is still secure, reproducible, and production-shaped.

## What Phase 0 avoids

Phase 0 explicitly does **not** create or provision any of the following
managed or paid services:

- Amazon RDS (PostgreSQL or otherwise)
- Amazon Aurora
- Amazon ElastiCache (Redis, Memcached)
- Amazon OpenSearch
- NAT Gateway
- Application Load Balancer (ALB)
- Network Load Balancer (NLB)
- Amazon ECS / EKS / Fargate
- Amazon ECR (beyond what's free-tier)
- AWS Config
- Amazon Kinesis
- AWS Step Functions
- Amazon SageMaker endpoints
- Any dedicated LiteLLM EC2 instance
- Paid AI API calls (OpenAI, Anthropic, Google Gemini, AWS Bedrock, OpenRouter)
- Terraform / CloudFormation / Pulumi (none of these are used)

The application PostgreSQL and LiteLLM run inside Docker containers on the
existing Ubuntu host. That host is the **only** compute required for the
development environment.

## What Phase 0 does use

| Component                 | Cost impact                                            |
| ------------------------- | ------------------------------------------------------ |
| Existing Ubuntu host      | Already paid for; Phase 0 adds no new compute          |
| Docker Engine + Compose   | Free and open-source                                   |
| PostgreSQL container      | Free; runs on existing host                            |
| LiteLLM Gateway container | Free; runs on existing host                            |
| Nginx container           | Free; runs on existing host                            |
| Backend container         | Free; runs on existing host                            |
| Frontend container        | Free; runs on existing host                            |

The only outbound network calls in Phase 0 are:

- `docker compose pull` to fetch pinned images from Docker Hub and
  `ghcr.io/berriai/litellm` (free).
- `npm install` and `pip install` (free public registries).
- `openssl` (local randomness — no network).

There are **no** API calls to paid AI providers in Phase 0.

## Stopping the environment

Stopping the application (`make down`) removes the Docker containers but
**does not** automatically zero AWS charges.

- The Ubuntu host itself (if it is an EC2 instance) continues to incur
  compute charges while running.
- Any EC2-attached EBS volumes continue to incur storage charges while the
  volume exists, regardless of container state.
- Elastic IP addresses, if any, continue to incur charges while associated.
- Any AWS account-level resources that exist independently of this project
  continue to incur their own charges.

To stop **all** AWS charges associated with this project, you must stop or
terminate the underlying EC2 instance and release any associated Elastic IPs
and EBS volumes via the AWS console or CLI. The application cannot do this
for you.

> **Important**: stopping the application does not mean "no AWS charges".
> See `docs/phase0-report.md` for the Phase 0 environment summary recorded
> at completion.

## Future cost discipline

Phase 1 and later phases will:

- Use read-only IAM roles (preferably an EC2 instance profile).
- Never configure paid AI providers in production unless explicitly enabled.
- Cap LiteLLM per-request spend in `litellm/config.yaml` once provider
  models are introduced.
- Surface per-account daily/weekly spend summaries in the application UI.

## Phase 3 additions

* **Compute Optimizer.** Free at the API level for `Get*` / `List*`
  calls.  We never call `UpdateEnrollmentStatus`, which can opt the
  account into enhanced infrastructure metrics (a paid feature).
* **Cost Optimization Hub.** Free for AWS-managed recommendations.
  `ListRecommendations` and `ListRecommendationSummaries` are
  paginated with a small `MaxResults` cap (100) so we stay well
  under any rate limit.  We never call `UpdatePreferences` or the
  `*RecommendationPreferences` family of operations.
* **No local pricing calculations.** The deterministic rule engine
  never computes a dollar savings figure.  When AWS supplies one
  we surface it; when it does not, `savings_source=UNKNOWN` is
  recorded so the savings aggregate stays accurate.
* **CloudWatch reuse.** The optimization route reuses the Phase 2
  `batch_query` path so we do not double-bill for the same metric
  data.
