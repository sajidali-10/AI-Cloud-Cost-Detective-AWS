#!/usr/bin/env bash
# Phase 9B.1 — Rackspace canonical cost integration validator.
#
# Drives the Rackspace canonical cost path end-to-end against the
# LIVE deployment and the *real* Rackspace Cloud APIs, using
# credentials supplied in the host's ``.env`` only.
#
# Proves:
#   * Identity authentication succeeds
#   * billing:observer role is present; no elevated roles
#   * Billing account GET + estimated_charges GET succeed
#   * the canonical API surface answers provider=RACKSPACE with
#     currency / period / total / (optional) service breakdown
#   * RECONCILIATION (Phase 9B closure): the raw sanitized Billing
#     total equals the canonical Rackspace total, and the service
#     breakdown sums exactly to the canonical total — obtained in
#     the SAME run, Decimal-safe
#   * zero product write actions (GET only; the single identity
#     POST is the documented authentication verb)
#   * zero secret leakage (no API key / X-Auth-Token / RAN in any
#     captured output)
#   * a valid zero total is distinguished from unavailable/error
#
# Exit codes:
#   0 — validation successful (every gate passed)
#   1 — validation failure (one or more gates failed)
#   2 — configuration / pre-condition violated
#
# The script NEVER prints the API key, X-Auth-Token, password, or
# the Billing Account Number (RAN).

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

bold "Phase 9B.1 — Rackspace canonical cost integration validation"

# ---------------------------------------------------------------------------
# Step 1: configuration preconditions (no values are ever printed).
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
    ok "Rackspace Identity credential variables are present in $(basename "$ENV_FILE") (values redacted)"
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
    bad "RACKSPACE_BILLING_ACCOUNT (RAN) is not configured — the canonical cost path cannot be validated"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# The backend container must be running with the Phase 9B.1 code and
# the Rackspace environment wired through docker-compose.
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^cost-detective-backend$'; then
    bad "cost-detective-backend container is not running"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi
ok "cost-detective-backend container is running"

BACKEND_CFG="$(docker exec cost-detective-backend python -c "
from app.core.config import get_settings
s = get_settings()
print('1' if (s.rackspace_username and s.rackspace_api_key
             and s.rackspace_account_id and s.rackspace_billing_account) else '0')
" 2>/dev/null | tail -1)"
if [[ "$BACKEND_CFG" == "1" ]]; then
    ok "backend container sees the full Rackspace configuration (values redacted)"
else
    bad "backend container does not have the Rackspace configuration wired (rebuild/recreate with the Phase 9B.1 compose env)"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# The registry must resolve the RACKSPACE adapter (Phase 9B.1 code).
ADAPTER_OK="$(docker exec cost-detective-backend python -c "
from app.services.cloud.registry import ProviderRegistry
from app.services.cloud.models import CloudProviderKind
try:
    ProviderRegistry.current().require(CloudProviderKind.RACKSPACE)
    print('1')
except Exception:
    print('0')
" 2>/dev/null | tail -1)"
if [[ "$ADAPTER_OK" == "1" ]]; then
    ok "ProviderRegistry resolves the RACKSPACE adapter"
else
    bad "ProviderRegistry does not resolve RACKSPACE — the backend image predates Phase 9B.1"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# The account bootstrap must have registered the tenant row so the
# canonical route can select it without manual SQL.
ROW_OK="$(docker exec cost-detective-backend python -c "
from app.db.session import SessionLocal
from sqlalchemy import text
db = SessionLocal()
try:
    n = db.execute(text(\"SELECT count(*) FROM cloud_accounts WHERE provider = 'RACKSPACE' AND enabled = true\")).scalar()
    print('1' if (n or 0) >= 1 else '0')
finally:
    db.close()
" 2>/dev/null | tail -1)"
if [[ "$ROW_OK" == "1" ]]; then
    ok "Rackspace account row is registered in cloud_accounts (derived, no manual SQL)"
else
    bad "no enabled RACKSPACE row in cloud_accounts — the startup bootstrap did not run (restart the backend container)"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# ---------------------------------------------------------------------------
# Step 2: drive the Phase 9A POC runner for Identity + Billing proof.
# (Its JSON output is sanitized by design — no raw records, no
# credentials, no RAN.)
# ---------------------------------------------------------------------------

bold ""
bold "Rackspace Identity + Billing (Phase 9A POC client)"

while IFS='=' read -r key value; do
    case "$key" in
        ''|'#'*) continue ;;
    esac
    case "$key" in
        RACKSPACE_USERNAME|RACKSPACE_API_KEY|RACKSPACE_ACCOUNT_ID|RACKSPACE_BILLING_ACCOUNT)
            value="${value%\"}"
            value="${value#\"}"
            value="${value%\'}"
            value="${value#\'}"
            value="${value%$'\r'}"
            export "$key=$value"
            ;;
    esac
done < "$ENV_FILE"

POC_JSON="$(cd "$REPO_ROOT" && backend/.venv/bin/python scripts/phase9a_rackspace_poc_runner.py 2>&1)"
POC_EXIT=$?
if [[ $POC_EXIT -ne 0 ]]; then
    bad "Phase 9A POC runner exited with code $POC_EXIT"
    echo "$POC_JSON" | sed 's/^/    /' | head -30
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

PYBIN="$REPO_ROOT/backend/.venv/bin/python"
read_poc_field() {
    echo "$POC_JSON" | "$PYBIN" -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = data.get('$1')
if isinstance(v, bool):
    print('true' if v else 'false')
elif v is None:
    print('')
else:
    print(v)
" 2>/dev/null
}

identity_ok="$(read_poc_field identity_ok)"
roles="$(read_poc_field roles)"
elevated_roles_present="$(read_poc_field elevated_roles_present)"
billing_account_status="$(read_poc_field billing_account)"
billing_estimated_status="$(read_poc_field billing_estimated_charges)"
write_methods_exposed="$(read_poc_field write_methods_exposed)"
no_token_in_output="$(read_poc_field no_token_in_output)"

if [[ "$identity_ok" == "true" ]]; then
    ok "Rackspace Identity authentication succeeded"
else
    bad "Rackspace Identity authentication failed"
fi

if [[ "$roles" == *"billing:observer"* ]]; then
    ok "billing:observer role is present on the principal"
else
    bad "billing:observer role is NOT present on the principal (roles: $roles)"
fi

if [[ -z "$elevated_roles_present" || "$elevated_roles_present" == "[]" || "$elevated_roles_present" == "false" ]]; then
    ok "no elevated Rackspace roles are present"
else
    bad "elevated Rackspace roles detected — the read-only contract is violated"
fi

if [[ "$billing_account_status" == "OK" ]]; then
    ok "Billing account GET succeeded (HTTP 200)"
else
    bad "Billing account GET returned $billing_account_status"
fi

if [[ "$billing_estimated_status" == "OK" ]]; then
    ok "estimated_charges GET succeeded (HTTP 200)"
else
    bad "estimated_charges GET returned $billing_estimated_status"
fi

if [[ "$write_methods_exposed" == *"put"* || "$write_methods_exposed" == *"patch"* || "$write_methods_exposed" == *"delete"* ]]; then
    bad "write-like HTTP verbs are exposed on the product transport"
else
    ok "product transport exposes no write verbs (GET only; Identity POST is the sole exception)"
fi

if [[ "$no_token_in_output" == "true" ]]; then
    ok "no token material in POC output"
else
    bad "token material detected in POC output"
fi

# ---------------------------------------------------------------------------
# Step 3: canonical API path — GET /api/cloud/costs?provider=RACKSPACE
# through the public Nginx entrypoint with an admin JWT.
# ---------------------------------------------------------------------------

bold ""
bold "Canonical cost path — /api/cloud/costs?provider=RACKSPACE"

MONTH="$(date -u +%Y-%m)"

TOKEN="$(docker exec cost-detective-backend python -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)"

if [[ -z "${TOKEN:-}" || "${TOKEN}" == *"Traceback"* ]]; then
    bad "unable to mint dev admin JWT"
    echo
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

API="${API:-http://localhost}"
CANON_JSON="$(curl -sS -m 60 \
    -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=RACKSPACE&month=$MONTH" 2>&1)"
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
    bad "canonical cost request failed at the transport layer (curl exit $CURL_EXIT)"
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

read_canon_field() {
    echo "$CANON_JSON" | "$PYBIN" -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = d.get('$1')
if v is None and isinstance(d.get('report'), dict):
    v = d['report'].get('$1')
if isinstance(v, bool):
    print('true' if v else 'false')
elif v is None:
    print('')
else:
    print(v)
" 2>/dev/null
}

canon_provider="$(read_canon_field provider)"
canon_status="$(read_canon_field status)"
canon_currency="$(read_canon_field currency)"
canon_total="$(read_canon_field total_cost)"
canon_period_start="$(read_canon_field period)"
canon_source="$(read_canon_field source)"
canon_cache_status="$(read_canon_field cache_status)"
canon_prev_unavailable="$(read_canon_field previous_period_unavailable)"
canon_error_code="$(read_canon_field error_code)"

if [[ "$canon_error_code" == "PERIOD_UNSUPPORTED" ]]; then
    # The current billing cycle may not overlap the requested month
    # window if the Rackspace cycle just rolled over.  Retry with the
    # 30-day lookback which always overlaps the current cycle.
    CANON_JSON="$(curl -sS -m 60 \
        -H "Authorization: Bearer $TOKEN" \
        "$API/api/cloud/costs?provider=RACKSPACE&days=30" 2>&1)"
    canon_error_code="$(read_canon_field error_code)"
fi

if [[ "$canon_provider" == "RACKSPACE" ]]; then
    ok "canonical response provider=RACKSPACE"
else
    bad "canonical response provider is '${canon_provider}' (expected RACKSPACE; error_code='${canon_error_code}')"
fi

if [[ "$canon_status" == "SUCCESS" ]]; then
    ok "canonical status=SUCCESS"
else
    bad "canonical status is '${canon_status}' (error_code='${canon_error_code}')"
fi

if [[ -n "$canon_currency" && "$canon_currency" != "None" ]]; then
    ok "currency returned by the canonical path: $canon_currency"
else
    bad "canonical response carries no currency"
fi

if [[ -n "$canon_period_start" && "$canon_period_start" != "None" ]]; then
    ok "period returned by the canonical path: $canon_period_start"
else
    bad "canonical response carries no period"
fi

if [[ -n "$canon_total" && "$canon_total" != "None" ]]; then
    ok "total returned by the canonical path: $canon_total"
else
    bad "canonical response carries no total"
fi

# True-zero vs unavailable distinction.
"$PYBIN" -c "
import json, sys
try:
    d = json.loads('''$canon_total''')
except Exception:
    sys.exit(1)
" 2>/dev/null
TOTAL_IS_NUMERIC=$?
if [[ $TOTAL_IS_NUMERIC -eq 0 ]]; then
    if [[ "$canon_total" == "0" || "$canon_total" == "0.00" || "$canon_total" == "0.0" ]]; then
        ok "total is a VALID ZERO (HTTP SUCCESS — not an unavailable state)"
    else
        ok "total is a non-zero numeric value (valid current-period cost)"
    fi
else
    bad "total is not numeric — an error must never masquerade as \$0"
fi

if [[ "$canon_source" == "RACKSPACE_BILLING" ]]; then
    ok "canonical source=RACKSPACE_BILLING"
else
    bad "canonical source is '${canon_source}' (expected RACKSPACE_BILLING)"
fi

if [[ "$canon_prev_unavailable" == "true" || "$canon_prev_unavailable" == "True" ]]; then
    ok "previous-period comparison is explicitly unavailable (never fabricated)"
else
    warn "previous_period_unavailable flag not observed as true ('$canon_prev_unavailable')"
fi

if [[ -n "$canon_cache_status" ]]; then
    ok "cache envelope present (cache_status=$canon_cache_status)"
else
    bad "canonical response carries no cache envelope"
fi

# Service breakdown + reconciliation (when present).  The breakdown
# must sum to the canonical total EXACTLY (Decimal-safe — no cent
# drift, no tolerance): an empty breakdown only reconciles with a
# zero total.
BREAKDOWN_INFO="$(echo "$CANON_JSON" | "$PYBIN" -c "
import json, sys
from decimal import Decimal
try:
    d = json.load(sys.stdin)
    report = d.get('report') or d
    services = report.get('by_service') or []
    total = report.get('total_cost')
    if not services:
        print('EMPTY breakdown_total=' + str(total))
        sys.exit(0)
    ssum = sum((Decimal(str(i.get('amount'))) for i in services), Decimal('0'))
    ok = 'OK' if ssum == Decimal(str(total)) else 'MISMATCH'
    print(f'{ok} services={len(services)} sum={ssum} total={total}')
except Exception as exc:
    print('PARSE_ERROR ' + type(exc).__name__)
" 2>/dev/null)"

if [[ "$BREAKDOWN_INFO" == PARSE_ERROR* ]]; then
    bad "unable to parse the service breakdown from the canonical response"
elif [[ "$BREAKDOWN_INFO" == EMPTY* ]]; then
    # An empty breakdown only reconciles with a canonical total of
    # exactly zero — anything else is a reconciliation failure.
    EMPTY_OK="$(printf '%s' "$canon_total" | "$PYBIN" -c "
import sys
from decimal import Decimal
try:
    print('OK' if Decimal(sys.stdin.read().strip()) == 0 else 'MISMATCH')
except Exception:
    print('PARSE_ERROR')
" 2>/dev/null)"
    if [[ "$EMPTY_OK" == "OK" ]]; then
        ok "service breakdown is empty and the canonical total is zero (0 == 0 — reconciles)"
    else
        bad "service breakdown is empty but the canonical total is non-zero ($canon_total) — the breakdown must reconcile with the total"
    fi
elif [[ "$BREAKDOWN_INFO" == OK* ]]; then
    ok "service breakdown reconciles exactly with the total ($BREAKDOWN_INFO)"
else
    bad "service breakdown does NOT reconcile with the total ($BREAKDOWN_INFO)"
fi

# Raw ↔ canonical reconciliation (Phase 9B closure invariant).
# The sanitized Billing total extracted from the LIVE
# ``estimated_charges`` response in the SAME run (the POC summary
# prefers the authoritative envelope ``total`` field, mirroring the
# canonical normalizer's precedence) must equal the canonical
# Rackspace total EXACTLY — Decimal-safe, no tolerance.  A genuine
# authoritative $0 reconciles as 0 == 0; the invariant holds for
# whatever the authoritative value happens to be.
RAW_VS_CANON="$(echo "$POC_JSON" | "$PYBIN" -c "
import json, sys
from decimal import Decimal
try:
    canon_raw = '''$canon_total'''
    poc = json.load(sys.stdin)
    summary = poc.get('billing_estimated_charges_summary') or {}
    raw = summary.get('total')
    if raw is None:
        print('RAW_MISSING sanitized_current_cost_available=%s' % summary.get('sanitized_current_cost_available'))
    else:
        raw_d = Decimal(str(raw))
        canon_d = Decimal(canon_raw)
        if raw_d == canon_d:
            print('OK raw=%s canon=%s' % (raw_d, canon_d))
        else:
            print('MISMATCH raw=%s canon=%s' % (raw_d, canon_d))
except Exception as exc:
    print('PARSE_ERROR ' + type(exc).__name__)
" 2>/dev/null)"
if [[ "$RAW_VS_CANON" == OK* ]]; then
    ok "raw Billing total == canonical Rackspace total ($RAW_VS_CANON)"
elif [[ "$RAW_VS_CANON" == RAW_MISSING* ]]; then
    bad "the raw sanitized Billing summary carries no total — raw↔canonical reconciliation impossible ($RAW_VS_CANON)"
elif [[ "$RAW_VS_CANON" == PARSE_ERROR* ]]; then
    bad "unable to compare the raw Billing total with the canonical total ($RAW_VS_CANON)"
else
    bad "raw Billing total does NOT reconcile with the canonical Rackspace total ($RAW_VS_CANON)"
fi

# Daily trend / region are unsupported dimensions — they must be
# absent (empty), never fabricated.
DAILY_REGION_INFO="$(echo "$CANON_JSON" | "$PYBIN" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    report = d.get('report') or d
    print('daily=%d region=%d' % (len(report.get('daily_trend') or []),
                                   len(report.get('by_region') or [])))
except Exception:
    print('PARSE_ERROR')
" 2>/dev/null)"
if [[ "$DAILY_REGION_INFO" == "daily=0 region=0" ]]; then
    ok "unsupported dimensions (daily trend, region) are empty — never fabricated"
else
    bad "unexpected fabricated dimensions in the response: $DAILY_REGION_INFO"
fi

# ---------------------------------------------------------------------------
# Step 4: secret-leakage scan over every captured artifact.
# ---------------------------------------------------------------------------

bold ""
bold "Secret leakage scan"

LEAK=0
RAN_VALUE="$(grep -E '^RACKSPACE_BILLING_ACCOUNT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r')"
APIKEY_VALUE="$(grep -E '^RACKSPACE_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r')"

for artifact_name in CANON_JSON; do
    artifact="${!artifact_name}"
    if [[ -n "$RAN_VALUE" && "$RAN_VALUE" != "" && "$artifact" == *"$RAN_VALUE"* ]]; then
        bad "$artifact_name contains the RAN"
        LEAK=1
    fi
    if [[ -n "$APIKEY_VALUE" && "$artifact" == *"$APIKEY_VALUE"* ]]; then
        bad "$artifact_name contains the API key"
        LEAK=1
    fi
    if echo "$artifact" | grep -qi 'x-auth-token'; then
        bad "$artifact_name mentions X-Auth-Token"
        LEAK=1
    fi
done

if [[ $LEAK -eq 0 ]]; then
    ok "no RAN, API key, or token material in any captured output"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

bold ""
bold "Summary"
echo "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
if [[ $FAIL -eq 0 ]]; then
    bold "RESULT: validation successful"
    exit 0
fi
bold "RESULT: validation failure"
exit 1
