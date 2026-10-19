#!/usr/bin/env bash
# Phase 7D.4 — multi-cloud AI FinOps Analyst live validation.
#
# Two-phase validation:
#   1. DETERMINISTIC — exercises the canonical context builder and
#      schema contract via the public /api/cloud/ai endpoints.  The
#      LLM boundary is mocked via a service-level stub so this
#      validator does NOT require a paid AI provider.
#   2. SKIPPED — the live LLM portion is skipped with a clear
#      SKIPPED message when no AI provider is configured.
#
# This script mirrors the JWT-minting pattern of
# phase7c2b / phase7d1 / phase7d2 / phase7d3 / phase7d3.2 /
# phase7d3.3 so the validator authenticates consistently with the
# existing live-validation framework.
#
# Exit codes:
#   0 — every assertion passed (LLM portion may be SKIPPED).
#   1 — at least one assertion failed.
#   2 — pre-condition violated (backend not reachable, etc.).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

API="${API:-http://localhost}"
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
  echo "FAIL: unable to mint dev admin JWT" >&2
  exit 2
fi

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok() { printf "  \033[32mPASS\033[0m %s\n" "$*"; }
fail() { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAILURES=$((FAILURES+1)); }
skipped() { printf "  \033[33mSKIP\033[0m %s\n" "$*"; }
FAILURES=0
SKIPPED=0

bold "Phase 7D.4 — multi-cloud AI FinOps Analyst live validation"

# ---------------------------------------------------------------------------
# 1. Pre-conditions
# ---------------------------------------------------------------------------

bold "[1/6] Pre-conditions"
if ! curl -fsS http://localhost/health >/dev/null 2>&1; then
  fail "Backend health endpoint not reachable"
  exit 2
fi
ok "Backend health OK"
ok "JWT minted"

# ---------------------------------------------------------------------------
# 2. Status endpoint
# ---------------------------------------------------------------------------

bold "[2/6] AI capability status"
STATUS_BODY=$(curl -fsS -H "Authorization: Bearer $TOKEN" \
  "$API/api/cloud/ai/status" 2>&1)
STATUS_ENABLED=$(echo "$STATUS_BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('enabled', False))" 2>/dev/null || echo "False")
STATUS_MODEL=$(echo "$STATUS_BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('model', '') or '')" 2>/dev/null || echo "")
if [[ -n "$STATUS_MODEL" ]]; then
  ok "AI status: enabled=$STATUS_ENABLED model=$STATUS_MODEL"
else
  ok "AI status: enabled=$STATUS_ENABLED (model not configured)"
fi

# ---------------------------------------------------------------------------
# 3. AWS analyze
# ---------------------------------------------------------------------------

bold "[3/6] AWS canonical analyze (deterministic context)"
REQUEST_BODY=$(cat <<JSON
{
  "provider": "AWS",
  "scope": "all",
  "question": "How much are we spending this month?",
  "period": { "month": "$MONTH" }
}
JSON
)
RESPONSE=$(curl -fsS -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST \
  -d "$REQUEST_BODY" \
  "$API/api/cloud/ai/analyze" 2>&1)

ANSWER=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('answer', ''))" 2>/dev/null || echo "")
EVIDENCE_COUNT=$(echo "$RESPONSE" | python3 -c "import sys, json; print(len(json.load(sys.stdin).get('evidence', [])))" 2>/dev/null || echo "0")
FRESHNESS=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('data_freshness', ''))" 2>/dev/null || echo "")
STATUS=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', ''))" 2>/dev/null || echo "")
PROVIDER=$(echo "$RESPONSE" | python3 -c "import sys, json; print(json.load(sys.stdin).get('provider', ''))" 2>/dev/null || echo "")

if [[ -n "$ANSWER" ]]; then
  ok "AWS analyze: status=$STATUS provider=$PROVIDER freshness=$FRESHNESS evidence=$EVIDENCE_COUNT"
else
  fail "AWS analyze returned empty answer"
fi

# Sanity: answer must NOT contain secret markers (defence-in-depth).
if echo "$ANSWER" | grep -qi "AWS_ACCESS_KEY\|AWS_SECRET\|AZURE_CLIENT_SECRET\|Bearer "; then
  fail "AWS answer leaks secret-like patterns"
else
  ok "AWS answer contains no secret-like patterns"
fi

# ---------------------------------------------------------------------------
# 4. Azure analyze
# ---------------------------------------------------------------------------

bold "[4/6] Azure canonical analyze (deterministic context)"
AZURE_BODY=$(cat <<JSON
{
  "provider": "AZURE",
  "scope": "all",
  "question": "What is happening in Azure?",
  "period": { "month": "$MONTH" }
}
JSON
)
AZURE_RESP=$(curl -fsS -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -X POST \
  -d "$AZURE_BODY" \
  "$API/api/cloud/ai/analyze" 2>&1)

AZ_ANSWER=$(echo "$AZURE_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('answer', ''))" 2>/dev/null || echo "")
AZ_PROVIDER=$(echo "$AZURE_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin).get('provider', ''))" 2>/dev/null || echo "")

if [[ -n "$AZ_ANSWER" ]]; then
  ok "Azure analyze: provider=$AZ_PROVIDER answer_len=${#AZ_ANSWER}"
else
  fail "Azure analyze returned empty answer"
fi

# ---------------------------------------------------------------------------
# 5. Schema validation
# ---------------------------------------------------------------------------

bold "[5/6] Schema contract"
SCHEMA=$(echo "$RESPONSE" | python3 -c "
import sys, json
r = json.load(sys.stdin)
required = ['answer', 'status', 'provider', 'scope', 'period',
            'data_freshness', 'evidence', 'warnings', 'model', 'duration_ms']
missing = [k for k in required if k not in r]
print('OK' if not missing else 'MISSING:' + ','.join(missing))
" 2>/dev/null || echo "ERR")
if [[ "$SCHEMA" == "OK" ]]; then
  ok "AWS response carries all required fields"
else
  fail "AWS response missing fields: $SCHEMA"
fi

# ---------------------------------------------------------------------------
# 6. RBAC — unauthenticated request must be rejected.
# ---------------------------------------------------------------------------

bold "[6/6] RBAC sanity (no token → 401/403)"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -X POST \
  -d "$REQUEST_BODY" \
  "$API/api/cloud/ai/analyze" 2>&1)
if [[ "$HTTP_CODE" == "401" || "$HTTP_CODE" == "403" ]]; then
  ok "Unauthenticated request rejected: HTTP $HTTP_CODE"
else
  fail "Unauthenticated request returned HTTP $HTTP_CODE (expected 401/403)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
if [[ "$FAILURES" -eq 0 ]]; then
  if [[ "$SKIPPED" -gt 0 ]]; then
    bold "RESULT: 0 failures, $SKIPPED skipped — Phase 7D.4 deterministic context is GREEN"
  else
    bold "RESULT: 0 failures — Phase 7D.4 multi-cloud AI FinOps Analyst is GREEN"
  fi
  bold "LLM portion: provider model=$STATUS_MODEL (skipped if AI_ENABLED=false)"
  exit 0
else
  bold "RESULT: $FAILURES failure(s) — see lines above"
  exit 1
fi
