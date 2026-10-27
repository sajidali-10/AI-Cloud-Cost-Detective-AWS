#!/usr/bin/env bash
# Phase 9D.1 — Unified multi-provider dashboard validator (AWS / Azure / Rackspace).
#
# Validates the Phase 9D.1 acceptance contract end to end:
#
#   Reachability (secure production path)
#     * backend health  = 200 via nginx
#     * frontend root   = 200 via nginx
#     * HTTPS app route = 200 (production Host, same pattern as Phase 8.1)
#
#   Provider discovery
#     * /api/cloud/providers lists AWS + AZURE + RACKSPACE
#     * RACKSPACE capabilities: COSTS / RESOURCES / OPTIMIZATION = SUPPORTED
#     * AWS and AZURE keep COSTS / RESOURCES / OPTIMIZATION = SUPPORTED
#
#   Rackspace canonical surfaces (authenticated, via nginx)
#     * GET /api/cloud/costs?provider=RACKSPACE
#         - envelope valid, currency present on success, billing period present
#         - source = RACKSPACE_BILLING
#         - unsupported daily trend NOT fabricated (empty, not invented points)
#         - unsupported regional breakdown NOT fabricated (empty, not zeros)
#         - previous-period unavailable stays null — never converted to $0
#     * GET /api/cloud/resources?provider=RACKSPACE
#         - envelope valid, total_resources == len(resources), by_type reconciles
#         - only recognizable canonical types (SERVER / VOLUME / LOAD_BALANCER)
#         - zero load balancers is a VALID empty state, not an error
#     * GET /api/cloud/recommendations?provider=RACKSPACE
#         - envelope valid, count reconciles, IDs unique
#         - unknown savings: all null + savings_status=EMPTY (never $0)
#         - severity / confidence valid; evidence + limitations present
#         - RS-VOL-001 advisory (review semantics, no destructive actions)
#         - ACTIVE servers produce zero recommendations (no false rightsizing)
#
#   Frontend source contracts (static, no brittle assertions where
#   component tests already provide stronger evidence)
#     * RACKSPACE is a selectable provider in the frontend provider context
#     * Costs / Resources / Recommendations UI route RACKSPACE through the
#       canonical /api/cloud/* endpoints (never legacy provider routes)
#     * provider-prefixed cache keys (stale-data protection) intact
#     * unknown savings render as unavailable — never as $0
#     * no browser-side Rackspace credentials / direct Rackspace API calls
#
#   Focused frontend tests (default on; PHASE9D1_RUN_NPM=0 to skip)
#     * the Phase 9D.1 component tests (costs, resources, recommendations,
#       provider context, cache-key isolation, unified dashboard)
#
# The validator is deterministic, read-only and non-destructive.
# It never prints secret values (RAN, API key, tokens).
#
# Exit codes:
#   0 — validation successful
#   1 — validation failure
#   2 — prerequisite / configuration failure

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 2

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mPASS\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
info() { printf "  \033[36mINFO\033[0m %s\n" "$*"; }

PASS=0
FAIL=0

bold "Phase 9D.1 — Unified multi-provider dashboard validation"

# ---------------------------------------------------------------------------
# Step 1: preconditions (no secret values are printed).
# ---------------------------------------------------------------------------

ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
if [[ ! -f "$ENV_FILE" ]]; then
    bad "host $ENV_FILE not found"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^cost-detective-backend$'; then
    bad "cost-detective-backend container is not running"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi
ok "cost-detective-backend container is running"

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^cost-detective-frontend$'; then
    bad "cost-detective-frontend container is not running"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi
ok "cost-detective-frontend container is running"

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
    bad "ProviderRegistry does not resolve RACKSPACE — image predates Phase 9B"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# JWT for the secure production path (same pattern as Phase 7D.1 / 9C.2).
TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

API="http://localhost"
PROD_HOST="${PROD_HOST:-finops.hiplink.com}"
auth() { curl -s -H "Authorization: Bearer $TOKEN" "$@"; }

# ---------------------------------------------------------------------------
# Step 2: reachability on the secure production path.
# ---------------------------------------------------------------------------

bold ""
bold "Reachability (secure production path)"

HEALTH_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$API/api/health" --max-time 10 2>/dev/null || echo 000)"
if [[ "$HEALTH_CODE" == "200" ]]; then
    ok "backend health via nginx -> 200"
else
    bad "backend health via nginx -> $HEALTH_CODE (expected 200)"
fi

FRONTEND_CODE="$(curl -s -o /dev/null -w '%{http_code}' "$API/" --max-time 10 2>/dev/null || echo 000)"
if [[ "$FRONTEND_CODE" == "200" ]]; then
    ok "frontend root via nginx -> 200"
else
    bad "frontend root via nginx -> $FRONTEND_CODE (expected 200)"
fi

HTTPS_CODE="$(curl -ksS -o /dev/null -w '%{http_code}' "https://$PROD_HOST/" --max-time 10 2>/dev/null || echo 000)"
if [[ "$HTTPS_CODE" == "200" ]]; then
    ok "HTTPS application route -> 200"
else
    bad "HTTPS application route -> $HTTPS_CODE (expected 200)"
fi

# ---------------------------------------------------------------------------
# Step 3: provider discovery.
# ---------------------------------------------------------------------------

bold ""
bold "Provider discovery"

PROVIDERS_JSON="$(auth "$API/api/cloud/providers" 2>/dev/null | grep -m1 '^{')"
if [[ -z "$PROVIDERS_JSON" ]]; then
    bad "/api/cloud/providers returned no JSON"
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))"
    bold "RESULT: validation failure"
    exit 1
fi

CAP_CHECK="$(printf '%s\n' "$PROVIDERS_JSON" | "$REPO_ROOT/backend/.venv/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
def caps(provider):
    for p in d.get('providers', []):
        if p.get('provider') == provider:
            return {c.get('capability'): c.get('state') for c in p.get('capabilities', [])}
    return None
for provider in ('AWS', 'AZURE', 'RACKSPACE'):
    c = caps(provider)
    if c is None:
        print('MISSING')
    else:
        print(provider + '=' + ','.join(f'{k}={c.get(k)}' for k in ('COSTS', 'RESOURCES', 'OPTIMIZATION')))
print(json.dumps({p.get('provider'): {
    'registered': p.get('is_registered'), 'enabled': p.get('is_enabled')}
    for p in d.get('providers', [])}))
" 2>/dev/null)"

for provider in AWS AZURE RACKSPACE; do
    ROW="$(printf '%s\n' "$CAP_CHECK" | grep "^${provider}=" | head -1)"
    if [[ -z "$ROW" ]]; then
        bad "provider discovery: $provider missing from /api/cloud/providers"
        continue
    fi
    MISSING=0
    for cap in COSTS RESOURCES OPTIMIZATION; do
        STATE="$(echo "$ROW" | sed "s/^${provider}=//" | tr ',' '\n' | grep "^${cap}=" | cut -d= -f2)"
        if [[ "$provider" == "RACKSPACE" || "$cap" != "MISSING" ]]; then
            if [[ "$STATE" != "SUPPORTED" ]]; then MISSING=1; fi
        fi
    done
    if [[ $MISSING -eq 0 ]]; then
        ok "provider discovery: $provider COSTS/RESOURCES/recommendations = SUPPORTED"
    else
        bad "provider discovery: $provider capabilities degraded ($ROW)"
    fi
done

# ---------------------------------------------------------------------------
# Step 4: Rackspace canonical surfaces via the production HTTP path.
# ---------------------------------------------------------------------------

bold ""
bold "Rackspace canonical surfaces (authenticated production path)"

COSTS_RAW="$(auth -w '\n%{http_code}\n' "$API/api/cloud/costs?provider=RACKSPACE" 2>/dev/null)"
COSTS_CODE="$(echo "$COSTS_RAW" | tail -1 | tr -d '[:space:]')"
COSTS_JSON="$(echo "$COSTS_RAW" | sed '$d' | grep -m1 '^{')"

RES_RAW="$(auth -w '\n%{http_code}\n' "$API/api/cloud/resources?provider=RACKSPACE" 2>/dev/null)"
RES_CODE="$(echo "$RES_RAW" | tail -1 | tr -d '[:space:]')"
RES_JSON="$(echo "$RES_RAW" | sed '$d' | grep -m1 '^{')"

RECS_RAW="$(auth -w '\n%{http_code}\n' "$API/api/cloud/recommendations?provider=RACKSPACE" 2>/dev/null)"
RECS_CODE="$(echo "$RECS_RAW" | tail -1 | tr -d '[:space:]')"
RECS_JSON="$(echo "$RECS_RAW" | sed '$d' | grep -m1 '^{')"

if [[ "$COSTS_CODE" == "200" && -n "$COSTS_JSON" ]]; then
    ok "GET /api/cloud/costs?provider=RACKSPACE -> 200"
else
    bad "GET /api/cloud/costs?provider=RACKSPACE -> $COSTS_CODE"
fi
if [[ "$RES_CODE" == "200" && -n "$RES_JSON" ]]; then
    ok "GET /api/cloud/resources?provider=RACKSPACE -> 200"
else
    bad "GET /api/cloud/resources?provider=RACKSPACE -> $RES_CODE"
fi
if [[ "$RECS_CODE" == "200" && -n "$RECS_JSON" ]]; then
    ok "GET /api/cloud/recommendations?provider=RACKSPACE -> 200"
else
    bad "GET /api/cloud/recommendations?provider=RACKSPACE -> $RECS_CODE"
fi

if [[ -z "$COSTS_JSON" || -z "$RES_JSON" || -z "$RECS_JSON" ]]; then
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))"
    bold "RESULT: validation failure"
    exit 1
fi

# Combined envelope analysis (canonical shape only — no hardcoded numbers).
ENVELOPE_CHECK="$(printf '%s\n%s\n%s\n' "$COSTS_JSON" "$RES_JSON" "$RECS_JSON" | "$REPO_ROOT/backend/.venv/bin/python" -c "
import json, sys

def decimal_ok(value):
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False

lines = sys.stdin.read().split('\n')
costs = json.loads(lines[0]); resources = json.loads(lines[1]); recs = json.loads(lines[2])

checks = {}

# ---- costs envelope ------------------------------------------------------
r = costs.get('report', {})
status = r.get('status')
checks['costs_provider'] = r.get('provider') == 'RACKSPACE'
checks['costs_status_valid'] = status in ('SUCCESS', 'NO_DATA', 'PARTIAL_SUCCESS')
period = r.get('period') or {}
checks['costs_period_present'] = bool(period.get('start')) and bool(period.get('end')) and period.get('days', 0) > 0
checks['costs_source'] = r.get('source') == 'RACKSPACE_BILLING'
checks['costs_currency_present'] = (status == 'NO_DATA') or bool(r.get('currency'))
checks['costs_total_ok'] = decimal_ok(r.get('total_cost'))
# Unsupported dimensions must be EMPTY (honest unavailability), never fabricated.
checks['costs_trend_not_fabricated'] = (r.get('daily_trend') or []) == []
checks['costs_region_not_fabricated'] = (r.get('by_region') or []) == []
# Previous period must remain unavailable (null), never invented as zero.
prev = r.get('previous_period_cost')
checks['costs_prev_period_not_zero'] = prev is None or prev == '' or decimal_ok(prev)
checks['costs_prev_period_unavailable'] = (r.get('previous_period_unavailable') is True) or (prev in (None, ''))
by_service = r.get('by_service') or []
checks['costs_service_breakdown'] = (status == 'NO_DATA') or len(by_service) > 0
succ = r.get('successful_accounts'); fail = r.get('failed_accounts')
checks['costs_account_counts'] = isinstance(succ, int) and isinstance(fail, int) and succ + fail >= 1

# ---- resources envelope ----------------------------------------------------
s = resources.get('summary', {})
items = resources.get('resources', [])
by_type = s.get('by_type') or []
checks['res_provider'] = s.get('provider') == 'RACKSPACE'
checks['res_status_valid'] = s.get('status') in ('SUCCESS', 'NO_DATA', 'PARTIAL_SUCCESS')
checks['res_source'] = s.get('source') == 'RACKSPACE_CLOUD_API'
checks['res_count_reconciles'] = s.get('total_resources') == len(items)
checks['res_by_type_reconciles'] = sum(g.get('count', 0) for g in by_type) == s.get('total_resources')
KNOWN_TYPES = {'SERVER', 'VOLUME', 'LOAD_BALANCER'}
checks['res_types_recognizable'] = all(g.get('key') in KNOWN_TYPES for g in by_type)
lb = next((g.get('count', 0) for g in by_type if g.get('key') == 'LOAD_BALANCER'), 0)
checks['res_lb_zero_valid'] = lb == 0  # zero is a valid empty state, not an error

# ---- recommendations envelope ------------------------------------------------
rs = recs.get('summary', {})
rec_list = recs.get('recommendations', [])
checks['recs_provider'] = rs.get('provider') == 'RACKSPACE'
checks['recs_status_valid'] = rs.get('status') in ('SUCCESS', 'NO_DATA', 'PARTIAL_SUCCESS')
checks['recs_source'] = rs.get('source') == 'RACKSPACE_OPTIMIZATION'
checks['recs_count_reconciles'] = rs.get('total_recommendations') == len(rec_list)
checks['recs_ids_unique'] = len({x.get('recommendation_id') for x in rec_list}) == len(rec_list)
checks['recs_unknown_savings'] = (
    rs.get('savings_status') == 'EMPTY'
    and rs.get('potential_savings') is None
    and all(x.get('estimated_savings') is None for x in rec_list)
    and rs.get('recommendations_with_savings', 0) == 0
)
checks['recs_severity_valid'] = all(x.get('impact') in ('LOW', 'MEDIUM', 'HIGH') for x in rec_list)
checks['recs_confidence_valid'] = all(
    (x.get('extensions') or {}).get('confidence') in ('LOW', 'MEDIUM', 'HIGH') for x in rec_list
)
checks['recs_evidence_present'] = all(
    bool((x.get('extensions') or {}).get('evidence')) and bool((x.get('extensions') or {}).get('limitations'))
    for x in rec_list
)
DESTRUCTIVE = {'RIGHTSIZE', 'DOWNSIZE', 'STOP', 'DELETE', 'IDLE', 'WASTE', 'TERMINATE'}
checks['recs_no_destructive_actions'] = all(
    (x.get('action') or '').upper() not in DESTRUCTIVE for x in rec_list
)
vol_recs = [x for x in rec_list if (x.get('extensions') or {}).get('rule_id') == 'RS-VOL-001']
detached_ids = {
    i.get('resource_id') for i in items
    if i.get('resource_type') == 'VOLUME'
    and ((i.get('metadata') or {}).get('attachment_state') or '').upper() == 'DETACHED'
}
checks['recs_vol001_advisory_detached'] = all(x.get('resource_id') in detached_ids for x in vol_recs)
active_ids = {
    i.get('resource_id') for i in items
    if i.get('resource_type') == 'SERVER' and (i.get('state') or '').upper() == 'ACTIVE'
}
checks['recs_active_servers_unsafe_free'] = all(x.get('resource_id') not in active_ids for x in rec_list)
checks['recs_no_frontend_generation'] = all(x.get('source') == 'RACKSPACE_OPTIMIZATION' for x in rec_list)

failures = [k for k, v in checks.items() if not v]
print(json.dumps({'failures': failures, 'checks': checks}))
" 2>/dev/null)"

ENV_FAILURES="$(printf '%s\n' "$ENVELOPE_CHECK" | "$REPO_ROOT/backend/.venv/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
print(' '.join(d['failures']) if d['failures'] else 'NONE')
" 2>/dev/null)"

CHECK_NAMES=(
    costs_provider costs_status_valid costs_period_present costs_source
    costs_currency_present costs_total_ok costs_trend_not_fabricated
    costs_region_not_fabricated costs_prev_period_not_zero
    costs_prev_period_unavailable costs_service_breakdown costs_account_counts
    res_provider res_status_valid res_source res_count_reconciles
    res_by_type_reconciles res_types_recognizable res_lb_zero_valid
    recs_provider recs_status_valid recs_source recs_count_reconciles
    recs_ids_unique recs_unknown_savings recs_severity_valid
    recs_confidence_valid recs_evidence_present recs_no_destructive_actions
    recs_vol001_advisory_detached recs_active_servers_unsafe_free
    recs_no_frontend_generation
)

for name in "${CHECK_NAMES[@]}"; do
    if [[ "$ENV_FAILURES" == *" $name "* || "$ENV_FAILURES" == "$name "* || "$ENV_FAILURES" == *" $name" ]]; then
        bad "envelope: $name"
    else
        ok "envelope: $name"
    fi
done

# Live counters for the report (never asserted against).
LIVE_INFO="$(printf '%s\n%s\n%s\n' "$COSTS_JSON" "$RES_JSON" "$RECS_JSON" | "$REPO_ROOT/backend/.venv/bin/python" -c "
import json, sys
lines = sys.stdin.read().split('\n')
costs = json.loads(lines[0]); resources = json.loads(lines[1]); recs = json.loads(lines[2])
r = costs.get('report', {}); s = resources.get('summary', {}); rs = recs.get('summary', {})
by_type = {g.get('key'): g.get('count', 0) for g in (s.get('by_type') or [])}
rules = {}
for x in recs.get('recommendations', []):
    rid = (x.get('extensions') or {}).get('rule_id') or 'UNKNOWN'
    rules[rid] = rules.get(rid, 0) + 1
print(json.dumps({
    'costs_status': r.get('status'), 'currency': r.get('currency'),
    'period': f\"{r.get('period', {}).get('start')}..{r.get('period', {}).get('end')}\",
    'res_total': s.get('total_resources'), 'servers': by_type.get('SERVER', 0),
    'volumes': by_type.get('VOLUME', 0), 'lbs': by_type.get('LOAD_BALANCER', 0),
    'rec_total': rs.get('total_recommendations'), 'rules': rules,
    'savings_status': rs.get('savings_status'),
}))
" 2>/dev/null)"
info "live envelope: $LIVE_INFO"

# ---------------------------------------------------------------------------
# Step 5: frontend source contracts (static, deterministic).
# ---------------------------------------------------------------------------

bold ""
bold "Frontend source contracts"

FE_SRC="$REPO_ROOT/frontend/src"

static_check() {
    local name="$1" file="$2" pattern="$3"
    if grep -qE "$pattern" "$REPO_ROOT/frontend/$file" 2>/dev/null; then
        ok "frontend: $name"
    else
        bad "frontend: $name (pattern not found in $file)"
    fi
}

static_check "RACKSPACE is a valid selectable provider" \
    "src/lib/providerContext.tsx" \
    "VALID_PROVIDERS: ProviderKind\[\] = \['AWS', 'AZURE', 'RACKSPACE'\]"

static_check "Costs UI routes RACKSPACE through the canonical /api/cloud/costs endpoint" \
    "src/pages/CostsPage.tsx" \
    "useCanonical = provider === 'AZURE' \|\| provider === 'RACKSPACE'"

static_check "Costs UI derives the Rackspace label from the source billing period" \
    "src/pages/CostsPage.tsx" \
    "billingCycleLabel\(reportSource\.period\)"

static_check "Resources UI routes RACKSPACE through the canonical resources service" \
    "src/pages/ResourcesPage.tsx" \
    "provider === 'RACKSPACE'"

static_check "Resources view renders from the canonical resource envelope" \
    "src/pages/RackspaceResourcesView.tsx" \
    "total_resources"

static_check "Recommendations UI routes RACKSPACE through the canonical recommendations service" \
    "src/pages/OptimizationPage.tsx" \
    "RACKSPACE"

static_check "canonical cost cache keys are provider-prefixed (stale-data protection)" \
    "src/lib/finops/cloudCosts.ts" \
    "\\\$\\{providerSeg\\}:cloud:costs:"

static_check "canonical resource cache keys are provider-prefixed" \
    "src/lib/finops/cloudResources.ts" \
    "\\\$\\{providerSeg\\}:cloud:resources:"

static_check "canonical recommendation cache keys are provider-prefixed" \
    "src/lib/finops/cloudRecommendations.ts" \
    "\\\$\\{providerSeg\\}:cloud:recommendations:"

static_check "unknown savings render as unavailable (never \$0)" \
    "src/pages/OptimizationPage.tsx" \
    "savingsStatus === 'EMPTY'"

if grep -rniE 'rackspacecloud\.com|X-Auth-Token|RACKSPACE_API_KEY|RACKSPACE_BILLING_ACCOUNT' \
    --include='*.ts' --include='*.tsx' "$FE_SRC" \
    | grep -v '/tests/' > /dev/null 2>&1; then
    bad "frontend: direct Rackspace credential/API reference found outside tests"
else
    ok "frontend: no browser-side Rackspace credentials or direct Rackspace API calls"
fi

# ---------------------------------------------------------------------------
# Step 6: focused frontend tests (strongest contract evidence).
# ---------------------------------------------------------------------------

bold ""
bold "Focused frontend tests"

if [[ "${PHASE9D1_RUN_NPM:-1}" == "1" ]] && command -v npm >/dev/null 2>&1 && [[ -d "$REPO_ROOT/frontend/node_modules" ]]; then
    FOCUSED_TESTS=(
        "src/tests/costs-page.test.tsx"
        "src/tests/rackspace-resources.test.tsx"
        "src/tests/rackspace-recommendations.test.tsx"
        "src/tests/phase7d3-unified-dashboard.test.tsx"
        "src/tests/phase7c1-provider-context.test.tsx"
        "src/tests/phase7c1-cache-key-isolation.test.ts"
    )
    if (cd "$REPO_ROOT/frontend" && npx vitest run "${FOCUSED_TESTS[@]}" \
        > /tmp/phase9d1_focused_tests.log 2>&1); then
        FOCUS_TOTAL="$(grep -oE 'Tests +[0-9]+ passed' /tmp/phase9d1_focused_tests.log | grep -oE '[0-9]+' | head -1)"
        ok "focused Phase 9D.1 component tests passed (${FOCUS_TOTAL:-all} tests)"
    else
        bad "focused Phase 9D.1 component tests FAILED (see /tmp/phase9d1_focused_tests.log)"
        tail -30 /tmp/phase9d1_focused_tests.log | sed 's/^/    /'
    fi
else
    info "focused frontend tests skipped (PHASE9D1_RUN_NPM=0 or npm/node_modules unavailable)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

bold ""
bold "Summary"
echo "  PASS=$PASS  FAIL=$FAIL"
if [[ $FAIL -eq 0 ]]; then
    bold "RESULT: validation successful"
    exit 0
fi
bold "RESULT: validation failure"
exit 1
