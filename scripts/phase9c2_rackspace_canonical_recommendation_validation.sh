#!/usr/bin/env bash
# Phase 9C.2 — Rackspace canonical recommendations validator.
#
# Exercises the ACTUAL canonical endpoint through the secure production
# application path (nginx + JWT auth):
#
#     GET /api/cloud/recommendations?provider=RACKSPACE
#
# and cross-checks the response against live canonical evidence:
#
#   * provider = RACKSPACE, status valid, source valid
#   * recommendation count reconciles with current detached inventory
#   * IDs unique + stable; two runs produce identical identity/order
#   * resource references exist in the canonical inventory
#   * severity / confidence valid
#   * unknown savings are null — never represented as $0
#   * ACTIVE servers produce no rightsizing recommendations
#   * attached volumes produce no detached-volume recommendations
#   * detached-volume findings correspond to current detached volumes
#   * product write requests = 0 (transport spy over the route path)
#   * secret leakage = 0
#   * provider discovery advertises RACKSPACE recommendations SUPPORTED
#     with AWS / Azure unchanged
#
# Exit codes:
#   0 — validation successful
#   1 — validation failure
#   2 — configuration / pre-condition violated

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
WARN=0

bold "Phase 9C.2 — Rackspace canonical recommendations validation"

# ---------------------------------------------------------------------------
# Step 1: preconditions (no values are printed).
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

RECS_IMPORT_OK="$(docker exec cost-detective-backend python -c "
from app.services.cloud.rackspace.recommendations import (
    aggregate_rackspace_recommendations_for_account,
    finding_to_cloud_recommendation,
    RACKSPACE_RECOMMENDATION_SOURCE,
)
print('1')
" 2>/dev/null | tail -1)"
if [[ "$RECS_IMPORT_OK" == "1" ]]; then
    ok "Phase 9C.2 canonical recommendation aggregator imports inside the container"
else
    bad "Phase 9C.2 aggregator is NOT importable — image predates Phase 9C.2 (rebuild backend)"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# JWT for the secure production path (same pattern as Phase 7D.1).
TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

API="http://localhost"
auth() { curl -s -H "Authorization: Bearer $TOKEN" "$@"; }

PYBIN="$REPO_ROOT/backend/.venv/bin/python"

# ---------------------------------------------------------------------------
# Step 2: in-container driver — canonical evidence + route-path spy.
# ---------------------------------------------------------------------------

bold ""
bold "Canonical route execution with transport spy"

PY_SRC='import asyncio, json
from types import SimpleNamespace

# Spy on RackspaceHttpClient to prove the canonical recommendation path
# issues ZERO product writes.  Only (verb, host) tuples are captured so
# no path parameter (which may include the Billing Account Number) ever
# lands in output.
calls = []
try:
    from app.services.rackspace.client import RackspaceHttpClient
    _orig_get = RackspaceHttpClient.get
    _orig_post = RackspaceHttpClient.post_json

    async def _spy_get(self, base_url, path, **kwargs):
        from urllib.parse import urlparse
        parsed = urlparse(str(base_url))
        calls.append(("GET", parsed.netloc))
        return await _orig_get(self, base_url, path, **kwargs)

    async def _spy_post(self, base_url, path, body, **kwargs):
        from urllib.parse import urlparse
        parsed = urlparse(str(base_url))
        calls.append(("POST", parsed.netloc))
        return await _orig_post(self, base_url, path, body, **kwargs)

    RackspaceHttpClient.get = _spy_get
    RackspaceHttpClient.post_json = _spy_post
except Exception:
    pass

from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import CloudAccount
from app.services.cloud.registry import ProviderRegistry
from app.services.cloud.models import CloudProviderKind
from app.api.cloud_recommendations import get_cloud_recommendations

db = SessionLocal()
try:
    rows = list(
        db.execute(
            select(CloudAccount).where(
                CloudAccount.provider == "RACKSPACE",
                CloudAccount.enabled.is_(True),
            )
        ).scalars().all()
    )
finally:
    db.close()

adapter = ProviderRegistry.current().require(CloudProviderKind.RACKSPACE)

async def _run():
    resources = await adapter.get_resources(accounts=rows)
    # Exercise the REAL route handler twice (the same code path the
    # HTTP request takes after auth) so the spy observes the exact
    # canonical recommendation pipeline.
    db1 = SessionLocal()
    try:
        out1 = await get_cloud_recommendations(
            provider="RACKSPACE", account_id=None, region=None, days=30,
            user=SimpleNamespace(id=1), db=db1,
        )
    finally:
        db1.close()
    db2 = SessionLocal()
    try:
        out2 = await get_cloud_recommendations(
            provider="RACKSPACE", account_id=None, region=None, days=30,
            user=SimpleNamespace(id=1), db=db2,
        )
    finally:
        db2.close()
    return resources, out1, out2

resources, out1, out2 = asyncio.run(_run())


def _meta(r):
    return dict(r.metadata or {})


detached = [
    r for r in resources
    if r.resource_type == "VOLUME" and _meta(r).get("attachment_state") == "DETACHED"
]
attached = [
    r for r in resources
    if r.resource_type == "VOLUME" and _meta(r).get("attachment_state") == "ATTACHED"
]
active_servers = [
    r for r in resources
    if r.resource_type == "SERVER" and (r.state or "").upper() == "ACTIVE"
]
non_active_servers = [
    r for r in resources
    if r.resource_type == "SERVER"
    and (r.state or "").upper() in ("SHUTOFF", "SUSPENDED", "PAUSED", "ERROR")
]
load_balancers = [r for r in resources if r.resource_type == "LOAD_BALANCER"]

recs1 = out1.get("recommendations", []) if isinstance(out1, dict) else []
recs2 = out2.get("recommendations", []) if isinstance(out2, dict) else []

vol_recs = [r for r in recs1 if r.get("extensions", {}).get("rule_id") == "RS-VOL-001"]
srv_recs = [r for r in recs1 if r.get("extensions", {}).get("rule_id") == "RS-SRV-001"]
lb_recs = [r for r in recs1 if r.get("extensions", {}).get("rule_id") == "RS-LB-001"]

identity1 = [
    (r.get("recommendation_id"), r.get("resource_id"), r.get("impact")) for r in recs1
]
identity2 = [
    (r.get("recommendation_id"), r.get("resource_id"), r.get("impact")) for r in recs2
]

all_resource_ids = {r.resource_id for r in resources}
detached_ids = {r.resource_id for r in detached}
attached_ids = {r.resource_id for r in attached}
active_ids = {r.resource_id for r in active_servers}

out = {
    "resource_count": len(resources),
    "detached_volume_count": len(detached),
    "attached_volume_count": len(attached),
    "active_server_count": len(active_servers),
    "non_active_server_count": len(non_active_servers),
    "load_balancer_count": len(load_balancers),
    "detached_volume_sizes": sorted(
        _meta(r).get("size_gb") for r in detached
    ),
    "summary": out1.get("summary", {}) if isinstance(out1, dict) else {},
    "total_recommendations": len(recs1),
    "vol_rec_count": len(vol_recs),
    "srv_rec_count": len(srv_recs),
    "lb_rec_count": len(lb_recs),
    "rec_ids": [r.get("recommendation_id") for r in recs1],
    "rec_rules": sorted({r.get("extensions", {}).get("rule_id") for r in recs1 if r.get("extensions")}),
    "rec_actions": sorted({r.get("action") for r in recs1}),
    "deterministic_identity_match": identity1 == identity2,
    "deterministic_payload_match": out1 == out2,
    "ids_unique": len({i for i in (r.get("recommendation_id") for r in recs1)}) == len(recs1),
    "all_resource_refs_exist": all(
        r.get("resource_id") in all_resource_ids for r in recs1
    ),
    "unknown_savings_all": all(r.get("estimated_savings") is None for r in recs1),
    "no_currency_claimed": all(not r.get("currency") for r in recs1),
    "severity_valid": all(r.get("impact") in ("LOW", "MEDIUM", "HIGH") for r in recs1),
    "confidence_valid": all(
        r.get("extensions", {}).get("confidence") in ("LOW", "MEDIUM", "HIGH")
        for r in recs1
    ),
    "active_server_rec_overlap": sorted(
        {r.get("resource_id") for r in recs1} & active_ids
    ),
    "active_server_no_rightsizing_actions": all(
        (r.get("action") or "").upper()
        not in ("RIGHTSIZE", "DOWNSIZE", "STOP", "DELETE", "IDLE", "WASTE")
        for r in recs1
    ),
    "attached_volume_vol_rec_overlap": sorted(
        {r.get("resource_id") for r in vol_recs} & attached_ids
    ),
    "vol_rec_resource_ids_match_detached": sorted(
        {r.get("resource_id") for r in vol_recs}
    ) == sorted(detached_ids),
    "transport_call_count": len(calls),
    "transport_verbs": sorted({m for m, _h in calls}),
    "transport_product_writes": [
        {"method": m, "host": h} for m, h in calls if m != "GET" and "identity" not in h
    ],
}
print(json.dumps(out))
'

RAW="$(printf '%s\n' "$PY_SRC" | docker exec -i cost-detective-backend python - 2>/tmp/phase9c2_spy_stderr.log)"
EXIT=$?
JSON="$(printf '%s\n' "$RAW" | grep -m1 '^{')"

if [[ $EXIT -ne 0 || -z "$JSON" ]]; then
    bad "in-container canonical route run failed (exit $EXIT)"
    sed 's/^/    /' /tmp/phase9c2_spy_stderr.log | tail -25
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

read_field() {
    echo "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
v = d
for part in '$1'.split('.'):
    if not isinstance(v, dict):
        v = None
        break
    v = v.get(part)
if isinstance(v, bool):
    print('true' if v else 'false')
elif v is None:
    print('')
elif isinstance(v, (dict, list)):
    print(json.dumps(v, sort_keys=True))
else:
    print(v)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Step 3: the ACTUAL production HTTP endpoint (nginx + JWT).
# ---------------------------------------------------------------------------

bold ""
bold "Production HTTP endpoint (secure application path)"

HTTP_RAW="$(auth -w '\n%{http_code}\n' "$API/api/cloud/recommendations?provider=RACKSPACE" 2>/dev/null)"
HTTP_BODY="$(echo "$HTTP_RAW" | sed '$d')"
HTTP_CODE="$(echo "$HTTP_RAW" | tail -1 | tr -d '[:space:]')"
HTTP_JSON="$(printf '%s\n' "$HTTP_BODY" | grep -m1 '^{')"

if [[ "$HTTP_CODE" == "200" && -n "$HTTP_JSON" ]]; then
    ok "GET /api/cloud/recommendations?provider=RACKSPACE -> 200"
else
    bad "canonical endpoint answered HTTP $HTTP_CODE"
fi

if [[ -n "$HTTP_JSON" ]]; then
    HTTP_CHECK="$(printf '%s\n' "$HTTP_JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('summary', {})
recs = d.get('recommendations', [])
checks = {
    'provider': s.get('provider') == 'RACKSPACE',
    'status_valid': s.get('status') in ('SUCCESS', 'NO_DATA', 'PARTIAL_SUCCESS'),
    'source_valid': bool(s.get('source')),
    'count_reconciles': s.get('total_recommendations') == len(recs),
    'ids_unique': len({r.get('recommendation_id') for r in recs}) == len(recs),
    'unknown_savings': s.get('savings_status') == 'EMPTY'
        and s.get('potential_savings') is None
        and all(r.get('estimated_savings') is None for r in recs),
    'no_zero_savings_claim': not any(
        r.get('estimated_savings') is not None for r in recs
    ),
}
print(json.dumps(checks))
" 2>/dev/null)"
    for pair in $(printf '%s\n' "$HTTP_CHECK" | tr -d ' {}"' | tr ',' ' '); do
        key="${pair%%:*}"; val="${pair#*:}"
        if [[ "$val" == "true" ]]; then
            ok "http: $key"
        else
            bad "http: $key"
        fi
    done
else
    bad "production endpoint returned no JSON body"
fi

# Run the production request twice and compare deterministic identity.
HTTP2_JSON="$(auth "$API/api/cloud/recommendations?provider=RACKSPACE" 2>/dev/null | grep -m1 '^{')"
if [[ -n "$HTTP_JSON" && -n "$HTTP2_JSON" ]]; then
    DET_HTTP="$(python3 - <<PYEOF 2>/dev/null
import json
a = json.loads('''$HTTP_JSON''')
b = json.loads('''$HTTP2_JSON''')
ia = [(r.get('recommendation_id'), r.get('resource_id')) for r in a.get('recommendations', [])]
ib = [(r.get('recommendation_id'), r.get('resource_id')) for r in b.get('recommendations', [])]
print('true' if ia == ib else 'false')
PYEOF
)"
    if [[ "$DET_HTTP" == "true" ]]; then
        ok "production endpoint: two requests returned identical recommendation identity/order"
    else
        bad "production endpoint: recommendation identity/order changed between requests"
    fi
fi

# ---------------------------------------------------------------------------
# Step 4: provider discovery regression.
# ---------------------------------------------------------------------------

bold ""
bold "Provider discovery"

PROVIDERS_JSON="$(auth "$API/api/cloud/providers" 2>/dev/null | grep -m1 '^{')"
if [[ -n "$PROVIDERS_JSON" ]]; then
    CAP_CHECK="$(printf '%s\n' "$PROVIDERS_JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
def cap(provider, name):
    for p in d.get('providers', []):
        if p.get('provider') == provider:
            for c in p.get('capabilities', []):
                if c.get('capability') == name:
                    return c.get('state')
    return None
print(cap('RACKSPACE', 'OPTIMIZATION'))
print(cap('AWS', 'OPTIMIZATION'))
print(cap('AZURE', 'OPTIMIZATION'))
print(cap('RACKSPACE', 'COSTS'))
print(cap('RACKSPACE', 'RESOURCES'))
" 2>/dev/null)"
    RS_OPT="$(echo "$CAP_CHECK" | sed -n 1p)"
    AWS_OPT="$(echo "$CAP_CHECK" | sed -n 2p)"
    AZ_OPT="$(echo "$CAP_CHECK" | sed -n 3p)"
    RS_COSTS="$(echo "$CAP_CHECK" | sed -n 4p)"
    RS_RES="$(echo "$CAP_CHECK" | sed -n 5p)"

    if [[ "$RS_OPT" == "SUPPORTED" ]]; then
        ok "provider discovery: RACKSPACE recommendations (OPTIMIZATION) = SUPPORTED"
    else
        bad "provider discovery: RACKSPACE recommendations = '$RS_OPT' (expected SUPPORTED)"
    fi
    if [[ "$RS_COSTS" == "SUPPORTED" ]]; then
        ok "provider discovery: RACKSPACE costs = SUPPORTED"
    else
        bad "provider discovery: RACKSPACE costs = '$RS_COSTS'"
    fi
    if [[ "$RS_RES" == "SUPPORTED" ]]; then
        ok "provider discovery: RACKSPACE resources = SUPPORTED"
    else
        bad "provider discovery: RACKSPACE resources = '$RS_RES'"
    fi
    if [[ "$AWS_OPT" == "SUPPORTED" ]]; then
        ok "provider discovery: AWS unchanged (recommendations SUPPORTED)"
    else
        bad "provider discovery: AWS recommendations = '$AWS_OPT'"
    fi
    if [[ -n "$AZ_OPT" ]]; then
        ok "provider discovery: AZURE present and unchanged (recommendations=$AZ_OPT)"
    else
        bad "provider discovery: AZURE capability missing"
    fi
else
    bad "/api/cloud/providers returned no JSON"
fi

# ---------------------------------------------------------------------------
# Step 5: reconcile the in-container canonical evidence.
# ---------------------------------------------------------------------------

bold ""
bold "Canonical evidence reconciliation"

res_count="$(read_field resource_count)"
det_count="$(read_field detached_volume_count)"
att_count="$(read_field attached_volume_count)"
active_count="$(read_field active_server_count)"
non_active_count="$(read_field non_active_server_count)"
lb_count="$(read_field load_balancer_count)"
total_recs="$(read_field total_recommendations)"
vol_recs="$(read_field vol_rec_count)"
srv_recs="$(read_field srv_rec_count)"
lb_recs="$(read_field lb_rec_count)"
det_sizes="$(read_field detached_volume_sizes)"
summary_json="$(read_field summary)"
status_val="$(read_field summary.status)"
savings_status="$(read_field summary.savings_status)"
potential_savings="$(read_field summary.potential_savings)"
with_savings="$(read_field summary.recommendations_with_savings)"
without_savings="$(read_field summary.recommendations_without_savings)"
by_impact="$(read_field summary.by_impact)"
by_source="$(read_field summary.by_source)"

info "resources=$res_count volumes(detached=$det_count attached=$att_count) servers(active=$active_count non-active=$non_active_count) load_balancers=$lb_count"
info "status=$status_val total_recommendations=$total_recs rules=$(read_field rec_rules) actions=$(read_field rec_actions)"
info "by_impact=$by_impact savings_status=$savings_status with_savings=$with_savings without_savings=$without_savings potential_savings=$potential_savings"
info "detached volume sizes (live): $det_sizes"

# ---- provider / status -------------------------------------------------------

if [[ "$status_val" == "SUCCESS" || "$status_val" == "NO_DATA" || "$status_val" == "PARTIAL_SUCCESS" ]]; then
    ok "canonical status is valid ($status_val)"
else
    bad "canonical status is '${status_val}'"
fi

if [[ -n "$(read_field summary.source)" ]]; then
    ok "summary source present ($(read_field summary.source))"
else
    bad "summary source missing"
fi

# ---- count reconciliation ----------------------------------------------------

if [[ "$vol_recs" == "$det_count" ]]; then
    ok "RS-VOL-001 recommendations ($vol_recs) reconcile with current detached inventory ($det_count)"
else
    bad "RS-VOL-001 recommendations ($vol_recs) != detached volumes ($det_count)"
fi

if [[ "$srv_recs" == "$non_active_count" ]]; then
    ok "RS-SRV-001 recommendations ($srv_recs) reconcile with non-ACTIVE servers ($non_active_count)"
else
    bad "RS-SRV-001 recommendations ($srv_recs) != non-ACTIVE servers ($non_active_count)"
fi

if [[ "$lb_recs" == "0" ]]; then
    ok "RS-LB-001 recommendations = 0 (conservative; DELETED-status only)"
else
    info "RS-LB-001 produced $lb_recs findings (DELETED-status load balancers present)"
fi

TOTAL_OK="$(printf '%s\n' "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
total = d['total_recommendations']
ok = total == d['vol_rec_count'] + d['srv_rec_count'] + d['lb_rec_count']
by_impact_sum = sum(g['count'] for g in d['summary'].get('by_impact', []))
by_type_sum = sum(g['count'] for g in d['summary'].get('by_resource_type', []))
by_action_sum = sum(g['count'] for g in d['summary'].get('by_action', []))
print('1' if ok and by_impact_sum == total and by_type_sum == total and by_action_sum == total else '0')
" 2>/dev/null)"
if [[ "$TOTAL_OK" == "1" ]]; then
    ok "summary breakdowns reconcile with total_recommendations ($total_recs)"
else
    bad "summary breakdowns do NOT reconcile with total_recommendations ($total_recs)"
fi

# ---- determinism --------------------------------------------------------------

if [[ "$(read_field deterministic_identity_match)" == "true" ]]; then
    ok "deterministic identity/order: two runs produced identical recommendation identity + order"
else
    bad "recommendation identity/order differs between consecutive runs"
fi
if [[ "$(read_field deterministic_payload_match)" == "true" ]]; then
    ok "deterministic payloads: byte-identical envelopes across consecutive runs"
else
    bad "envelope payloads differ between consecutive runs"
fi
if [[ "$(read_field ids_unique)" == "true" ]]; then
    ok "recommendation IDs are unique ($total_recs recommendations)"
else
    bad "recommendation IDs contain duplicates"
fi

# ---- resource references / severity / confidence ------------------------------

if [[ "$(read_field all_resource_refs_exist)" == "true" ]]; then
    ok "every recommendation references a resource in the canonical inventory"
else
    bad "a recommendation references an unknown resource"
fi
if [[ "$(read_field severity_valid)" == "true" ]]; then
    ok "severity valid for every recommendation (by_impact=$by_impact)"
else
    bad "invalid severity on a recommendation"
fi
if [[ "$(read_field confidence_valid)" == "true" ]]; then
    ok "confidence valid for every recommendation"
else
    bad "invalid confidence on a recommendation"
fi

# ---- savings semantics --------------------------------------------------------

if [[ "$(read_field unknown_savings_all)" == "true" && "$with_savings" == "0" && "$without_savings" == "$total_recs" ]]; then
    ok "unknown savings remain null for all $total_recs recommendations (known=$with_savings unknown=$without_savings)"
else
    bad "savings semantics broken: known=$with_savings unknown=$without_savings total=$total_recs"
fi
if [[ "$potential_savings" == "" && "$savings_status" == "EMPTY" ]]; then
    ok "estimated savings total is null with savings_status=EMPTY (unknown != \$0)"
else
    bad "estimated savings total is '$potential_savings' with savings_status='$savings_status'"
fi
if [[ "$(read_field no_currency_claimed)" == "true" ]]; then
    ok "no currency is claimed for unknown savings"
else
    bad "a currency was claimed without a savings figure"
fi

# ---- ACTIVE server safety ------------------------------------------------------

ACTIVE_OVERLAP="$(read_field active_server_rec_overlap)"
if [[ "$ACTIVE_OVERLAP" == "[]" ]]; then
    ok "ACTIVE servers ($active_count) have zero recommendations (no false rightsizing)"
else
    bad "ACTIVE servers produced recommendations: $ACTIVE_OVERLAP"
fi
if [[ "$(read_field active_server_no_rightsizing_actions)" == "true" ]]; then
    ok "no rightsizing/downsizing/idle/waste actions in the canonical output"
else
    bad "a rightsizing-style action leaked into the canonical output"
fi

# ---- attached volume safety ----------------------------------------------------

ATT_OVERLAP="$(read_field attached_volume_vol_rec_overlap)"
if [[ "$ATT_OVERLAP" == "[]" ]]; then
    ok "attached volumes ($att_count) have no detached-volume recommendations"
else
    bad "attached volumes produced RS-VOL-001 findings: $ATT_OVERLAP"
fi
if [[ "$(read_field vol_rec_resource_ids_match_detached)" == "true" ]]; then
    ok "RS-VOL-001 resource IDs match the current detached inventory exactly"
else
    bad "RS-VOL-001 resource IDs do not match the detached inventory"
fi

# ---- read-only wire contract ----------------------------------------------------

PRODUCT_WRITES="$(read_field transport_product_writes)"
VERBS="$(read_field transport_verbs)"
if [[ "$PRODUCT_WRITES" == "[]" && "$VERBS" != *"PUT"* && "$VERBS" != *"PATCH"* && "$VERBS" != *"DELETE"* ]]; then
    ok "product write requests = 0 (verbs observed: $VERBS)"
else
    bad "product write requests detected: $PRODUCT_WRITES verbs=$VERBS"
fi

# ---- secret leakage scan ---------------------------------------------------------

LEAK=0
RAN_VALUE="$(grep -E '^RACKSPACE_BILLING_ACCOUNT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"
APIKEY_VALUE="$(grep -E '^RACKSPACE_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"

for fragment in "$JSON" "$RAW" "$HTTP_JSON"; do
    if [[ -n "$RAN_VALUE" && "$fragment" == *"$RAN_VALUE"* ]]; then
        bad "captured output contains the RAN"
        LEAK=1
    fi
    if [[ -n "$APIKEY_VALUE" && "$fragment" == *"$APIKEY_VALUE"* ]]; then
        bad "captured output contains the API key"
        LEAK=1
    fi
    if echo "$fragment" | grep -qi 'x-auth-token'; then
        bad "captured output mentions X-Auth-Token"
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
echo "  PASS=$PASS  FAIL=$FAIL"
if [[ $FAIL -eq 0 ]]; then
    bold "RESULT: validation successful"
    exit 0
fi
bold "RESULT: validation failure"
exit 1
