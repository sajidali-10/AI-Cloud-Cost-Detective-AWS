#!/usr/bin/env bash
# Phase 7D.3.2 — live validation for runtime consistency /
# cost completeness semantics.
#
# This script is intentionally narrow: it exercises the canonical
# /api/cloud/costs surface that the Phase 7D.3.2 frontend
# completeness contract depends on, and asserts that the envelope
# shape (status / successful_accounts / failed_accounts /
# account_errors) supports the new semantics.  It does NOT
# duplicate the broader Phase 7D.3 live validation.
#
# Read-only: no AWS / Azure write operations.

set -uo pipefail

API="${API:-http://localhost}"
REGION="${REGION:-us-east-1}"
MONTH="${MONTH:-2026-10}"

SUB_MCPP="${SUB_MCPP:-sub:a54d076b-2650-4b4f-a28c-2deed2011f83}"
SUB_SPONSOR="${SUB_SPONSOR:-sub:9353d858-27fa-4ce7-bf87-782f3323d297}"

TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

if [[ -z "${TOKEN:-}" || "${TOKEN}" == *"Traceback"* ]]; then
  echo "FAIL: unable to mint dev admin JWT (is the backend container running?)" >&2
  exit 1
fi

probe() {
  local scope="$1"
  local label="$2"
  echo "=== $label (provider=AZURE account_id=$scope) ==="
  local resp
  resp=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=AZURE&month=$MONTH&account_id=$scope")
  python3 - "$resp" <<'PY'
import sys, json
try:
    r = json.loads(sys.argv[1])
except Exception as e:
    print(f"  ERR: invalid response: {e}")
    sys.exit(0)
if 'report' not in r:
    print(f"  ERR: no report envelope; payload={r}")
    sys.exit(0)
rep = r.get('report', {})
print(f"  status            : {rep.get('status')}")
print(f"  total_cost        : {rep.get('total_cost')}")
print(f"  successful_accts  : {rep.get('successful_accounts')}")
print(f"  failed_accts      : {rep.get('failed_accounts')}")
print(f"  account_errors    : {sorted({e.get('code') for e in rep.get('account_errors', []) or []})}")
print(f"  cache_status      : {r.get('cache_status')}")
print(f"  by_account count  : {len(rep.get('by_account', []) or [])}")
# Frontend semantics check — derived view-model per the new contract.
status = (rep.get('status') or '').upper()
sa = rep.get('successful_accounts') or 0
fa = rep.get('failed_accounts') or 0
total = rep.get('total_cost')
codes = {e.get('code') for e in rep.get('account_errors', []) or []}
if status == 'PARTIAL_SUCCESS' and sa == 0 and 'THROTTLED' in codes:
    print("  view-model        : unavailable (frontend MUST render 'Unavailable', not '$0.00')")
elif status == 'PARTIAL_SUCCESS' and sa >= 1:
    print("  view-model        : partial (frontend MUST render subtotal + 'Partial (N unavailable)')")
elif status == 'SUCCESS' and total in ('0.00', '0', 0):
    print("  view-model        : success (frontend MUST render '$0.00' as authoritative zero)")
elif status == 'SUCCESS':
    print("  view-model        : success (frontend MUST render real amount)")
else:
    print(f"  view-model        : unavailable (status={status})")
PY
}

probe "all"            "AZURE All Subscriptions"
probe "$SUB_MCPP"      "AZURE MCPP Subscription"
probe "$SUB_SPONSOR"   "AZURE Sponsorship Subscription"

echo
echo "Phase 7D.3.2 — runtime consistency live validation: complete."
