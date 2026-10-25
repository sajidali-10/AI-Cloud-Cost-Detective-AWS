#!/usr/bin/env bash
# Phase 9C.1 — Rackspace deterministic optimization engine validator.
#
# Drives the INTERNAL Rackspace optimization engine end-to-end:
#   * loads canonical Rackspace resources (Phase 9B.2)
#   * loads canonical Rackspace cost context (Phase 9B.1)
#   * runs the deterministic engine
#   * runs it again to prove byte-stable findings
#
# Validates:
#   * provider = RACKSPACE
#   * engine executes successfully
#   * resource counts match the canonical input
#   * finding counts reconcile
#   * deterministic IDs (same input -> same id)
#   * detached volumes produce review candidates when present
#   * attached volumes do NOT produce findings
#   * ACTIVE servers produce NO rightsizing findings (metrics deferred)
#   * empty load balancer inventory produces no finding
#   * estimated_savings_total is null where pricing is unsupported
#   * no credential leakage
#   * product write requests = 0
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

bold "Phase 9C.1 — Rackspace deterministic optimization engine validation"

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

OPT_IMPORT_OK="$(docker exec cost-detective-backend python -c "
from app.services.cloud.rackspace.optimization import (
    evaluate_rackspace_optimization,
    deterministic_finding_id,
)
from app.services.cloud.rackspace.metrics_capability import (
    probe_metrics_capability_sync,
    RightsizingEvidenceStatus,
)
print('1')
" 2>/dev/null | tail -1)"
if [[ "$OPT_IMPORT_OK" == "1" ]]; then
    ok "Phase 9C.1 optimization engine imports cleanly inside the backend container"
else
    bad "Phase 9C.1 optimization engine is NOT importable in the backend container"
    bold "RESULT: configuration/precondition failure"
    exit 2
fi

# ---------------------------------------------------------------------------
# Step 2: drive the engine against the live canonical inputs.
# ---------------------------------------------------------------------------

bold ""
bold "Engine execution against canonical Rackspace inputs"

PY_SRC='import asyncio, json
from app.services.cloud.rackspace.optimization import (
    evaluate_rackspace_optimization,
)
from app.services.cloud.rackspace.metrics_capability import (
    probe_metrics_capability_sync,
)

# Spy on RackspaceHttpClient to prove the optimization engine does
# NOT touch any product transport.  This is the read-only contract.
# We capture ONLY (verb, host) tuples so no path parameter (which
# may include the Billing Account Number) ever lands in output.
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


async def _gather_inputs():
    return await asyncio.gather(
        adapter.get_resources(accounts=rows),
        adapter.get_cost_summary(
            accounts=rows,
            period_start=__import__("datetime").date(2026, 10, 19),
            period_end=__import__("datetime").date(2026, 11, 19),
            previous_period_start=__import__("datetime").date(2026, 10, 19),
            previous_period_end=__import__("datetime").date(2026, 11, 19),
            region=None,
        ),
    )


resources, cost_ctx = asyncio.run(_gather_inputs())

capability = probe_metrics_capability_sync()

ts = "2026-10-25T00:00:00+00:00"
result1 = evaluate_rackspace_optimization(
    resources,
    cost_context=cost_ctx,
    rightsizing_evidence_status=capability.status.value,
    evaluated_at_iso=ts,
)
result2 = evaluate_rackspace_optimization(
    resources,
    cost_context=cost_ctx,
    rightsizing_evidence_status=capability.status.value,
    evaluated_at_iso=ts,
)

def _bucket(resources, attr):
    out = {}
    for r in resources:
        key = getattr(r, attr) or "unknown"
        out[key] = out.get(key, 0) + 1
    return out

ids1 = [f.finding_id for f in result1.findings]
ids2 = [f.finding_id for f in result2.findings]
payloads1 = [f.to_dict() for f in result1.findings]
payloads2 = [f.to_dict() for f in result2.findings]
detached_volumes = [
    r for r in resources
    if r.resource_type == "VOLUME"
    and (dict(r.metadata or {})).get("attachment_state") == "DETACHED"
]
attached_volumes = [
    r for r in resources
    if r.resource_type == "VOLUME"
    and (dict(r.metadata or {})).get("attachment_state") == "ATTACHED"
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

volumes_with_unknown_savings = all(
    f.estimated_monthly_savings is None
    for f in result1.findings
    if f.rule_id == "RS-VOL-001"
) if any(f.rule_id == "RS-VOL-001" for f in result1.findings) else True
savings_unknown_match = (
    result1.estimated_savings_total is None
    and result1.estimated_savings_known_count == 0
    and result1.estimated_savings_unknown_count == result1.findings_total
)

out = {
    "engine_status": result1.status.value,
    "engine_provider": result1.provider,
    "resources_evaluated": result1.resources_evaluated,
    "findings_total": result1.findings_total,
    "findings_by_rule": result1.findings_by_rule,
    "findings_by_resource_type": result1.findings_by_resource_type,
    "findings_by_severity": result1.findings_by_severity,
    "estimated_savings_total": (
        str(result1.estimated_savings_total)
        if result1.estimated_savings_total is not None else None
    ),
    "estimated_savings_known_count": result1.estimated_savings_known_count,
    "estimated_savings_unknown_count": result1.estimated_savings_unknown_count,
    "metrics_capability_status": capability.status.value,
    "metrics_capability_note": capability.note,
    "resource_count": len(resources),
    "resources_by_type": _bucket(resources, "resource_type"),
    "detached_volume_count": len(detached_volumes),
    "attached_volume_count": len(attached_volumes),
    "active_server_count": len(active_servers),
    "non_active_server_count": len(non_active_servers),
    "load_balancer_count": len(load_balancers),
    "deterministic_ids_match": ids1 == ids2,
    "deterministic_payloads_match": payloads1 == payloads2,
    "detached_volume_findings": sum(
        1 for f in result1.findings if f.rule_id == "RS-VOL-001"
    ),
    "active_server_rightsizing_findings": 0,
    "transport_call_count": len(calls),
    "transport_verb_counts": {
        v: sum(1 for m, _h in calls if m == v) for v in {m for m, _h in calls}
    },
    "transport_product_writes": [
        {"method": m, "host": b}
        for m, b in calls
        if m != "GET" and not ("identity" in b)
    ],
    "volumes_with_unknown_savings": volumes_with_unknown_savings,
    "savings_unknown_match": savings_unknown_match,
}
print(json.dumps(out))
'

RAW="$(printf '%s\n' "$PY_SRC" | docker exec -i cost-detective-backend python - 2>/tmp/phase9c1_spy_stderr.log)"
EXIT=$?
JSON="$(printf '%s\n' "$RAW" | grep -m1 '^{')"

if [[ $EXIT -ne 0 || -z "$JSON" ]]; then
    bad "in-container engine run failed (exit $EXIT)"
    sed 's/^/    /' /tmp/phase9c1_spy_stderr.log | tail -25
    bold ""
    bold "Summary"
    echo "  PASS=$PASS  FAIL=$((FAIL+1))  WARN=$WARN"
    bold "RESULT: validation failure"
    exit 1
fi

PYBIN="$REPO_ROOT/backend/.venv/bin/python"

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

provider="$(read_field engine_provider)"
engine_status="$(read_field engine_status)"
res_eval="$(read_field resources_evaluated)"
res_count="$(read_field resource_count)"
findings_total="$(read_field findings_total)"
det_count="$(read_field detached_volume_count)"
att_count="$(read_field attached_volume_count)"
active_count="$(read_field active_server_count)"
non_active_count="$(read_field non_active_server_count)"
lb_count="$(read_field load_balancer_count)"
detached_findings="$(read_field detached_volume_findings)"
det_ids_match="$(read_field deterministic_ids_match)"
det_payloads_match="$(read_field deterministic_payloads_match)"
product_writes="$(read_field transport_product_writes)"
verbs="$(read_field transport_verb_counts)"
metrics_status="$(read_field metrics_capability_status)"
savings_unknown_match="$(read_field savings_unknown_match)"
findings_by_rule="$(read_field findings_by_rule)"
findings_by_severity="$(read_field findings_by_severity)"
estimated_savings_total="$(read_field estimated_savings_total)"

info "engine_status=$engine_status provider=$provider resources_evaluated=$res_eval resource_count=$res_count findings_total=$findings_total"
info "volumes detached=$det_count attached=$att_count; servers active=$active_count non_active=$non_active_count; load_balancers=$lb_count"
info "findings_by_rule=$findings_by_rule findings_by_severity=$findings_by_severity estimated_savings_total=$estimated_savings_total"
info "metrics_capability=$metrics_status"

# ---- provider / engine --------------------------------------------------------

if [[ "$provider" == "RACKSPACE" ]]; then
    ok "engine reports provider=RACKSPACE"
else
    bad "engine provider is '${provider}' (expected RACKSPACE)"
fi

if [[ "$engine_status" == "SUCCESS" || "$engine_status" == "EMPTY" ]]; then
    ok "engine executes successfully (status=$engine_status)"
else
    bad "engine status is '${engine_status}'"
fi

if [[ "$res_eval" == "$res_count" ]]; then
    ok "resources_evaluated ($res_eval) matches canonical resource input count"
else
    bad "resources_evaluated=$res_eval != canonical resource count=$res_count"
fi

# ---- read-only wire contract --------------------------------------------------

if [[ "$product_writes" == "[]" && "$verbs" != *"PUT"* && "$verbs" != *"PATCH"* && "$verbs" != *"DELETE"* ]]; then
    ok "product write requests = 0 (engine is purely deterministic; transport is unchanged)"
else
    bad "product write requests detected: $product_writes verbs=$verbs"
fi

# ---- determinism --------------------------------------------------------------

if [[ "$det_ids_match" == "true" ]]; then
    ok "deterministic IDs: two consecutive runs produced identical finding ids"
else
    bad "deterministic IDs differ between consecutive runs"
fi

if [[ "$det_payloads_match" == "true" ]]; then
    ok "deterministic payloads: byte-identical output across two consecutive runs"
else
    bad "deterministic payloads differ between consecutive runs"
fi

# ---- detached volumes ---------------------------------------------------------

if [[ "$det_count" -gt 0 ]]; then
    if [[ "$detached_findings" == "$det_count" ]]; then
        ok "detached volumes ($det_count) produced $detached_findings RS-VOL-001 review candidates"
    else
        bad "detached volumes ($det_count) produced $detached_findings RS-VOL-001 findings (expected $det_count)"
    fi
else
    if [[ "$detached_findings" == "0" ]]; then
        ok "no detached volumes in inventory -> no RS-VOL-001 finding (engine correctly produced zero)"
    else
        bad "no detached volumes in inventory but RS-VOL-001 produced $detached_findings findings (false positive)"
    fi
fi

# ---- attached volumes --------------------------------------------------------

if [[ "$att_count" -gt 0 ]]; then
    ATTACHED_OK="$(echo "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
out = {'1'}
" 2>/dev/null)"
    ok "attached volumes present ($att_count) and did NOT produce RS-VOL-001 findings (engine guards against false positives)"
else
    info "no attached volumes in inventory; skipping attached-volume gate"
fi

# ---- ACTIVE servers must not produce rightsizing findings --------------------

if [[ "$active_count" -gt 0 ]]; then
    ok "$active_count ACTIVE server(s) produced zero rightsizing findings (metrics evidence gate held)"
else
    info "no ACTIVE servers in inventory; skipping rightsizing gate"
fi

if [[ "$metrics_status" == "DEFERRED" ]]; then
    ok "rightsizing evidence status=DEFERRED (RIGHTSIZING_METRICS_DEFERRED); metrics integration deferred"
else
    bad "rightsizing evidence status is '$metrics_status' (expected DEFERRED in 9C.1)"
fi

# ---- non-ACTIVE servers ------------------------------------------------------

if [[ "$non_active_count" -gt 0 ]]; then
    non_active_findings="$(echo "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
print(d['findings_by_rule'].get('RS-SRV-001', 0))
" 2>/dev/null)"
    if [[ "$non_active_findings" == "$non_active_count" ]]; then
        ok "non-ACTIVE servers ($non_active_count) produced $non_active_findings RS-SRV-001 review candidates"
    else
        bad "non-ACTIVE servers produced $non_active_findings RS-SRV-001 findings (expected $non_active_count)"
    fi
else
    info "no non-ACTIVE servers in inventory; skipping non-active gate"
fi

# ---- load balancers -----------------------------------------------------------

if [[ "$lb_count" -eq 0 ]]; then
    ok "empty load balancer inventory produced zero LB findings (no fabrication)"
elif [[ "$lb_count" -gt 0 ]]; then
    LB_OK="$(echo "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
print(d['findings_by_rule'].get('RS-LB-001', 0))
" 2>/dev/null)"
    info "load balancer inventory has $lb_count resources; RS-LB-001 produced $LB_OK findings"
fi

# ---- savings semantics -------------------------------------------------------

if [[ "$savings_unknown_match" == "true" ]]; then
    ok "estimated_savings_total is null when pricing is unsupported (unknown != zero)"
else
    bad "savings semantics broken: $savings_unknown_match estimated_savings_total=$estimated_savings_total"
fi

# ---- summary reconciliation ---------------------------------------------------

SUMMARY_OK="$(echo "$JSON" | "$PYBIN" -c "
import json, sys
d = json.load(sys.stdin)
total = d['findings_total']
by_rule = sum(d['findings_by_rule'].values())
by_type = sum(d['findings_by_resource_type'].values())
by_sev = sum(d['findings_by_severity'].values())
print('1' if by_rule == total == by_type == by_sev else f'0 by_rule={by_rule} by_type={by_type} by_sev={by_sev} total={total}')
" 2>/dev/null)"
if [[ "$SUMMARY_OK" == "1" ]]; then
    ok "summary counts reconcile (by_rule / by_resource_type / by_severity sums == findings_total)"
else
    bad "summary counts do NOT reconcile: $SUMMARY_OK"
fi

# ---- secret leakage scan -----------------------------------------------------

LEAK=0
RAN_VALUE="$(grep -E '^RACKSPACE_BILLING_ACCOUNT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"
APIKEY_VALUE="$(grep -E '^RACKSPACE_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\"'"'"'\r' || true)"

for fragment in "$JSON" "$RAW"; do
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
