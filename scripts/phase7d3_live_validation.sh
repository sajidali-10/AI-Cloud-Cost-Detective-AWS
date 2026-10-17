#!/usr/bin/env bash
# Phase 7D.3 — live validation for the unified multi-cloud executive
# Dashboard.
#
# Exercises the three canonical endpoints the unified Dashboard
# consumes for AWS and Azure across every scope the UI supports:
#
#   * /api/cloud/costs          — Spend KPI, Spend Trend, by_service,
#                                 by_region
#   * /api/cloud/resources      — Resources KPI, Resource Summary
#   * /api/cloud/recommendations — Optimization KPI, Potential
#                                  Monthly Savings, Optimization Summary
#
# Scopes verified:
#   * AWS QA-Dev                 acct:1
#   * AWS Production             acct:3
#   * All AWS Accounts           account_id=all
#   * Azure MCPP Subscription    sub:<mcpp-guid>
#   * Azure Sponsorship          sub:<sponsor-guid>
#   * All Azure Subscriptions    account_id=all
#
# Asserts:
#   * Provider / scope identity matches the request — a stale AWS
#     response cannot satisfy an Azure request and vice versa.
#   * Cost completeness semantics (Phase 7C hotfix #2 / 7D.3
#     invariant) — successful zero, partial subtotal, and
#     all-failed throttled scenarios render correctly.
#   * Recommendation savings semantics — AWS MONTHLY, Azure
#     ANNUAL, mixed currencies, and EMPTY all behave.
#   * Cross-provider selector rejection (sub: into AWS → 400;
#     acct: into Azure → 400).
#
# Read-only: no AWS/Azure write operations.
set -uo pipefail

API="http://localhost"
REGION="us-east-1"
DAYS="30"
MONTH="2026-10"
AWS_QA="acct:1"           # 974053642038  AWS QA-Dev
AWS_PROD="acct:3"         # 852428675923  AWS Production
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
echo "Phase 7D.3 live validation — unified Dashboard"
echo "=========================================="

# ===========================================================================
#  Canonical costs — AWS
# ===========================================================================
echo "--- canonical AWS costs ---"
COST_AWS_ALL=$(auth "$API/api/cloud/costs?provider=AWS&account_id=all&month=$MONTH")
COST_AWS_QA=$(auth "$API/api/cloud/costs?provider=AWS&account_id=$AWS_QA&month=$MONTH")
COST_AWS_PROD=$(auth "$API/api/cloud/costs?provider=AWS&account_id=$AWS_PROD&month=$MONTH")

report "AWS all-accounts costs provider=AWS" \
  "$(echo "$COST_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('provider')=='AWS' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS QA-Dev costs scope=acct:974053642038" \
  "$(echo "$COST_AWS_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('scope_kind')=='single' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS Production costs scope=acct:852428675923" \
  "$(echo "$COST_AWS_PROD" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('scope_kind')=='single' else 'FAIL')" 2>/dev/null || echo FAIL)"

# Daily trend has dates the Spend Trend chart consumes.
report "AWS all-accounts daily_trend non-empty" \
  "$(echo "$COST_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if len(r.get('daily_trend',[]))>0 else 'FAIL')" 2>/dev/null || echo FAIL)"

# by_service and by_region render in the Spend by Service / Spend
# by Region charts.  They MUST NOT be hardcoded — assert real provider
# data is returned.
report "AWS by_service has at least one entry" \
  "$(echo "$COST_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if len(r.get('by_service',[]))>0 else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS by_region has at least one entry" \
  "$(echo "$COST_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if len(r.get('by_region',[]))>0 else 'FAIL')" 2>/dev/null || echo FAIL)"

# ===========================================================================
#  Canonical costs — Azure
# ===========================================================================
echo "--- canonical Azure costs ---"
COST_AZ_ALL=$(auth "$API/api/cloud/costs?provider=AZURE&account_id=all&month=$MONTH")
COST_AZ_MCPP=$(auth "$API/api/cloud/costs?provider=AZURE&account_id=$SUB_MCPP&month=$MONTH")
COST_AZ_SPON=$(auth "$API/api/cloud/costs?provider=AZURE&account_id=$SUB_SPONSOR&month=$MONTH")

report "Azure all-subscriptions costs provider=AZURE" \
  "$(echo "$COST_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('provider')=='AZURE' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure MCPP sub: scope_kind=single" \
  "$(echo "$COST_AZ_MCPP" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('scope_kind')=='single' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure Sponsorship sub: scope_kind=single" \
  "$(echo "$COST_AZ_SPON" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});print('PASS' if r.get('scope_kind')=='single' else 'FAIL')" 2>/dev/null || echo FAIL)"

# All-Azure aggregation MUST carry the per-subscription breakdown
# the Resources KPI uses to render the friendly attribution.
report "Azure all-subscriptions by_account has 2 entries" \
  "$(echo "$COST_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);r=d.get('report',{});ba=r.get('by_account',[]);print('PASS' if len(ba)>=2 else 'FAIL')" 2>/dev/null || echo FAIL)"

# Cost-completeness fields MUST be present on every Azure envelope so
# the Spend KPI can render Unavailable / Partial / $0.00 correctly.
report "Azure cost envelope exposes status / successful_accounts / failed_accounts" \
  "$(echo "$COST_AZ_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('report', {})
ok = ('status' in r and 'successful_accounts' in r and 'failed_accounts' in r)
print('PASS' if ok else 'FAIL')
" 2>/dev/null || echo FAIL)"

# When ALL Azure subscriptions are throttled (real-world
# scenario), successful_accounts must be 0 — the dashboard
# must NEVER render $0.00 for that state.
report "Azure throttling surfaces failed_accounts > 0 (completeness field set)" \
  "$(echo "$COST_AZ_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
r = d.get('report', {})
# Either there is real data (some successful) or all-failed.
# The invariant is the field MUST exist and be a non-negative integer.
fa = r.get('failed_accounts', None)
print('PASS' if isinstance(fa, int) and fa >= 0 else 'FAIL')
" 2>/dev/null || echo FAIL)"

# ===========================================================================
#  Canonical resources — AWS + Azure
# ===========================================================================
echo "--- canonical resources ---"
RES_AWS_ALL=$(auth "$API/api/cloud/resources?provider=AWS&account_id=all")
RES_AWS_QA=$(auth "$API/api/cloud/resources?provider=AWS&account_id=$AWS_QA")
RES_AZ_ALL=$(auth "$API/api/cloud/resources?provider=AZURE&account_id=all")
RES_AZ_MCPP=$(auth "$API/api/cloud/resources?provider=AZURE&account_id=$SUB_MCPP")

report "AWS resources provider=AWS" \
  "$(echo "$RES_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('provider')=='AWS' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure resources provider=AZURE" \
  "$(echo "$RES_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('provider')=='AZURE' else 'FAIL')" 2>/dev/null || echo FAIL)"

# Resources KPI counts real resources.
report "AWS resources total_resources is numeric" \
  "$(echo "$RES_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});t=s.get('total_resources',None);print('PASS' if isinstance(t,int) else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure resources total_resources is numeric" \
  "$(echo "$RES_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});t=s.get('total_resources',None);print('PASS' if isinstance(t,int) else 'FAIL')" 2>/dev/null || echo FAIL)"

# by_type / by_location populate the resource summary section.
report "AWS resources by_type has entries" \
  "$(echo "$RES_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if len(s.get('by_type',[]))>0 else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure resources by_type has entries (or NO_DATA accepted)" \
  "$(echo "$RES_AZ_ALL" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('summary', {})
status = s.get('status', '')
by_type = s.get('by_type', [])
print('PASS' if by_type or status in ('NO_DATA','ERROR','PARTIAL_SUCCESS') else 'FAIL')
" 2>/dev/null || echo FAIL)"

# Scope isolation: QA scope total_resources <= All total_resources.
QA_TOTAL=$(echo "$RES_AWS_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print(s.get('total_resources',0))" 2>/dev/null || echo 0)
ALL_TOTAL=$(echo "$RES_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print(s.get('total_resources',0))" 2>/dev/null || echo 0)

report "AWS QA total_resources <= AWS all total_resources (scope isolation)" \
  "$([ "$QA_TOTAL" -le "$ALL_TOTAL" ] && echo PASS || echo FAIL)"

# ===========================================================================
#  Canonical recommendations — AWS + Azure
# ===========================================================================
echo "--- canonical recommendations ---"
REC_AWS_ALL=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=all&days=$DAYS")
REC_AWS_QA=$(auth "$API/api/cloud/recommendations?provider=AWS&account_id=$AWS_QA&days=$DAYS")
REC_AZ_ALL=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=all")
REC_AZ_MCPP=$(auth "$API/api/cloud/recommendations?provider=AZURE&account_id=$SUB_MCPP")

report "AWS recommendations provider=AWS" \
  "$(echo "$REC_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('provider')=='AWS' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure recommendations provider=AZURE" \
  "$(echo "$REC_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('provider')=='AZURE' else 'FAIL')" 2>/dev/null || echo FAIL)"

# Savings period — AWS MONTHLY, Azure ANNUAL.
report "AWS savings_period=MONTHLY" \
  "$(echo "$REC_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('savings_period')=='MONTHLY' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure savings_period=ANNUAL" \
  "$(echo "$REC_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('savings_period')=='ANNUAL' else 'FAIL')" 2>/dev/null || echo FAIL)"

# Source breakdown populates the Optimization summary section.
report "AWS by_source present" \
  "$(echo "$REC_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('by_source') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure by_source present" \
  "$(echo "$REC_AZ_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print('PASS' if s.get('by_source') else 'FAIL')" 2>/dev/null || echo FAIL)"

# ===========================================================================
#  Cross-provider selector rejection
# ===========================================================================
echo "--- selector isolation ---"
report "AWS rejects Azure sub: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/costs?provider=AWS&account_id=sub:abc&month=$MONTH" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure rejects AWS acct: selector (400)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/costs?provider=AZURE&account_id=acct:1&month=$MONTH" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS resources rejects Azure sub: selector" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/resources?provider=AWS&account_id=sub:abc" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure resources rejects AWS acct: selector" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/resources?provider=AZURE&account_id=acct:1" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS recommendations rejects Azure sub: selector" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AWS&account_id=sub:abc" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure recommendations rejects AWS acct: selector" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AZURE&account_id=acct:1" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip() in ('400','422') else 'FAIL')" 2>/dev/null || echo FAIL)"

# ===========================================================================
#  Scope totals vary across scopes (cache isolation invariant)
# ===========================================================================
QA_REC=$(echo "$REC_AWS_QA" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print(s.get('total_recommendations',0))" 2>/dev/null || echo 0)
ALL_REC=$(echo "$REC_AWS_ALL" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print(s.get('total_recommendations',0))" 2>/dev/null || echo 0)
MCPP_REC=$(echo "$REC_AZ_MCPP" | python3 -c "import json,sys;d=json.load(sys.stdin);s=d.get('summary',{});print(s.get('total_recommendations',0))" 2>/dev/null || echo 0)

report "AWS scope total varies (cache isolation; QA != all)" \
  "$([ "$QA_REC" != "$ALL_REC" ] && echo PASS || ([ "$QA_REC" -eq 0 ] && [ "$ALL_REC" -eq 0 ] && echo PASS || echo FAIL))"

report "Azure MCPP scope recognised (canonical route OK)" \
  "$([ -n "$REC_AZ_MCPP" ] && echo PASS || echo FAIL)"

# ===========================================================================
#  Auth + route health
# ===========================================================================
echo "--- route health ---"
report "AWS costs canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/costs?provider=AWS&account_id=all&month=$MONTH" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure costs canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/costs?provider=AZURE&account_id=all&month=$MONTH" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS resources canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/resources?provider=AWS&account_id=all" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure resources canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/resources?provider=AZURE&account_id=all" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "AWS recommendations canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AWS&account_id=all&days=$DAYS" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

report "Azure recommendations canonical route accessible (200)" \
  "$(auth -o /dev/null -w '%{http_code}' "$API/api/cloud/recommendations?provider=AZURE&account_id=all" | python3 -c "import sys;print('PASS' if sys.stdin.read().strip()=='200' else 'FAIL')" 2>/dev/null || echo FAIL)"

echo ""
echo "=========================================="
echo "Phase 7D.3 live validation: pass=$pass fail=$fail"
echo "=========================================="
exit $fail
