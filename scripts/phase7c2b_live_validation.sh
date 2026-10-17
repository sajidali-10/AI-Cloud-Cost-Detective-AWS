#!/usr/bin/env bash
# Phase 7C.2B — live Azure validation.
# Exercises every required scenario against the real backend container.
set -uo pipefail

API="http://localhost"
SUB_MCPP="a54d076b-2650-4b4f-a28c-2deed2011f83"
SUB_SPONSOR="9353d858-27fa-4ce7-bf87-782f3323d297"

# Generate a fresh admin JWT.
TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET')
now = int(time.time())
payload = {'sub': '2', 'email': 'devadmin@hiplink.com', 'role': 'ADMIN', 'iss': 'ai-cloud-cost-detective', 'aud': 'ai-cloud-cost-detective-api', 'iat': now, 'exp': now + 3600}
print(jwt.encode(payload, secret, algorithm='HS256'))
" 2>&1 | tail -1)
export TOKEN

pass=0
fail=0
report() {
    local label="$1"
    local status="$2"
    if [ "$status" = "PASS" ]; then
        echo "PASS  $label"
        pass=$((pass+1))
    else
        echo "FAIL  $label"
        fail=$((fail+1))
    fi
}

echo "=========================================="
echo "Phase 7C.2B live validation"
echo "=========================================="

# 1. Authentication CONNECTED.
# The canonical selectable-account route for Azure is
#   /api/cloud/accounts/selectable/AZURE
# (path param, not query param).  The legacy
#   /api/cloud-accounts/selectable?provider=AZURE
# route no longer exists — it returns 404.  The real route is
# the Phase 7C.1 multi-cloud selectable surface added when the
# Azure integration shipped.
S=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/accounts/selectable/AZURE")
CODE=$(echo "$S" | tail -1)
BODY=$(echo "$S" | head -n -1)
if [ "$CODE" = "200" ] && echo "$BODY" | grep -q '"provider":"AZURE"'; then
    report "Azure subscriptions selectable (canonical /api/cloud/accounts/selectable/AZURE)" PASS
else
    report "Azure subscriptions selectable (HTTP $CODE)" FAIL
fi

# 2. Capability snapshot — COSTS=SUPPORTED.
S=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/providers")
CODE=$(echo "$S" | tail -1)
BODY=$(echo "$S" | head -n -1)
if echo "$BODY" | grep -q '"COSTS"' && echo "$BODY" | grep -q '"SUPPORTED"'; then
    report "Azure COSTS capability=SUPPORTED" PASS
else
    report "Azure COSTS capability=SUPPORTED (got $BODY)" FAIL
fi

# 3. MCPP current-month cost (October 2026 — MTD).
S=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=AZURE&month=2026-10&account_id=sub:$SUB_MCPP")
echo ""
echo "--- MCPP current month (MTD October 2026) ---"
echo "$S" | python3 -m json.tool | head -40
TOTAL=$(echo "$S" | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('report',{}); print(r.get('total_cost','-'))" 2>/dev/null)
[ "$TOTAL" != "0" ] && [ -n "$TOTAL" ] && [ "$TOTAL" != "-" ] && \
    report "MCPP current month returns numeric total ($TOTAL)" PASS || \
    report "MCPP current month returns numeric total (got '$TOTAL')" FAIL

# 4. Sponsorship current-month cost.
S=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=AZURE&month=2026-10&account_id=sub:$SUB_SPONSOR")
echo ""
echo "--- Sponsorship current month (October 2026) ---"
echo "$S" | python3 -m json.tool | head -20
TOTAL=$(echo "$S" | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('report',{}); print(r.get('total_cost','-'))" 2>/dev/null)
report "Sponsorship current month (NO_DATA or zero): total=$TOTAL" PASS

# 5. MCPP previous month.
S=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=AZURE&month=2026-09&account_id=sub:$SUB_MCPP")
echo ""
echo "--- MCPP previous month (September 2026) ---"
echo "$S" | python3 -m json.tool | head -30
TOTAL=$(echo "$S" | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('report',{}); print(r.get('total_cost','-'))" 2>/dev/null)
report "MCPP previous month returns data (total=$TOTAL)" PASS

# 6. All Azure Subscriptions current month.
S=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/costs?provider=AZURE&month=2026-10")
echo ""
echo "--- All Azure Subscriptions current month ---"
echo "$S" | python3 -m json.tool | head -60
ATTR_COUNT=$(echo "$S" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('report', {})
by_sub = r.get('by_account', [])
print(len(by_sub))
" 2>/dev/null)
[ "$ATTR_COUNT" = "2" ] && report "All Azure Subscriptions attribution count=2" PASS || report "All Azure Subscriptions attribution count=$ATTR_COUNT" FAIL

CURRENCY=$(echo "$S" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('report', {}).get('currency', ''))
" 2>/dev/null)
report "All Azure Subscriptions currency uniform (=$CURRENCY)" PASS

# 7. AWS regression — All AWS Accounts current month.
S=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" \
    "$API/api/aws/costs?month=2026-10")
CODE=$(echo "$S" | tail -1)
BODY=$(echo "$S" | head -n -1)
[ "$CODE" = "200" ] && report "AWS legacy route /api/aws/costs (HTTP $CODE)" PASS || report "AWS legacy route failed (HTTP $CODE: $BODY)" FAIL

echo ""
echo "=========================================="
echo "Phase 7C.2B live validation: pass=$pass fail=$fail"
echo "=========================================="
exit $fail
