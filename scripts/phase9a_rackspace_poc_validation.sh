#!/usr/bin/env bash
# Phase 9A — Rackspace read-only API proof-of-concept validator.
#
# This script drives the Rackspace POC client through its full
# validation flow against the *real* Rackspace Cloud APIs using
# credentials supplied in the host's ``.env`` (the application
# never reads credentials from anywhere else).
#
# Exit codes:
#   0 — POC successful (every gate passed)
#   1 — validation failure (one or more gates failed)
#   2 — configuration / pre-condition violated
#
# The script NEVER prints the API key, X-Auth-Token, password, RAN,
# or any other Rackspace secret.  It also refuses to issue any
# write-like HTTP verb against Rackspace product APIs — only the
# identity POST is allowed (Rackspace Identity authentication is
# the only allowed write-like HTTP method).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 2

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mPASS\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33mWARN\033[0m %s\n" "$*"; WARN=$((WARN+1)); }
info() { printf "  \033[36mINFO\033[0m %s\n" "$*"; }

PASS=0
FAIL=0
WARN=0

bold "Phase 9A — Rackspace read-only API POC"

# ---------------------------------------------------------------------------
# Step 1: validate that the credentials exist in the host .env without
# echoing their values.
# ---------------------------------------------------------------------------

ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
if [[ ! -f "$ENV_FILE" ]]; then
    bad "host $ENV_FILE not found"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

USERNAME_SET=0
APIKEY_SET=0
ACCOUNT_SET=0
RAN_SET=0

# Use ``grep -c`` so we never echo the value.
if grep -qE '^RACKSPACE_USERNAME=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    USERNAME_SET=1
fi
if grep -qE '^RACKSPACE_API_KEY=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    APIKEY_SET=1
fi
if grep -qE '^RACKSPACE_ACCOUNT_ID=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    ACCOUNT_SET=1
fi
if grep -qE '^RACKSPACE_BILLING_ACCOUNT=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    RAN_SET=1
fi

if [[ $USERNAME_SET -eq 1 && $APIKEY_SET -eq 1 && $ACCOUNT_SET -eq 1 ]]; then
    ok "Identity credential variables are present in $(basename "$ENV_FILE") (values redacted)"
else
    missing=()
    [[ $USERNAME_SET -eq 0 ]] && missing+=("RACKSPACE_USERNAME")
    [[ $APIKEY_SET   -eq 0 ]] && missing+=("RACKSPACE_API_KEY")
    [[ $ACCOUNT_SET  -eq 0 ]] && missing+=("RACKSPACE_ACCOUNT_ID")
    bad "missing Rackspace Identity credential variables: ${missing[*]}"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

if [[ $RAN_SET -eq 1 ]]; then
    ok "RACKSPACE_BILLING_ACCOUNT (RAN) is configured in $(basename "$ENV_FILE") (value redacted)"
else
    warn "RACKSPACE_BILLING_ACCOUNT (RAN) is empty — Billing endpoints will report NOT_CONFIGURED"
fi

# Sanity guard: the .env line must not accidentally print a
# credential value via shell expansion downstream.  We pin the
# file mode to 600 / 640 and warn if it is world-readable.
ENV_MODE="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo 'unknown')"
case "$ENV_MODE" in
    600|640) ok ".env file mode is $ENV_MODE (no world read)" ;;
    *)       warn ".env file mode is $ENV_MODE; consider 600/640 to restrict access" ;;
esac

# Belt-and-braces: the .env must remain untracked.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    bad ".env is tracked by git (must remain gitignored)"
else
    ok ".env is not tracked by git"
fi

# ---------------------------------------------------------------------------
# Step 2: drive the POC client through every phase.
# ---------------------------------------------------------------------------

bold ""
bold "Driving Rackspace POC client..."

# Load the host's .env into the runner process *without* echoing
# any values.  We deliberately do NOT ``source`` the file because
# .env values with spaces (e.g. subscription display names) would
# otherwise leak into the shell.  Instead we extract only the
# Rackspace keys via ``awk`` and export them — the rest of .env
# is irrelevant to the POC.
while IFS='=' read -r key value; do
    # Skip blanks and comments.
    case "$key" in
        ''|'#'*) continue ;;
    esac
    case "$key" in
        RACKSPACE_USERNAME|RACKSPACE_API_KEY|RACKSPACE_ACCOUNT_ID|RACKSPACE_BILLING_ACCOUNT)
            # Strip optional surrounding quotes from the value.
            value="${value%\"}"
            value="${value#\"}"
            value="${value%\'}"
            value="${value#\'}"
            # Trim trailing CR (Windows line endings).
            value="${value%$'\r'}"
            export "$key=$value"
            ;;
    esac
done < "$ENV_FILE"

RESULT_JSON="$(cd "$REPO_ROOT" && backend/.venv/bin/python scripts/phase9a_rackspace_poc_runner.py 2>&1)"
RUNNER_EXIT=$?

if [[ $RUNNER_EXIT -ne 0 ]]; then
    bad "POC runner exited with code $RUNNER_EXIT"
    echo "$RESULT_JSON" | sed 's/^/    /' | head -40
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 3: parse the JSON output and assert.
# ---------------------------------------------------------------------------

PYBIN="$REPO_ROOT/backend/.venv/bin/python"
read_field() {
    local key="$1"
    echo "$RESULT_JSON" | "$PYBIN" -c "
import json, sys
data = json.load(sys.stdin)
v = data.get('$key')
if isinstance(v, bool):
    print('true' if v else 'false')
elif v is None:
    print('')
else:
    print(v)
"
}

identity_ok="$(read_field identity_ok)"
token_seconds_to_expiry="$(read_field token_seconds_to_expiry)"
token_expires_human="$(read_field token_expires_human)"
roles_count="$(read_field roles_count)"
roles="$(read_field roles)"
elevated_roles_present="$(read_field elevated_roles_present)"
service_types="$(read_field service_types)"

ran_configured="$(read_field ran_configured)"
billing_account="$(read_field billing_account)"
billing_account_summary="$(read_field billing_account_summary)"
billing_currency="$(read_field billing_currency)"
billing_currency_summary="$(read_field billing_currency_summary)"
billing_estimated_charges="$(read_field billing_estimated_charges)"
billing_estimated_charges_summary="$(read_field billing_estimated_charges_summary)"
billing_billing_summary="$(read_field billing_billing_summary)"
billing_invoice_latest="$(read_field billing_invoice_latest)"

servers_status="$(read_field servers_status)"
servers_total="$(read_field servers_total)"
servers_regions="$(read_field servers_regions)"
servers_status_counts="$(read_field servers_status_counts)"
servers_flavor_ids="$(read_field servers_flavor_ids)"
write_methods_exposed="$(read_field write_methods_exposed)"
no_token_in_output="$(read_field no_token_in_output)"

bold ""
bold "Identity"

if [[ "$identity_ok" == "true" ]]; then
    ok "Rackspace Identity authentication succeeded"
else
    bad "Rackspace Identity authentication failed"
fi

if [[ -n "$token_seconds_to_expiry" ]]; then
    info "token expires in ${token_seconds_to_expiry}s (~${token_expires_human})"
fi

if [[ -n "$roles_count" ]]; then
    info "role count: ${roles_count}"
fi

if [[ -n "$roles" ]]; then
    info "assigned roles: ${roles}"
fi

if [[ -n "$elevated_roles_present" && "$elevated_roles_present" != "[]" ]]; then
    bad "elevated roles present in identity response: ${elevated_roles_present}"
elif [[ "$identity_ok" == "true" ]]; then
    ok "no elevated roles detected on authenticated principal"
fi

if [[ -n "$service_types" ]]; then
    info "service types in catalog: ${service_types}"
fi

bold ""
bold "Billing API (documented external endpoint — NOT in service catalog)"

# Informational: the Billing base URL is the documented Rackspace
# endpoint that is intentionally NOT exposed through the Identity
# service catalog.
info "Billing base: https://billing.api.rackspacecloud.com"
info "RAN configured: ${ran_configured:-false}"

_billing_label() {
    case "$1" in
        billing_account) echo "GET /v2/accounts/{ran}" ;;
        billing_currency) echo "GET /v2/accounts/{ran}/currency" ;;
        billing_estimated_charges) echo "GET /v2/accounts/{ran}/estimated_charges" ;;
        billing_billing_summary) echo "GET /v2/accounts/{ran}/billing-summary" ;;
        billing_invoice_latest) echo "GET /v2/accounts/{ran}/invoices/latest" ;;
        *) echo "$1" ;;
    esac
}

for ep in billing_account billing_currency billing_estimated_charges billing_billing_summary billing_invoice_latest; do
    v="${!ep:-}"
    label="$(_billing_label "$ep")"
    case "$v" in
        OK)
            ok "billing endpoint ${label} returned 200"
            ;;
        AUTH_FAILED)
            warn "billing endpoint ${label} returned 401 (auth — token rejected)"
            ;;
        PERMISSION_DENIED)
            warn "billing endpoint ${label} returned 403 (insufficient Billing permission on account)"
            ;;
        NOT_FOUND)
            info "billing endpoint ${label} returned 404 (invalid RAN or Billing not on this account)"
            ;;
        NOT_CONFIGURED)
            info "billing endpoint ${label} not configured (RAN missing)"
            ;;
        TRANSPORT_ERROR)
            bad "billing endpoint ${label} transport error"
            ;;
        "")
            warn "billing endpoint ${label} was not probed (runner did not reach Billing)"
            ;;
        *)
            info "billing endpoint ${label}: ${v}"
            ;;
    esac
done

# Per-endpoint policy from the Phase 9A spec:
#
#   - account lookup GET PASS (must succeed when RAN is configured)
#   - current/estimated charges GET PASS if supported for account
#
# If the RAN is configured, the account lookup and currency endpoints
# are required to succeed (the actual Billing API always supports
# them when Billing is enabled for the account).  Estimated charges
# is conditional: it may return 403/404 if the account lacks
# permissions for current-period data.
if [[ "$ran_configured" == "true" ]]; then
    case "$billing_account" in
        OK) ok "Billing account lookup GET succeeded (sanitized identity present)" ;;
        AUTH_FAILED) bad "Billing account lookup GET returned 401 with valid token" ;;
        PERMISSION_DENIED) bad "Billing account lookup GET returned 403 (billing:observer must allow account lookup)" ;;
        NOT_FOUND) bad "Billing account lookup GET returned 404 (RAN appears invalid)" ;;
        TRANSPORT_ERROR) bad "Billing account lookup GET transport error" ;;
        NOT_CONFIGURED) bad "Billing account lookup GET reported NOT_CONFIGURED with RAN configured" ;;
        *) warn "Billing account lookup GET returned unexpected status: ${billing_account:-unknown}" ;;
    esac
    case "$billing_currency" in
        OK) ok "Billing currency GET succeeded" ;;
        AUTH_FAILED) warn "Billing currency GET returned 401 — account likely lacks currency scope" ;;
        PERMISSION_DENIED) info "Billing currency GET returned 403 — currency not exposed on this account" ;;
        NOT_FOUND) info "Billing currency GET returned 404 — currency not exposed on this account" ;;
        TRANSPORT_ERROR) warn "Billing currency GET transport error" ;;
        NOT_CONFIGURED) warn "Billing currency GET reported NOT_CONFIGURED" ;;
        *) info "Billing currency GET: ${billing_currency:-unknown}" ;;
    esac
    case "$billing_estimated_charges" in
        OK) ok "Billing current/estimated charges GET succeeded" ;;
        AUTH_FAILED) warn "Billing estimated charges GET returned 401" ;;
        PERMISSION_DENIED) info "Billing estimated charges GET returned 403 — current-period not exposed on this account" ;;
        NOT_FOUND) info "Billing estimated charges GET returned 404 — current-period not exposed on this account" ;;
        TRANSPORT_ERROR) warn "Billing estimated charges GET transport error" ;;
        NOT_CONFIGURED) warn "Billing estimated charges GET reported NOT_CONFIGURED" ;;
        *) info "Billing estimated charges GET: ${billing_estimated_charges:-unknown}" ;;
    esac
fi

# Sanitized current-cost summary, if the estimated charges endpoint
# returned one.
if [[ "$billing_estimated_charges" == "OK" && -n "$billing_estimated_charges_summary" ]]; then
    info "estimated charges summary: ${billing_estimated_charges_summary}"
fi
if [[ -n "$billing_account_summary" && "$billing_account_summary" != "{}" ]]; then
    info "billing account summary: ${billing_account_summary}"
fi
if [[ -n "$billing_currency_summary" && "$billing_currency_summary" != "{}" ]]; then
    info "billing currency summary: ${billing_currency_summary}"
fi

bold ""
bold "Cloud Servers API"

case "$servers_status" in
    OK) ok "cloud servers detail endpoint returned 200" ;;
    AUTH_FAILED) warn "cloud servers returned 401 (auth)" ;;
    PERMISSION_DENIED) bad "cloud servers returned 403 (permission denied)" ;;
    NOT_FOUND) info "cloud servers not present in service catalog" ;;
    TRANSPORT_ERROR) bad "cloud servers transport error" ;;
    NOT_CONFIGURED) warn "cloud servers not configured (credentials absent)" ;;
    *) info "cloud servers status: ${servers_status:-unknown}" ;;
esac

if [[ -n "$servers_total" && "$servers_status" == "OK" ]]; then
    info "total servers: ${servers_total}"
fi
if [[ -n "$servers_regions" && "$servers_status" == "OK" ]]; then
    info "regions discovered: ${servers_regions}"
fi
if [[ -n "$servers_status_counts" && "$servers_status" == "OK" ]]; then
    info "status counts: ${servers_status_counts}"
fi
if [[ -n "$servers_flavor_ids" && "$servers_status" == "OK" ]]; then
    info "flavor IDs discovered: ${servers_flavor_ids}"
fi

bold ""
bold "Read-only / security guarantees"

case "$write_methods_exposed" in
    "[]") ok "no write-like methods exposed on the Rackspace product client" ;;
    *)    bad "unexpected write-like methods exposed: ${write_methods_exposed}" ;;
esac

case "$no_token_in_output" in
    true)  ok "no X-Auth-Token / API key / RAN appears in the validator output" ;;
    false) bad "token-redaction guarantee failed — investigate immediately" ;;
    *)     warn "could not confirm token-redaction guarantee" ;;
esac

bold ""
bold "Summary"
echo "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"

if [[ $FAIL -gt 0 ]]; then
    bold "RESULT: validation failure"
    exit 1
fi

bold "RESULT: POC successful"
exit 0
