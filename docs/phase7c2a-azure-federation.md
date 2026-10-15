# Phase 7C.2A — Azure Workload Identity Federation

> **Scope.** Phase 7C.2A delivers **only** the Azure authentication +
> subscription-bootstrap layer for the multi-cloud platform. Cost
> Management, Resource Graph, and Advisor are scheduled for 7C.2B /
> 7C.2C / 7C.2D respectively and are explicitly out of scope here.

## What ships

### Backend

- `backend/app/services/cloud/azure/redaction.py` — sanitisation
  helpers: `scrub_jwt`, `scrub_authorization`,
  `scrub_client_assertion`, `scrub_aws_access_key`,
  `scrub_tenant_id`, `scrub_log_payload`, `safe_log_extra`,
  `safe_subscription_label`, `category_from_azure_error`. The seven
  `AZURE_STATUS_*` constants are defined here.
- `backend/app/services/cloud/azure/federation.py` — the AWS-side
  workload-identity federation assertion provider. Issues an STS
  `GetWebIdentityToken` against a *regional* STS endpoint with
  RS256 signing, a clamped `[60, 900]`-second lifetime, and an
  audience pinned to `api://AzureADTokenExchange` by default. No
  static AWS keys are ever supplied.
- `backend/app/services/cloud/azure/credential.py` — the
  `azure-identity` `ClientAssertionCredential` layer
  (`AzureRuntimeConfig`, `build_client_assertion_credential`,
  `get_credential`, `acquire_access_token`, `is_configured`).
- `backend/app/services/cloud/azure/arm_client.py` — minimal ARM
  REST client (`AzureArmClient.validate_subscription`).
- `backend/app/services/cloud/azure/registrations.py` — runtime
  subscription configuration loader.
- `backend/app/services/cloud/azure/adapter.py` — the
  `AzureCloudProvider` implementation of `CloudProvider`,
  `bootstrap_azure_subscriptions_once`, and `_register` self-
  registration into the singleton registry.
- `backend/app/core/config.py` — five new Azure settings.
- `backend/app/services/cloud/base.py` — new
  `CloudCapability.AUTHENTICATION` and
  `CloudCapability.SUBSCRIPTIONS` enum values.
- `backend/app/services/cloud/registry.py` — imports the Azure
  adapter during `_register_known_providers`.
- `backend/app/schemas/cloud_account.py` — per-provider validation
  (Azure GUID + lower-case normalisation, refuses `role_arn` on
  Azure rows, defaults `subscription_id` to `account_id`).
- `backend/app/services/aws/account_provider.py` —
  `create_account` carries `tenant_id` / `subscription_id` /
  `provider_metadata["credential_strategy"]` for Azure rows;
  `update_account` refuses Azure-side `role_arn` mutations.
- `backend/app/api/cloud_accounts.py` — every response carries the
  new fields; `validate_cloud_account` routes through the
  `ProviderRegistry` so Azure rows use the Azure adapter.
- `backend/app/main.py` — `_phase7c2a_bootstrap_azure_subscriptions`
  startup hook.
- `backend/requirements.txt` — `azure-identity==1.18.0`.
- `.env.example` — new `AZURE_*` block.

### Frontend

- `frontend/src/components/ProviderSelector.tsx` — top-bar provider
  selector (only registered + enabled providers are selectable).
- `frontend/src/components/ProviderCapabilityNotice.tsx` —
  capability-aware empty state.
- `frontend/src/components/AccountSelector.tsx` — provider-aware
  labels + row filtering + reset-to-`all` on provider change.
- `frontend/src/components/TopNavigation.tsx` — mounts
  `ProviderSelector`.
- `frontend/src/pages/AccountsPage.tsx` — provider-aware registry
  page (per-provider form, per-provider table sections,
  provider-aware column headers, provider-aware row actions).
- `frontend/src/pages/CostsPage.tsx`,
  `frontend/src/pages/ResourcesPage.tsx`,
  `frontend/src/pages/OptimizationPage.tsx`,
  `frontend/src/pages/DashboardPage.tsx` — early-return
  `ProviderCapabilityNotice` so AWS endpoints are never invoked
  under an Azure selection.

### Tests

- `backend/tests/test_phase7c2a_azure_workload_federation.py`
- `backend/tests/test_phase7c2a_azure_subscription_validation.py`
- `backend/tests/test_phase7c2a_provider_registry.py`
- `backend/tests/test_phase7c2a_secrets_not_logged.py`

Total: **63 Phase 7C.2A tests, all green.**

## Security contract

- **No Azure client secret is ever accepted, stored, or logged.**
  The only credential strategy on the Azure adapter is
  `AZURE_WORKLOAD_FEDERATION`. (`AZURE_CLIENT_SECRET` is reserved
  in the enum but has no implementation at this layer.)
- **No static AWS keys are ever read by the federation provider.**
  The EC2 instance profile chain is the only credential source for
  the AWS STS call.
- **No JWT, no `Authorization: Bearer …`, no `client_assertion=…`
  query segment, no AWS access-key id is ever logged.** The
  redaction layer is the single source of truth.
- **No data fabrication under Azure.** When Azure is selected the
  costs / resources / optimization / dashboard surfaces show the
  capability notice instead of fabricating zero values or invoking
  AWS endpoints.

## Verification

| Gate | Result |
| --- | --- |
| Backend pytest (754 tests, 1 environment-specific skip) | **PASS** |
| Backend Phase 7C.2A targeted suite (63 tests) | **PASS** |
| Backend Phase 7C.1 / 7B regression | **PASS** |
| Frontend `vitest run` (396 tests, 43 files) | **PASS** |
| Frontend `tsc --noEmit` | **PASS** (exit 0) |
| Live Azure validation | **DEFERRED** to operator deployment (documented below) |

## Live Azure acceptance gate

Phase 7C.2A's live Azure validation requires an EC2 instance
profile wired to the Entra federated credential and a deployment
`.env` carrying `AZURE_TENANT_ID` / `AZURE_CLIENT_ID`. The unit
tests pin every contract that does not require a live STS / Entra
round-trip; the operator must run the live acceptance gate in
their production AWS account before tagging 7C.2A operationally
complete.

**Operator deployment steps**:

1. Set `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` (non-secret
   identifiers) in the deployment `.env`.
2. Configure the Entra App Registration's federated credential
   against the AWS IAM role documented in
   `app/services/cloud/azure/federation.py:DEFAULT_ROLE_ARN`.
3. Verify the EC2 instance profile can
   `sts:GetWebIdentityToken` against a regional STS endpoint.
4. Restart the application; the bootstrap hook inserts the
   configured subscriptions into `cloud_accounts`.
5. As ADMIN, `POST /api/aws/cloud-accounts/<id>/validate` against
   each row — the response must be
   `{"status": "CONNECTED", "arn": "<subscription-id>", ...}` for
   every subscription the service principal can see.

## What 7C.2A does NOT ship

- Azure Cost Management (7C.2B).
- Azure Resource Graph inventory (7C.2C).
- Azure Advisor (7C.2D).
- Rackspace adapter / schema / UI (7C.3).
- Azure client-secret auth (`AZURE_CLIENT_SECRET` reserved, not
  implemented).
- Azure certificate auth (reserved for a future phase).

## What was changed during this delivery to unblock stale tests

The 7C.2A refactor legitimately widened two Phase 7C.1 contracts
(Azure is no longer a "reserved" provider — it is now registered)
and three Phase 7B source-level contracts (the AccountsPage edit
hint now reads as a single `<p>` rather than a single string
literal; the Disable / Enable callbacks carry intermediate
wrappers; the AccountSelector now requires `ProviderProvider` in
its render tree). The affected tests were updated to match the
new shape without weakening any invariant:

- `tests/test_phase7c1_cloud_providers_route.py` — Azure is now
  asserted as `is_registered=True`; RACKSPACE remains
  `NOT_REGISTERED`.
- `frontend/src/tests/phase7b-account-lifecycle.test.tsx` —
  regex matchers relaxed to allow intermediate wrappers.
- `frontend/src/tests/phase7b-account-edit.test.tsx` — multi-line
  JSX hint asserted by stable substrings; a new test pins the
  new copy literal.
- `frontend/src/tests/phase7b-runtime-closure.test.tsx` —
  AccountSelector render is wrapped in `<ProviderProvider>` so
  the new `useProviderContext` dependency is satisfied.
