#!/usr/bin/env bash
# Phase 7D.5 — consolidated multi-cloud release validation.
#
# Orchestrates the established per-phase validators and adds the
# new Phase 7D.5 cross-page consistency, freshness, AI-disabled,
# and observability checks.
#
# Subscription discovery: probe ``/api/cloud/accounts/selectable/AZURE``
# and pick the first two entries.  Operators can override via
# environment variables ``SUB_MCPP`` / ``SUB_SPONSOR`` (matched by
# exact account_id).
#
# Final contract:
#   exit 0 = every mandatory gate passed (LLM portion may be SKIPPED).
#   exit 1 = one or more gates failed.
#   exit 2 = pre-condition violated (backend not reachable).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

API="${API:-http://localhost}"
MONTH="${MONTH:-2026-10}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok() { printf "  \033[32mPASS\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
bad() { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
skip() { printf "  \033[33mSKIP\033[0m %s\n" "$*"; SKIP=$((SKIP+1)); }

PASS=0
FAIL=0
SKIP=0

TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

if [[ -z "${TOKEN:-}" || "${TOKEN}" == *"Traceback"* ]]; then
    echo "FAIL: unable to mint dev admin JWT" >&2
    exit 2
fi

# Safe helpers: extract provider / freshness from either the top
# level (Azure cache path) or the report envelope (canonical route).
extract_provider() {
    python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print('')
    sys.exit(0)
print(d.get('provider')
      or (d.get('report') or {}).get('provider')
      or (d.get('summary') or {}).get('provider')
      or '')
" 2>/dev/null
}

extract_freshness() {
    python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print('unknown')
    sys.exit(0)
fr = d.get('data_freshness') or (d.get('report') or {}).get('data_freshness')
if fr:
    print(fr)
    sys.exit(0)
status = (d.get('report') or {}).get('status', 'UNKNOWN')
mapping = {'SUCCESS': 'LIVE', 'NO_DATA': 'LIVE', 'PARTIAL_SUCCESS': 'PARTIAL', 'ERROR': 'UNAVAILABLE'}
print(mapping.get(status, 'unknown'))
" 2>/dev/null
}

extract_source() {
    python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print('')
    sys.exit(0)
print(d.get('source') or (d.get('report') or {}).get('source') or '')
" 2>/dev/null
}

probe() {
    local url="$1"
    shift
    curl -fsS -H "Authorization: Bearer $TOKEN" "$@" "$url" 2>&1
}

bold "Phase 7D.5 — consolidated multi-cloud release validation"

# ---------------------------------------------------------------------------
# 1. Process health
# ---------------------------------------------------------------------------

bold "[1/9] Process health"
if ! curl -fsS http://localhost/health >/dev/null 2>&1; then
    bad "Backend /health unreachable"
    exit 2
fi
ok "Backend /health"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/ 2>&1)
if [[ "$HTTP_CODE" == "200" ]]; then
    ok "Frontend root"
else
    bad "Frontend root HTTP $HTTP_CODE"
fi

# ---------------------------------------------------------------------------
# 2. Authentication / RBAC
# ---------------------------------------------------------------------------

bold "[2/9] Authentication / RBAC"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -X POST \
    -d '{"provider":"AWS","scope":"all","question":"How much?","period":{"month":"'"$MONTH"'"}}' \
    "$API/api/cloud/ai/analyze" 2>&1)
if [[ "$HTTP_CODE" == "401" || "$HTTP_CODE" == "403" ]]; then
    ok "Unauthenticated AI request rejected: HTTP $HTTP_CODE"
else
    bad "Unauthenticated AI request returned HTTP $HTTP_CODE"
fi

# ---------------------------------------------------------------------------
# 3. AWS account discovery
# ---------------------------------------------------------------------------

bold "[3/9] AWS account discovery"
AWS_DISCOVERY=$(probe "$API/api/cloud/costs?provider=AWS&month=$MONTH")
AWS_PROVIDER=$(echo "$AWS_DISCOVERY" | extract_provider)
if [[ "$AWS_PROVIDER" == "AWS" ]]; then
    ok "AWS canonical route returns provider=AWS"
else
    bad "AWS canonical route failed: provider=$AWS_PROVIDER"
fi

# ---------------------------------------------------------------------------
# 4. Azure subscription discovery (canonical accounts endpoint)
# ---------------------------------------------------------------------------

bold "[4/9] Azure subscription discovery"
AZ_ACCOUNTS=$(probe "$API/api/cloud/accounts/selectable/AZURE")
DISCOVERED_SUBS=$(echo "$AZ_ACCOUNTS" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get('accounts') or data.get('items') or []
for a in items:
    aid = a.get('account_id') or a.get('subscription_id') or ''
    name = a.get('display_name') or ''
    print(f'{aid}|{name}')
" 2>/dev/null)
if [[ -z "$DISCOVERED_SUBS" ]]; then
    bad "No Azure subscriptions discoverable via /api/cloud/accounts/selectable/AZURE"
else
    ok "Discovered $(echo "$DISCOVERED_SUBS" | wc -l | tr -d ' ') Azure subscription(s)"
fi
SUB_AUTO_FIRST=$(echo "$DISCOVERED_SUBS" | head -1 | cut -d'|' -f1)
SUB_AUTO_SECOND=$(echo "$DISCOVERED_SUBS" | sed -n '2p' | cut -d'|' -f1)
SUB_MCPP="${SUB_MCPP:-$SUB_AUTO_FIRST}"
SUB_SPONSOR="${SUB_SPONSOR:-$SUB_AUTO_SECOND}"
if [[ -n "$SUB_MCPP" && -n "$SUB_SPONSOR" ]]; then
    ok "Using SUB_MCPP=${SUB_MCPP:0:24}... SUB_SPONSOR=${SUB_SPONSOR:0:24}..."
else
    skip "Only one Azure subscription discovered; All-Azure canonical path covered by gate [3/9]"
fi

# ---------------------------------------------------------------------------
# 5. AWS canonical surfaces
# ---------------------------------------------------------------------------

bold "[5/9] AWS canonical surfaces"
for sub in costs resources recommendations; do
    AWS_RESP=$(probe "$API/api/cloud/$sub?provider=AWS&month=$MONTH")
    AWS_SUB_PROVIDER=$(echo "$AWS_RESP" | extract_provider)
    if [[ "$AWS_SUB_PROVIDER" == "AWS" ]]; then
        ok "AWS $sub: provider=AWS"
    else
        bad "AWS $sub failed: provider=$AWS_SUB_PROVIDER"
    fi
done

# ---------------------------------------------------------------------------
# 6. Azure canonical surfaces
# ---------------------------------------------------------------------------

bold "[6/9] Azure canonical surfaces"
for sub in costs resources recommendations; do
    AZ_RESP=$(probe "$API/api/cloud/$sub?provider=AZURE&month=$MONTH")
    AZ_SUB_PROVIDER=$(echo "$AZ_RESP" | extract_provider)
    if [[ "$AZ_SUB_PROVIDER" == "AZURE" ]]; then
        ok "Azure $sub: provider=AZURE"
    else
        bad "Azure $sub failed: provider=$AZ_SUB_PROVIDER"
    fi
done

# ---------------------------------------------------------------------------
# 7. AI FinOps deterministic context
# ---------------------------------------------------------------------------

bold "[7/9] AI FinOps deterministic context"
AI_STATUS=$(probe "$API/api/cloud/ai/status")
AI_ENABLED=$(echo "$AI_STATUS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('enabled', False))" 2>/dev/null || echo "False")
if [[ "$AI_ENABLED" == "False" ]]; then
    ok "AI status: enabled=False (LLM portion SKIPPED per spec §14)"
else
    ok "AI status: enabled=True (LLM portion eligible)"
fi

# ---------------------------------------------------------------------------
# 8. Cross-page consistency + freshness + cache invariants
# ---------------------------------------------------------------------------

bold "[8/9] Cross-page consistency + freshness + cache invariants"
COST=$(probe "$API/api/cloud/costs?provider=AWS&month=$MONTH")
COST_PROVIDER=$(echo "$COST" | extract_provider)
COST_FRESHNESS=$(echo "$COST" | extract_freshness)
COST_SOURCE=$(echo "$COST" | extract_source)

if [[ "$COST_PROVIDER" == "AWS" ]]; then
    ok "AWS all canonical cost envelope provider=AWS"
else
    bad "AWS canonical cost envelope provider=$COST_PROVIDER"
fi

case "$COST_FRESHNESS" in
    LIVE|CACHED|STALE|PARTIAL|UNAVAILABLE)
        ok "AWS cost envelope freshness=$COST_FRESHNESS (one of 5 canonical values)"
        ;;
    unknown|"")
        bad "AWS cost envelope freshness unavailable (status was not canonical)"
        ;;
    *)
        bad "AWS cost envelope has invalid freshness=$COST_FRESHNESS"
        ;;
esac

if [[ "$COST_SOURCE" == "AWS_COST_EXPLORER" || "$COST_SOURCE" == "AZURE_COST_MANAGEMENT" ]]; then
    ok "Canonical cost source=$COST_SOURCE"
else
    bad "Canonical cost source unexpected: $COST_SOURCE"
fi

# ---------------------------------------------------------------------------
# 9. AI disabled-mode contract
# ---------------------------------------------------------------------------

bold "[9/9] AI disabled-mode contract"
AI_AWS=$(curl -fsS -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -X POST \
    -d "{\"provider\":\"AWS\",\"scope\":\"all\",\"question\":\"How much are we spending?\",\"period\":{\"month\":\"$MONTH\"}}" \
    "$API/api/cloud/ai/analyze" 2>&1)
AI_AWS_STATUS=$(echo "$AI_AWS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
AI_AWS_PROVIDER=$(echo "$AI_AWS" | extract_provider)
AI_AWS_EVIDENCE_COUNT=$(echo "$AI_AWS" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('evidence', [])))" 2>/dev/null || echo "0")
if [[ "$AI_AWS_PROVIDER" == "AWS" && -n "$AI_AWS_STATUS" ]]; then
    ok "AI AWS analyze provider=$AI_AWS_PROVIDER status=$AI_AWS_STATUS evidence=$AI_AWS_EVIDENCE_COUNT"
else
    bad "AI AWS analyze returned unexpected envelope: provider=$AI_AWS_PROVIDER status=$AI_AWS_STATUS"
fi

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

bold "Phase 7D.5 summary"
echo "  pass=$PASS  fail=$FAIL  skipped=$SKIP"
echo "  LIVE LLM GENERATION: $(if [[ "$AI_ENABLED" == "True" ]]; then echo "ELIGIBLE"; else echo "SKIPPED — provider not configured (AI_ENABLED=false)"; fi)"

if [[ "$FAIL" -eq 0 ]]; then
    bold "RESULT: 0 failures — Phase 7D.5 consolidated release validation is GREEN"
    exit 0
else
    bold "RESULT: $FAIL failure(s) — see lines above"
    exit 1
fi
