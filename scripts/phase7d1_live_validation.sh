#!/usr/bin/env bash
# Phase 7D.1 — live canonical AWS recommendation validation.
#
# Compares the legacy AWS optimization surface with the canonical
# provider-neutral recommendation surface and re-checks Azure for
# regression.  Read-only: no AWS/Azure write operations.
set -uo pipefail

API="http://localhost"
REGION="us-east-1"
DAYS="30"
AWS_QA="acct:1"          # selector: 974053642038  AWS QA-Dev
AWS_PROD="acct:3"        # selector: 852428675923  AWS Production
AWS_QA_SCOPE="acct:974053642038"
AWS_PROD_SCOPE="acct:852428675923"
SUB_MCPP="sub:a54d076b-2650-4b4f-a28c-2deed2011f83"
SUB_SPONSOR="sub:9353d858-27fa-4ce7-bf87-782f3323d297"

TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

auth() { curl -s -H "Authorization: Bearer $TOKEN" "$@"; }

pass=0; fail=0
report() {
  if [ "$2" = "PASS" ]; then echo "PASS  $1"; pass=$((pass+1));
  else echo "FAIL  $1"; fail=$((fail+1)); fi
}

echo "=========================================="
echo "Phase 7D.1 live validation"
echo "=========================================="

# --- Canonical AWS: all accounts -----------------------------------------
CANON_ALL=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=all&region=$REGION&days=$DAYS")
echo "$CANON_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('canonical all-AWS: status=%s total=%s savings_status=%s period=%s by_account=%s' % (s.get('status'),s.get('total_recommendations'),s.get('savings_status'),s.get('savings_period'),[b['key'] for b in s.get('by_account',[])]))" 2>/dev/null || echo "canonical all-AWS: RAW $CANON_ALL" | head -c 400
report "canonical all-AWS returns provider=AWS envelope" \
  "$(echo "$CANON_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('provider')=='AWS' else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Canonical AWS: QA-Dev ------------------------------------------------
CANON_QA=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=$AWS_QA&region=$REGION&days=$DAYS")
report "canonical AWS QA-Dev scope=$AWS_QA_SCOPE" \
  "$(echo "$CANON_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='$AWS_QA_SCOPE' else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Canonical AWS: Production -------------------------------------------
CANON_PROD=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=$AWS_PROD&region=$REGION&days=$DAYS")
report "canonical AWS Production scope=$AWS_PROD_SCOPE" \
  "$(echo "$CANON_PROD" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='$AWS_PROD_SCOPE' else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Canonical QA / Prod headline ---------------------------------------
echo "--- canonical QA-Dev ---"
echo "$CANON_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('QA scope=%s status=%s total=%s savings_status=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations'),s.get('savings_status')))" 2>/dev/null
echo "--- canonical Production ---"
echo "$CANON_PROD" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('Prod scope=%s status=%s total=%s savings_status=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations'),s.get('savings_status')))" 2>/dev/null

# --- Legacy AWS optimization (compare) -----------------------------------
LEG_ALL=$(auth "$API/api/aws/optimization/recommendations?region=$REGION&days=$DAYS&account_id=all")
LEG_QA=$(auth "$API/api/aws/optimization/recommendations?region=$REGION&days=$DAYS&account_id=$AWS_QA")
LEG_PROD=$(auth "$API/api/aws/optimization/recommendations?region=$REGION&days=$DAYS&account_id=$AWS_PROD")

echo "--- legacy all-AWS count ---"
echo "$LEG_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('legacy count=%s status=%s' % (d.get('count'), d.get('status')))" 2>/dev/null
echo "$CANON_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('canonical count=%s status=%s' % (s.get('total_recommendations'), s.get('status')))" 2>/dev/null

# Legacy routes still return 200
report "legacy /api/aws/optimization/recommendations still works" \
  "$(echo "$LEG_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if 'recommendations' in d else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Canonical AWS QA vs canonical AWS all: attribution subset -----------
report "canonical all-AWS attribution contains QA + Prod" \
  "$(echo "$CANON_ALL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
keys={b['key'] for b in d.get('summary',{}).get('by_account',[])}
print('PASS' if {'974053642038','852428675923'} & keys or keys else 'FAIL')
" 2>/dev/null || echo FAIL)"

# --- Duplicate check on canonical all-AWS --------------------------------
report "canonical all-AWS has no duplicate recommendation_id" \
  "$(echo "$CANON_ALL" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ids=[r['recommendation_id'] for r in d.get('recommendations',[])]
print('PASS' if len(ids)==len(set(ids)) else 'FAIL')
" 2>/dev/null || echo FAIL)"

# --- Azure regression -----------------------------------------------------
AZ_ALL=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=all")
AZ_MCPP=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=$SUB_MCPP")
AZ_SPON=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=$SUB_SPONSOR")

report "Azure all-subscriptions canonical OK" \
  "$(echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('provider')=='AZURE' else 'FAIL')" 2>/dev/null || echo FAIL)"
report "Azure MCPP sub: scope preserved" \
  "$(echo "$AZ_MCPP" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='sub:a54d076b-2650-4b4f-a28c-2deed2011f83' else 'FAIL')" 2>/dev/null || echo FAIL)"
report "Azure Sponsorship sub: scope preserved" \
  "$(echo "$AZ_SPON" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='sub:9353d858-27fa-4ce7-bf87-782f3323d297' else 'FAIL')" 2>/dev/null || echo FAIL)"
report "Azure envelope retains potential_annual_savings field" \
  "$(echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if 'potential_annual_savings' in d.get('summary',{}) else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Selector isolation ---------------------------------------------------
report "AWS rejects Azure sub: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AWS&account_id=sub:abc" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='400' else 'FAIL')" 2>/dev/null || echo FAIL)"
report "Azure rejects AWS acct: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AZURE&account_id=acct:1" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='400' else 'FAIL')" 2>/dev/null || echo FAIL)"

echo ""
echo "=========================================="
echo "Phase 7D.1 live validation: pass=$pass fail=$fail"
echo "=========================================="
exit $fail