#!/usr/bin/env bash
# Phase 9B.2 — Rackspace canonical resource integration validator.
#
# Drives the Rackspace canonical resource path end-to-end against the
# LIVE deployment and the *real* Rackspace Cloud APIs, using
# credentials supplied in the host's ``.env`` only.
#
# Proves:
#   * Rackspace Identity authentication succeeds
#   * the service catalog resolves (product endpoints discovered)
#   * the canonical API surface answers provider=RACKSPACE
#   * Cloud Servers inventory works; an EMPTY server list is SUCCESS
#   * Cloud Block Storage inventory works where authorized; an EMPTY
#     volume list is SUCCESS
#   * Cloud Load Balancer capability is accurately reported (SUCCESS,
#     or a sanitized per-service failure such as UNSUPPORTED /
#     ACCESS_DENIED — never fabricated)
#   * multi-region discovery, partial-success semantics, and the
#     canonical summary reconcile with the flat resource list
#   * zero product write requests (GET only; the single Identity
#     authentication POST is the documented exception)
#   * zero secret leakage (no API key / X-Auth-Token / RAN in any
#     captured output)
#
# The live account does NOT need to contain resources — a tenant
# with zero resources is a fully valid passing state
# (HTTP 200 + [] = SUCCESS).
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

bold "Phase 9B.2 — Rackspace canonical resource integration validation"

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

if grep -qE '^RACKSPACE_USERNAME=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    USERNAME_SET=1
fi
if grep -qE '^RACKSPACE_API_KEY=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    APIKEY_SET=1
fi
if grep -qE '^RACKSPACE_ACCOUNT_ID=[^[:space:]](.*[^[:space:]])?$' "$ENV_FILE"; then
    ACCOUNT_SET=1
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

# The backend container must be running with the Phase 9B.2 code and
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
             and s.rackspace_account_id) else '0')
" 2>/dev/null | tail -1)"
if [[ "$BACKEND_CFG" == "1" ]]; then
    ok "backend container sees the Rackspace Identity configuration (values redacted)"
else
    bad "backend container does not have the Rackspace configuration wired (rebuild/recreate with the Phase 9B.2 compose env)"
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

# Phase 9B.2: the adapter must declare RESOURCES as SUPPORTED (when
# Identity credentials are configured).
CAP_OK="$(docker exec cost-detective-backend python -c "
from app.services.cloud.registry import ProviderRegistry
from app.services.cloud.base import CloudCapability
from app.services.cloud.models import CloudProviderKind
adapter = ProviderRegistry.current().require(CloudProviderKind.RACKSPACE)
status = adapter.get_capabilities()
print('1' if status.has(CloudCapability.RESOURCES) else '0')
" 2>/dev/null | tail -1)"
if [[ "$CAP_OK" == "1" ]]; then
    ok "RACKSPACE adapter declares RESOURCES = SUPPORTED (Phase 9B.2)"
else
    bad "RACKSPACE adapter does not declare RESOURCES = SUPPORTED — the backend image predates Phase 9B.2"
    echo
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# Read-only surface: the inventory client exposes no write verbs and
# the shared transport exposes GET (+ the Identity-only POST).
SURFACE_OK="$(docker exec cost-detective-backend python -c "
from app.services.rackspace.inventory import RackspaceInventoryClient
from app.services.rackspace.client import RackspaceHttpClient
inv = [n for n in dir(RackspaceInventoryClient) if not n.startswith('_')]
http = [n for n in dir(RackspaceHttpClient) if not n.startswith('_')]
bad_verbs = [
    v for v in ('post', 'post_json', 'put', 'put_json', 'patch',
                'patch_json', 'delete', 'delete_json')
    if v in inv
]
bad_verbs += [v for v in ('put', 'patch', 'delete') if v in http]
print('1' if (not bad_verbs and 'get' in http) else '0')
" 2>/dev/null | tail -1)"
if [[ "$SURFACE_OK" == "1" ]]; then
    ok "read-only surface: inventory client has no write verbs; transport is GET (+ Identity POST) only"
else
    bad "write-like verbs detected on the Rackspace resource surface"
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
# Step 2: Rackspace Identity + service catalog proof via the Phase 9A
# POC runner (its JSON output is sanitized by design — no raw records,
# no credentials, no RAN).
# ---------------------------------------------------------------------------

bold ""
bold "Rackspace Identity + service catalog (Phase 9A POC client)"

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
elif isinstance(v, list):
    print(','.join(str(item) for item in v))
else:
    print(v)
" 2>/dev/null
}

identity_ok="$(read_poc_field identity_ok)"
roles="$(read_poc_field roles)"
elevated_roles_present="$(read_poc_field elevated_roles_present)"
service_types="$(read_poc_field service_types)"
write_methods_exposed="$(read_poc_field write_methods_exposed)"
no_token_in_output="$(read_poc_field no_token_in_output)"

if [[ "$identity_ok" == "true" ]]; then
    ok "Rackspace Identity authentication succeeded"
else
    bad "Rackspace Identity authentication failed"
fi

if [[ -n "$roles" && "$roles" != "None" ]]; then
    ok "Identity roles are present on the principal ($roles)"
else
    bad "no Identity roles returned for the principal"
fi

if [[ -z "$elevated_roles_present" || "$elevated_roles_present" == "[]" || "$elevated_roles_present" == "false" ]]; then
    ok "no elevated Rackspace roles are present"
else
    bad "elevated Rackspace roles detected — the read-only contract is violated"
fi

if [[ -n "$service_types" && "$service_types" != "None" ]]; then
    ok "service catalog resolved ($(echo "$service_types" | tr ',' '\n' | wc -l | tr -d ' ') service types published)"
else
    bad "service catalog is empty — cannot resolve product endpoints"
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
# Step 3: live resource aggregation inside the backend container with
# a transport verb spy.  Proves the read-only wire contract (every
# product request is a GET; the only POST is Identity /v2.0/tokens)
# and captures the inventory snapshot used by the report below.
# ---------------------------------------------------------------------------

bold ""
bold "Live resource aggregation (transport verb spy — canonical aggregator)"

SPY_SRC='import asyncio, json

from sqlalchemy import select

from app.db.session import SessionLocal
from app.db.models import CloudAccount
from app.services.rackspace.client import RackspaceHttpClient

calls = []

_orig_get = RackspaceHttpClient.get
_orig_post = RackspaceHttpClient.post_json


async def _spy_get(self, base_url, path, **kwargs):
    calls.append(("GET", str(base_url), str(path)))
    return await _orig_get(self, base_url, path, **kwargs)


async def _spy_post(self, base_url, path, body, **kwargs):
    calls.append(("POST", str(base_url), str(path)))
    return await _orig_post(self, base_url, path, body, **kwargs)


RackspaceHttpClient.get = _spy_get
RackspaceHttpClient.post_json = _spy_post

from app.services.cloud.rackspace.resources import (
    aggregate_rackspace_resources,
)

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

summary, resources, failures = asyncio.run(
    aggregate_rackspace_resources(rows=rows)
)
ext = dict(summary.extensions or {})

def _bucket(resources, attr):
    out = {}
    for r in resources:
        key = getattr(r, attr) or "unknown"
        out[key] = out.get(key, 0) + 1
    return out

servers = [r for r in resources if r.resource_type == "SERVER"]
volumes = [r for r in resources if r.resource_type == "VOLUME"]
lbs = [r for r in resources if r.resource_type == "LOAD_BALANCER"]

flavors = {}
for r in servers:
    meta = dict(r.metadata or {})
    label = meta.get("flavor_name") or meta.get("flavor_id") or "unknown"
    flavors[label] = flavors.get(label, 0) + 1

attachments = {}
for r in volumes:
    state = (dict(r.metadata or {})).get("attachment_state", "unknown")
    attachments[state] = attachments.get(state, 0) + 1

verbs = sorted({m for m, _b, _p in calls})
posts = [(b, p) for m, b, p in calls if m == "POST"]
gets = [(b, p) for m, b, p in calls if m == "GET"]
identity_posts = [
    (b, p) for b, p in posts
    if "identity" in b or p.endswith("/tokens")
]
product_writes = [
    {"method": m, "base": b, "path": p}
    for m, b, p in calls
    if m != "GET" and not ("identity" in b or p.endswith("/tokens"))
]

composite_ids = [r.composite_id for r in resources]

out = {
    "status": ext.get("status"),
    "source": ext.get("source"),
    "total_resources": ext.get("total_resources"),
    "by_type": {e["key"]: e["count"] for e in ext.get("by_type", [])},
    "by_location": {e["key"]: e["count"] for e in ext.get("by_location", [])},
    "by_status": {e["key"]: e["count"] for e in ext.get("by_status", [])},
    "by_subscription": ext.get("by_subscription", []),
    "subscription_errors": ext.get("subscription_errors", []),
    "service_status": ext.get("service_status", {}),
    "regions_queried": ext.get("regions_queried", []),
    "warnings": ext.get("warnings", []),
    "failures": [
        {"service": f.service, "region": f.region, "code": f.category.value}
        for f in failures
    ],
    "servers": {
        "count": len(servers),
        "regions": sorted({r.region for r in servers if r.region}),
        "statuses": _bucket(servers, "state"),
        "flavors": flavors,
    },
    "volumes": {
        "count": len(volumes),
        "total_gb": sum(int((dict(r.metadata or {})).get("size_gb") or 0) for r in volumes),
        "statuses": _bucket(volumes, "state"),
        "attachments": attachments,
    },
    "load_balancers": {"count": len(lbs), "statuses": _bucket(lbs, "state")},
    "created_timestamp_present": sum(
        1 for r in resources if (dict(r.metadata or {})).get("created")
    ),
    "composite_duplicates": len(composite_ids) - len(set(composite_ids)),
    "verbs": verbs,
    "get_count": len(gets),
    "post_count": len(posts),
    "identity_post_count": len(identity_posts),
    "product_write_requests": product_writes,
    "product_paths": sorted({p for _b, p in gets}),
}
print(json.dumps(out))
'

SPY_RAW="$(printf '%s\n' "$SPY_SRC" | docker exec -i cost-detective-backend python - 2>/tmp/phase9b2_spy_stderr.log)"
SPY_EXIT=$?
SPY_JSON="$(printf '%s\n' "$SPY_RAW" | grep -m1 '^{')"

if [[ $SPY_EXIT -ne 0 || -z "$SPY_JSON" ]]; then
    bad "in-container resource aggregation failed (exit $SPY_EXIT)"
    sed 's/^/    /' /tmp/phase9b2_spy_stderr.log | tail -15
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

read_spy() {
    echo "$SPY_JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
cur = d
for part in '$1'.split('.'):
    if not isinstance(cur, dict):
        cur = None
        break
    cur = cur.get(part)
if isinstance(cur, bool):
    print('true' if cur else 'false')
elif cur is None:
    print('')
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, sort_keys=True))
else:
    print(cur)
" 2>/dev/null
}

spy_status="$(read_spy status)"
spy_source="$(read_spy source)"
spy_total="$(read_spy total_resources)"
spy_verbs="$(read_spy verbs)"
spy_product_writes="$(read_spy product_write_requests)"
spy_get_count="$(read_spy get_count)"
spy_regions="$(read_spy regions_queried)"
spy_service_status="$(read_spy service_status)"
spy_dupes="$(read_spy composite_duplicates)"

info "live status=$spy_status total_resources=$spy_total regions_queried=$spy_regions product_GETs=$spy_get_count verbs=$spy_verbs"
info "server count=$(read_spy servers.count) regions=$(read_spy servers.regions) statuses=$(read_spy servers.statuses)"
info "server flavors=$(read_spy servers.flavors)"
info "volume count=$(read_spy volumes.count) total_gb=$(read_spy volumes.total_gb) statuses=$(read_spy volumes.statuses) attachments=$(read_spy volumes.attachments)"
info "load balancer count=$(read_spy load_balancers.count) statuses=$(read_spy load_balancers.statuses)"

# ---- read-only wire contract ------------------------------------------------

if [[ "$spy_product_writes" == "[]" && "$spy_verbs" != *"PUT"* && "$spy_verbs" != *"PATCH"* && "$spy_verbs" != *"DELETE"* ]]; then
    ok "product write requests = 0 (only GET product calls; Identity POST is authentication)"
else
    bad "product write requests detected: $spy_product_writes verbs=$spy_verbs"
fi

if [[ "$spy_verbs" == *"GET"* ]]; then
    ok "product APIs were queried with GET only ($spy_get_count GET requests across $(echo "$(read_spy product_paths)" | "$PYBIN" -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null) distinct paths)"
else
    bad "no product GET requests observed — inventory did not touch the product APIs"
fi

# ---- status + reconciliation ------------------------------------------------

if [[ "$spy_status" == "SUCCESS" ]]; then
    ok "canonical aggregation status=SUCCESS"
    if [[ "$spy_total" == "0" ]]; then
        ok "empty Rackspace inventory is SUCCESS (HTTP 200 + [] semantics hold)"
    fi
elif [[ "$spy_status" == "PARTIAL_SUCCESS" ]]; then
    spy_errors="$(read_spy subscription_errors)"
    if [[ "$spy_errors" != "[]" && -n "$spy_errors" ]]; then
        ok "canonical aggregation status=PARTIAL_SUCCESS with sanitized per-service errors ($spy_errors)"
    else
        bad "status=PARTIAL_SUCCESS but no per-service failures were reported — inconsistent rollup"
    fi
else
    bad "canonical aggregation status='$spy_status' (expected SUCCESS or PARTIAL_SUCCESS)"
fi

if [[ "$spy_source" == "RACKSPACE_CLOUD_API" ]]; then
    ok "canonical source=RACKSPACE_CLOUD_API"
else
    bad "canonical source='$spy_source' (expected RACKSPACE_CLOUD_API)"
fi

SPY_RECON="$(echo "$SPY_JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
total = d.get('total_resources')
ok = isinstance(total, int)
by_type_sum = sum((d.get('by_type') or {}).values())
by_loc_sum = sum((d.get('by_location') or {}).values())
by_sub_sum = sum(e.get('count', 0) for e in (d.get('by_subscription') or []))
ok = ok and by_type_sum == total and by_loc_sum == total and by_sub_sum == total
print('1' if ok else f'0 by_type={by_type_sum} by_location={by_loc_sum} by_subscription={by_sub_sum} total={total}')
" 2>/dev/null)"
if [[ "$SPY_RECON" == "1" ]]; then
    ok "resource totals reconcile (by_type / by_location / by_subscription sums == total_resources == $spy_total)"
else
    bad "resource totals do NOT reconcile: $SPY_RECON"
fi

if [[ "$spy_dupes" == "0" ]]; then
    ok "no duplicate composite ids (multi-region aggregation guard held)"
else
    bad "$spy_dupes duplicate composite ids detected"
fi

# ---- per-service gates ------------------------------------------------------

SPY_CHECK_SVC="$(echo "$SPY_JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
ss = d.get('service_status') or {}
out = []
for name in ('Cloud Servers', 'Cloud Block Storage', 'Cloud Load Balancers'):
    entry = ss.get(name)
    if entry is None:
        out.append(name + ':MISSING')
    else:
        out.append(name + ':' + str(entry.get('status')) + ':' + str(entry.get('error_code')))
print('|'.join(out))
" 2>/dev/null)"

IFS='|' read -ra SVC_PARTS <<< "$SPY_CHECK_SVC"
SVC_SERVERS="${SVC_PARTS[0]:-Cloud Servers:MISSING}"
SVC_CBS="${SVC_PARTS[1]:-Cloud Block Storage:MISSING}"
SVC_LB="${SVC_PARTS[2]:-Cloud Load Balancers:MISSING}"

if [[ "$SVC_SERVERS" == "Cloud Servers:SUCCESS:None" ]]; then
    ok "Cloud Servers query succeeded (server regions: $(read_spy servers.regions); catalog regions queried: $spy_regions)"
else
    bad "Cloud Servers service did not succeed: $SVC_SERVERS"
fi

if [[ "$SVC_CBS" == "Cloud Block Storage:SUCCESS:None" ]]; then
    ok "Cloud Block Storage query succeeded (attachment state + size captured)"
elif [[ "$SVC_CBS" == *ACCESS_DENIED* || "$SVC_CBS" == *UNSUPPORTED* ]]; then
    ok "Cloud Block Storage is not authorized on this tenant — accurately reported ($SVC_CBS)"
else
    bad "Cloud Block Storage service state is unexpected: $SVC_CBS"
fi

if [[ "$SVC_LB" == "Cloud Load Balancers:SUCCESS:None" ]]; then
    ok "Cloud Load Balancers query succeeded (count=$(read_spy load_balancers.count))"
elif [[ "$SVC_LB" == *UNSUPPORTED* || "$SVC_LB" == *ACCESS_DENIED* || "$SVC_LB" == *NOT_FOUND* || "$SVC_LB" == *NETWORK_ERROR* ]]; then
    ok "Cloud Load Balancers capability accurately reported ($SVC_LB) — never fabricated"
else
    bad "Cloud Load Balancers capability is misreported: $SVC_LB"
fi

# ---------------------------------------------------------------------------
# Step 4: canonical API path — GET /api/cloud/resources?provider=RACKSPACE
# through the public Nginx entrypoint with an admin JWT.
# ---------------------------------------------------------------------------

bold ""
bold "Canonical resource path — /api/cloud/resources?provider=RACKSPACE"

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
CANON_JSON="$(curl -sS -m 90 \
    -H "Authorization: Bearer $TOKEN" \
    "$API/api/cloud/resources?provider=RACKSPACE" 2>&1)"
CURL_EXIT=$?

if [[ $CURL_EXIT -ne 0 ]]; then
    bad "canonical resource request failed at the transport layer (curl exit $CURL_EXIT)"
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

read_canon() {
    echo "$CANON_JSON" | "$PYBIN" -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
cur = d
for part in '$1'.split('.'):
    if not isinstance(cur, dict):
        cur = None
        break
    cur = cur.get(part)
if isinstance(cur, bool):
    print('true' if cur else 'false')
elif cur is None:
    print('')
elif isinstance(cur, (dict, list)):
    print(json.dumps(cur, sort_keys=True))
else:
    print(cur)
" 2>/dev/null
}

canon_provider="$(read_canon summary.provider)"
canon_status="$(read_canon summary.status)"
canon_scope="$(read_canon summary.scope)"
canon_total="$(read_canon summary.total_resources)"
canon_source="$(read_canon summary.source)"
canon_error_code="$(read_canon error_code)"

if [[ "$canon_provider" == "RACKSPACE" ]]; then
    ok "canonical response provider=RACKSPACE"
else
    bad "canonical response provider is '${canon_provider}' (expected RACKSPACE; error_code='${canon_error_code}')"
fi

if [[ "$canon_status" == "SUCCESS" || "$canon_status" == "PARTIAL_SUCCESS" ]]; then
    if [[ "$canon_status" == "SUCCESS" && "$canon_total" == "0" ]]; then
        ok "canonical status=SUCCESS with an EMPTY inventory (zero resources is a valid passing state)"
    else
        ok "canonical status=$canon_status"
    fi
else
    bad "canonical status is '${canon_status}' (error_code='${canon_error_code}')"
fi

if [[ -n "$canon_scope" ]]; then
    ok "canonical scope=$canon_scope"
else
    bad "canonical response carries no scope"
fi

if [[ "$canon_source" == "RACKSPACE_CLOUD_API" ]]; then
    ok "canonical source=RACKSPACE_CLOUD_API"
else
    bad "canonical source is '${canon_source}' (expected RACKSPACE_CLOUD_API)"
fi

CANON_RECON="$(echo "$CANON_JSON" | "$PYBIN" -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('0 PARSE')
    sys.exit(0)
summary = d.get('summary') or {}
resources = d.get('resources') or []
total = summary.get('total_resources')
ok = isinstance(total, int) and total == len(resources)
by_type_sum = sum(e.get('count', 0) for e in (summary.get('by_type') or []))
by_loc_sum = sum(e.get('count', 0) for e in (summary.get('by_location') or []))
by_sub_sum = sum(e.get('count', 0) for e in (summary.get('by_subscription') or []))
ok = ok and by_type_sum == total and by_loc_sum == total and by_sub_sum == total
providers = {r.get('provider') for r in resources}
ok = ok and (providers <= {'RACKSPACE'})
prefix_ok = all(
    str(r.get('composite_id', '')).startswith('RACKSPACE|') for r in resources
)
dupes = len(resources) - len({r.get('composite_id') for r in resources})
ok = ok and prefix_ok and dupes == 0
if summary.get('status') == 'PARTIAL_SUCCESS':
    errs = summary.get('subscription_errors') or []
    ok = ok and bool(errs) and all(e.get('code') for e in errs)
print('1' if ok else f'0 total={total} len={len(resources)} by_type={by_type_sum} by_location={by_loc_sum} by_subscription={by_sub_sum} providers={sorted(providers)} prefix_ok={prefix_ok} dupes={dupes}')
" 2>/dev/null)"

if [[ "$CANON_RECON" == "1" ]]; then
    ok "canonical envelope reconciles (total == listed resources; breakdown sums match; provider/composite-id discipline held)"
else
    bad "canonical envelope does NOT reconcile: $CANON_RECON"
fi

printf '%s\n' "$CANON_JSON" > /tmp/phase9b2_canon.json
printf '%s\n' "$SPY_JSON" > /tmp/phase9b2_spy.json
ROUTE_CONSIST="$("$PYBIN" -c "
import json
canon = json.load(open('/tmp/phase9b2_canon.json'))
spy = json.load(open('/tmp/phase9b2_spy.json'))
ct = (canon.get('summary') or {}).get('total_resources')
st = spy.get('total_resources')
print('1' if ct == st else f'0 route={ct} aggregator={st}')
" 2>/dev/null)"
if [[ "$ROUTE_CONSIST" == "1" ]]; then
    ok "route total ($canon_total) matches the in-container aggregation total ($spy_total)"
else
    bad "route/aggregator totals diverge: $ROUTE_CONSIST"
fi

# ---------------------------------------------------------------------------
# Step 5: secret-leakage scan over every captured artifact.
# ---------------------------------------------------------------------------

bold ""
bold "Secret leakage scan"

LEAK=0
RAN_VALUE="$(grep -E '^RACKSPACE_BILLING_ACCOUNT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"
APIKEY_VALUE="$(grep -E '^RACKSPACE_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"

for artifact_name in CANON_JSON SPY_RAW POC_JSON; do
    artifact="${!artifact_name}"
    if [[ -n "$RAN_VALUE" && "$artifact" == *"$RAN_VALUE"* ]]; then
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
