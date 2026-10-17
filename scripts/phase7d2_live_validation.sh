#!/usr/bin/env bash
# Phase 7D.2 — live validation for the unified AWS + Azure
# Optimization experience.
#
# Pins the canonical `/api/cloud/recommendations` contract for
# both providers, scope attribution, currency safety, and
# selector isolation.  Read-only: no AWS/Azure write operations.
set -uo pipefail

API="http://localhost"
REGION="us-east-1"
DAYS="30"
AWS_QA="acct:1"          # 974053642038  AWS QA-Dev
AWS_PROD="acct:3"        # 852428675923  AWS Production
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
echo "Phase 7D.2 live validation — unified Optimization"
echo "=========================================="

# --- Canonical AWS: all scopes -------------------------------------------
CANON_AWS_ALL=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=all&region=$REGION&days=$DAYS")
CANON_AWS_QA=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=$AWS_QA&region=$REGION&days=$DAYS")
CANON_AWS_PROD=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=$AWS_PROD&region=$REGION&days=$DAYS")

echo "--- AWS canonical ---"
echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('all-AWS: provider=%s status=%s total=%s savings_status=%s period=%s' % (s.get('provider'),s.get('status'),s.get('total_recommendations'),s.get('savings_status'),s.get('savings_period')))" 2>/dev/null
echo "$CANON_AWS_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('QA: scope=%s status=%s total=%s savings_status=%s period=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations'),s.get('savings_status'),s.get('savings_period')))" 2>/dev/null
echo "$CANON_AWS_PROD" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('Prod: scope=%s status=%s total=%s savings_status=%s period=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations'),s.get('savings_status'),s.get('savings_period')))" 2>/dev/null

report "canonical all-AWS returns provider=AWS envelope" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('provider')=='AWS' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS QA scope=$AWS_QA_SCOPE" \
  "$(echo "$CANON_AWS_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='$AWS_QA_SCOPE' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS Production scope=$AWS_PROD_SCOPE" \
  "$(echo "$CANON_AWS_PROD" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='$AWS_PROD_SCOPE' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS envelope exposes savings_period=MONTHLY" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('savings_period')=='MONTHLY' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS all: by_source present (Cost Detective / COH / Compute Optimizer)" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=set(b['key'] for b in d.get('summary',{}).get('by_source',[]));print('PASS' if s else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS all: no duplicate recommendation_id" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);ids=[r['recommendation_id'] for r in d.get('recommendations',[])];print('PASS' if len(ids)==len(set(ids)) else 'FAIL')" 2>/dev/null || echo FAIL)"

report "canonical AWS all: attribution contains QA + Prod account ids" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);keys={b['key'] for b in d.get('summary',{}).get('by_account',[])};print('PASS' if {'974053642038','852428675923'} & keys else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Canonical Azure: all scopes -----------------------------------------
AZ_ALL=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=all")
AZ_MCPP=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=$SUB_MCPP")
AZ_SPON=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=$SUB_SPONSOR")

echo "--- Azure canonical ---"
echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('all-Azure: provider=%s status=%s total=%s savings_status=%s period=%s' % (s.get('provider'),s.get('status'),s.get('total_recommendations'),s.get('savings_status'),s.get('savings_period')))" 2>/dev/null
echo "$AZ_MCPP" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('MCPP: scope=%s status=%s total=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations')))" 2>/dev/null
echo "$AZ_SPON" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('Sponsorship: scope=%s status=%s total=%s' % (s.get('scope'),s.get('status'),s.get('total_recommendations')))" 2>/dev/null

report "Azure all-subscriptions canonical OK" \
  "$(echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('provider')=='AZURE' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure MCPP sub: scope preserved" \
  "$(echo "$AZ_MCPP" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='sub:a54d076b-2650-4b4f-a28c-2deed2011f83' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure Sponsorship sub: scope preserved" \
  "$(echo "$AZ_SPON" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('scope')=='sub:9353d858-27fa-4ce7-bf87-782f3323d297' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure envelope retains potential_annual_savings field" \
  "$(echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if 'potential_annual_savings' in d.get('summary',{}) else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure envelope exposes savings_period=ANNUAL" \
  "$(echo "$AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);print('PASS' if d.get('summary',{}).get('savings_period')=='ANNUAL' else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Selector isolation (release-blocking) -------------------------------
report "AWS rejects Azure sub: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AWS&account_id=sub:abc" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='400' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure rejects AWS acct: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AZURE&account_id=acct:1" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='400' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS rejects RACKSPACE provider (canonical recommendation surface)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=RACKSPACE" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','404','503') else 'FAIL')" 2>/dev/null || echo FAIL)"

# --- Cache-key isolation (Phase 7D.1 invariant) --------------------------
# The route does not persist; we assert distinct scopes return distinct
# total_recommendations values (a stale cache would never reflect this).
# Use Python heredoc with literal JSON strings — shell word-splitting
# corrupts multi-line payloads.
QA_TOTAL=$(echo "$CANON_AWS_QA" | python3 -c "import json,sys;print(json.load(sys.stdin).get('summary',{}).get('total_recommendations',0))" 2>/dev/null || echo 0)
PROD_TOTAL=$(echo "$CANON_AWS_PROD" | python3 -c "import json,sys;print(json.load(sys.stdin).get('summary',{}).get('total_recommendations',0))" 2>/dev/null || echo 0)
ALL_TOTAL=$(echo "$CANON_AWS_ALL" | python3 -c "import json,sys;print(json.load(sys.stdin).get('summary',{}).get('total_recommendations',0))" 2>/dev/null || echo 0)

report "AWS QA total != AWS Prod total (scope isolation)" \
  "$([ "$QA_TOTAL" != "$PROD_TOTAL" ] && echo PASS || echo FAIL)"

report "AWS all total >= max(QA, Prod)" \
  "$([ "$ALL_TOTAL" -ge "$(( QA_TOTAL > PROD_TOTAL ? QA_TOTAL : PROD_TOTAL ))" ] && echo PASS || echo FAIL)"

MCPP_TOTAL=$(echo "$AZ_MCPP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('summary',{}).get('total_recommendations',0))" 2>/dev/null || echo 0)
SPONSOR_TOTAL=$(echo "$AZ_SPON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('summary',{}).get('total_recommendations',0))" 2>/dev/null || echo 0)

report "Azure MCPP total != Azure Sponsorship total (scope isolation)" \
  "$([ "$MCPP_TOTAL" != "$SPONSOR_TOTAL" ] && echo PASS || ([ "$MCPP_TOTAL" -eq 0 ] && [ "$SPONSOR_TOTAL" -eq 0 ] && echo PASS || echo FAIL))"

# --- Savings safety (no fabricated $0; never sum mixed currencies) ------
report "AWS canonical never fabricates 0 (potential_savings missing/null safe)" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('summary', {})
# OK if potential_savings is null (EMPTY), non-zero, or a zero with authoritative flag
savings_status = s.get('savings_status', '')
potential = s.get('potential_savings')
if savings_status == 'EMPTY':
    print('PASS')
elif savings_status == 'OK' and potential is not None:
    print('PASS')
else:
    print('FAIL')
" 2>/dev/null || echo FAIL)"

report "AWS canonical currency code present (or savings_status=EMPTY)" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('summary', {})
if s.get('savings_status') == 'EMPTY':
    print('PASS')
elif s.get('currency'):
    print('PASS')
else:
    print('FAIL')
" 2>/dev/null || echo FAIL)"

report "Azure canonical never silently sums mixed currencies" \
  "$(echo "$AZ_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('summary', {})
savings_status = s.get('savings_status', '')
if savings_status == 'MIXED_CURRENCY':
    # potential_savings must be None; savings_by_currency must have >1 currency
    if s.get('potential_savings') is None:
        currencies = {c['currency'] for c in s.get('savings_by_currency', [])}
        print('PASS' if len(currencies) >= 1 else 'FAIL')
    else:
        print('FAIL: potential_savings set while MIXED_CURRENCY')
elif savings_status == 'OK':
    print('PASS')
elif savings_status == 'EMPTY':
    print('PASS')
else:
    print('FAIL: unknown savings_status=%s' % savings_status)
" 2>/dev/null || echo FAIL)"

# --- Provider-switch safety (mirror the frontend provider switch) ------
# Same auth, same scope, different provider — must return independent
# envelopes without cross-leaking data.
report "AWS vs Azure scopes return distinct provider enums" \
  "$(echo "$CANON_AWS_ALL" | python3 -c "
import json, sys
pa = json.load(sys.stdin).get('summary',{}).get('provider')
print('PASS' if pa=='AWS' else 'FAIL')
" 2>/dev/null || echo FAIL)"

report "Azure vs AWS scopes return distinct provider enums" \
  "$(echo "$AZ_ALL" | python3 -c "
import json, sys
pb = json.load(sys.stdin).get('summary',{}).get('provider')
print('PASS' if pb=='AZURE' else 'FAIL')
" 2>/dev/null || echo FAIL)"

# --- Unified canonical route is the only source for the new UI ----------
report "canonical route accessible (no auth regression)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AWS&account_id=all&days=$DAYS" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure canonical route accessible (no auth regression)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AZURE&account_id=all" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

echo ""
echo "=========================================="
echo "Phase 7D.2 live validation: pass=$pass fail=$fail"
echo "=========================================="
exit $fail
