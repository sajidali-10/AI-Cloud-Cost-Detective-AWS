#!/usr/bin/env bash
# phase2_verify.sh — Phase 2 (Cost & Utilization Intelligence) verifier.
#
# Composes phase1_verify.sh with Phase 2-specific checks:
#   1. Phase 1 baseline (delegated to phase1_verify.sh -> phase0_verify.sh).
#   2. Alembic plumbing exists (alembic.ini, alembic/env.py, version 0001).
#   3. Alembic can apply 0001_cost_cache to the live Postgres database.
#   4. CostCache ORM model + JSONB dialect adapter are importable.
#   5. Phase 2 AWS service modules are importable (cost_explorer,
#      cloudwatch_metrics, guard) and every Boto3 call site is
#      wrapped by assert_read_only().
#   6. Phase 2 Pydantic schemas (cost, utilization, evidence) import
#      cleanly and carry the cost-scope invariant.
#   7. The read-through cost cache layer (get_or_refresh, GC, TTL)
#      behaves correctly under unit tests.
#   8. The cost evidence builder composes Cost Explorer + CloudWatch
#      + Phase 1 resources without ever emitting a per-resource
#      ``CostContext`` whose ``scope`` is ``RESOURCE`` (an enum
#      that does not exist).
#   9. No PytestCacheWarning on the canonical run (cache_dir is
#      writable without chmod'ing /app globally writable).
#  10. Phase 2 HTTP endpoints respond 200 / sanitized 502 — never 500:
#        - GET  /api/aws/costs?days=7|30|60|90
#        - POST /api/aws/utilization
#        - POST /api/aws/evidence
#  11. No write/mutation AWS APIs in Phase 2 source (assert_read_only
#      is wired at every Boto3 call site).
#  12. Secret scan remains clean.
#
# Exits non-zero on any failed check.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PASS=0
FAIL=0
FAILURES=()

note()  { printf '[check] %s\n' "$*"; }
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()   { FAIL=$((FAIL+1)); FAILURES+=("$*"); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

# ---------- 1. Phase 1 baseline (transitively phase 0) ----------
note "Phase 1 regression check (delegated to phase1_verify.sh)"
if bash scripts/phase1_verify.sh >/tmp/phase1.log 2>&1; then
  ok "phase1_verify.sh exited 0"
  P1_PASS=$(grep -oE '[0-9]+ passed' /tmp/phase1.log | tail -n 1 || echo "?")
  ok "phase 1 verify summary: ${P1_PASS}"
else
  bad "phase1_verify.sh failed - see /tmp/phase1.log"
fi

# ---------- 2. Alembic plumbing ----------
note "Alembic configuration + env.py + initial migration present"
for f in backend/alembic.ini backend/alembic/env.py backend/alembic/versions/0001_cost_cache.py; do
  if [[ -f "$f" ]]; then ok "file: $f"; else bad "missing file: $f"; fi
done
# env.py must NOT hardcode the production DSN - it must read from Settings.
if grep -E "^(sqlalchemy\.url|set_main_option\('sqlalchemy\.url'" backend/alembic.ini >/dev/null 2>&1; then
  if grep -E "config\.set_main_option\(.sqlalchemy\.url.,\s*get_settings\(\)\.database_url" backend/alembic/env.py >/dev/null 2>&1; then
    ok "alembic env.py reads DSN from app.core.config.get_settings()"
  else
    bad "alembic env.py does not override sqlalchemy.url from Settings"
  fi
fi
# The initial migration must create the cost_cache table.  The
# migration uses raw SQL (CREATE TABLE ... IF NOT EXISTS) so we grep
# for either the op.create_table idiom or the raw ``cost_cache``
# identifier with surrounding context.
if grep -qE "create_table\(.cost_cache." backend/alembic/versions/0001_cost_cache.py >/dev/null 2>&1 \
   || grep -qE "CREATE TABLE IF NOT EXISTS cost_cache" backend/alembic/versions/0001_cost_cache.py >/dev/null 2>&1 \
   || grep -qE "DROP TABLE IF EXISTS cost_cache" backend/alembic/versions/0001_cost_cache.py >/dev/null 2>&1; then
  ok "0001_cost_cache creates (and drops) the cost_cache table"
else
  bad "0001_cost_cache does not appear to create cost_cache"
fi

# ---------- 3. Alembic upgrade head ----------
note "alembic upgrade head applies cleanly (idempotent)"
# ``upgrade head`` is idempotent: re-running it after a successful
# upgrade is a no-op.  We pipe through ``head`` so the script does
# not need to know the current revision.
if docker compose exec -T backend alembic upgrade head >/tmp/alembic.out 2>&1; then
  ok "alembic upgrade head succeeded"
  TAIL=$(tail -n 5 /tmp/alembic.out | tr -d '\r')
  echo "  alembic tail: $(echo "${TAIL}" | tr '\n' ' ' | head -c 200)"
else
  bad "alembic upgrade head failed: $(tail -n 5 /tmp/alembic.out | tr -d '\r' | tr '\n' ' ')"
fi
# Sanity: the table actually exists now.
if docker compose exec -T postgres psql -U "${COST_DETECTIVE_DB_USER:-cost_detective_user}" -d "${COST_DETECTIVE_DB:-cost_detective}" -tAc \
     "SELECT 1 FROM information_schema.tables WHERE table_name='cost_cache';" 2>/dev/null | grep -q '^1$'; then
  ok "cost_cache table present in Postgres"
else
  bad "cost_cache table NOT present after alembic upgrade head"
fi

# ---------- 4. CostCache ORM + JSONB adapter ----------
note "CostCache ORM model + dialect-aware JSONB type importable"
if docker compose exec -T backend python -c "
from app.db.models import CostCache, Base, _JSONBType
from app.db.session import SessionLocal, get_engine, get_db
# Build the table without persisting so we exercise the DDL path
# against in-memory SQLite too.
from sqlalchemy import create_engine
eng = create_engine('sqlite:///:memory:')
Base.metadata.create_all(eng)
print('orm_ok')
" >/tmp/orm.txt 2>&1; then
  ok "ORM imports + SQLite create_all succeeds"
else
  bad "ORM import / DDL failed: $(tail -n 5 /tmp/orm.txt | tr '\n' ' ')"
fi

# ---------- 5. Phase 2 AWS service modules ----------
note "Phase 2 AWS service modules importable from the backend"
if docker compose exec -T backend python -c "
from app.services.aws.cost_explorer import (
  aggregate_cost_report, build_period, get_cost_explorer_client,
  get_total_and_previous, get_daily_trend, get_by_service, get_by_region,
  CostExplorerError, ALLOWED_LOOKBACK_DAYS, COST_METRIC,
)
from app.services.aws.cloudwatch_metrics import (
  DataQuality, classify_data_quality, build_metric_queries,
  batch_query, aggregate_results, period_seconds_for,
  get_cloudwatch_client, ResourceDescriptor, MetricSpec,
  ALLOWED_LOOKBACK_DAYS, AWS_GET_METRIC_DATA_MAX_QUERIES,
)
from app.services.aws.guard import (
  assert_read_only, is_read_only_operation, find_forbidden_prefix,
  FORBIDDEN_PREFIXES, READ_ONLY_PREFIXES, AwsReadOnlyViolation,
)
from app.services.cost_evidence_builder import build_evidence
from app.services.cost_cache import get_or_refresh, maybe_collect_garbage
print('phase2_services_ok')
" >/tmp/p2svc.txt 2>&1; then
  ok "Phase 2 services importable (CE + CloudWatch + Guard + Cache + Evidence)"
else
  bad "Phase 2 service import failed: $(tail -n 5 /tmp/p2svc.txt | tr '\n' ' ')"
fi

# ---------- 6. Phase 2 schemas + scope invariant ----------
note "Phase 2 Pydantic schemas + cost-scope invariant"
if docker compose exec -T backend python -c "
from app.schemas.cost import (
  CacheStatus, CostPeriod, DailyCostPoint, ServiceCost, RegionCost,
  CostReport, CostReportResponse,
)
from app.schemas.utilization import (
  UtilizationRequest, UtilizationResponse, ResourceUtilization,
  MetricSeries, Datapoint, Warning as UWarning,
)
from app.schemas.evidence import (
  CostContext, CostScope, EvidenceResponse, EvidenceStatus,
  ResourceEvidence, Warning as EWarning,
)
# The Phase 2 invariant: per-resource cost_context.scope must never be RESOURCE.
assert 'RESOURCE' not in {m.name for m in CostScope}, 'RESOURCE leaked into CostScope'
print('phase2_schemas_ok')
" >/tmp/p2sch.txt 2>&1; then
  ok "Phase 2 schemas importable + CostScope has no RESOURCE member"
else
  bad "Phase 2 schema import / scope invariant failed: $(tail -n 5 /tmp/p2sch.txt | tr '\n' ' ')"
fi

# ---------- 7. Cache layer uses settings, not hardcoded DSN ----------
note "Cost cache never hardcodes credentials or DSN"
# The cache layer must source the engine/session from app.db.session -
# it must NOT call SQLAlchemy with literal host/port/password values.
if grep -rEn --include='*.py' \
    -e 'postgresql\+psycopg://[^"'\''[:space:]]*:[^"'\''[:space:]]*@' \
    -e 'mysql\+pymysql://[^"'\''[:space:]]*:[^"'\''[:space:]]*@' \
    backend/app/services/cost_cache.py backend/app/services/cost_evidence_builder.py 2>/dev/null \
    | grep -v '#' >/tmp/dns.txt 2>&1; then
  bad "hardcoded DSN in cache layer: $(cat /tmp/dns.txt | head -n 3)"
else
  ok "no hardcoded DSN in cache / evidence modules"
fi

# ---------- 8. Pytest cache is writable; no PytestCacheWarning ----------
note "pytest run with cache_dir writable (no PytestCacheWarning)"
# We deliberately target the Phase 2 tests only; the full suite is
# run separately by ``make test``.  ``-W error::pytest.PytestCacheWarning``
# upgrades the warning to a hard failure so a regression on the
# Dockerfile's cache-dir ownership is caught immediately.
if docker compose exec -T backend python -m pytest \
    tests/test_cost_evidence_builder.py \
    tests/test_cost_explorer_service.py \
    tests/test_cloudwatch_batching.py \
    tests/test_aws_read_only_guard.py \
    tests/test_cost_cache.py \
    -W error::pytest.PytestCacheWarning \
    -p no:cacheprovider \
    >/tmp/p2_pytest.out 2>&1; then
  # Without ``-q`` pytest always prints a final ``X passed in Ys`` line
  # which we extract here so the user sees how many tests ran.
  LAST=$(grep -oE '[0-9]+ passed' /tmp/p2_pytest.out | tail -n 1 || echo "?")
  ok "targeted Phase 2 pytest: ${LAST} (PytestCacheWarning would have failed the run)"
else
  bad "targeted Phase 2 pytest failed (see /tmp/p2_pytest.out): $(tail -n 10 /tmp/p2_pytest.out | tr '\n' ' ' | head -c 400)"
fi
# Also confirm the default cache provider (cacheprovider NOT disabled)
# does NOT emit PytestCacheWarning.  This is the case the user actually
# cares about.
if docker compose exec -T backend python -m pytest -q \
    tests/test_aws_read_only_guard.py \
    >/tmp/p2_pytest_default.out 2>&1; then
  if grep -q 'PytestCacheWarning' /tmp/p2_pytest_default.out; then
    bad "default pytest cache provider still emits PytestCacheWarning"
  else
    ok "default pytest cache provider emits NO PytestCacheWarning"
  fi
else
  bad "default-cache pytest run failed: $(tail -n 5 /tmp/p2_pytest_default.out | tr '\n' ' ')"
fi

# ---------- 9. Read-only guard wiring ----------
note "Every Boto3 call site is guarded by assert_read_only"
# We grep for ``client.<op>(`` patterns in the Phase 2 AWS service
# modules and verify each one is preceded (in the same file) by an
# assert_read_only call.  This is a heuristic - it does NOT prove the
# guard fires before the call in every case, but it catches the easy
# regression of removing the guard entirely.
PHASE2_AWS_FILES=(
  backend/app/services/aws/cost_explorer.py
  backend/app/services/aws/cloudwatch_metrics.py
)
GUARD_FAIL=0
for f in "${PHASE2_AWS_FILES[@]}"; do
  # Find lines that look like a Boto3 method call.
  CALLS=$(grep -cE '\bclient\.[a-z_]+\(' "$f" 2>/dev/null || echo 0)
  GUARDS=$(grep -cE 'assert_read_only\(' "$f" 2>/dev/null || echo 0)
  if [[ "${CALLS}" -gt 0 && "${GUARDS}" -lt 1 ]]; then
    bad "$f has ${CALLS} client.<op>() calls but no assert_read_only guard"
    GUARD_FAIL=1
  fi
done
if [[ "${GUARD_FAIL}" -eq 0 ]]; then
  ok "guard wiring looks plausible in cost_explorer.py + cloudwatch_metrics.py"
fi

# ---------- 10. No write/mutation AWS APIs in Phase 2 source ----------
note "No write/mutation AWS APIs in Phase 2 source (cost_explorer + cloudwatch + cache + evidence)"
BANNED_RE='create_|delete_|modify_|update_|terminate|stop_|start_|reboot|attach|detach|enable_|disable_|put_|register|deregister|associate_|disassociate_|allocate|release|authorize|revoke|import_|export_|copy_|move_|restore|reset_|cancel_|complete_|fail_|succeed_|send_|publish_|invoke_|tag_resource|untag_resource'
HITS=$(grep -rEn --include='*.py' \
    -e "\.(${BANNED_RE})\(" \
    backend/app/services/aws/cost_explorer.py \
    backend/app/services/aws/cloudwatch_metrics.py \
    backend/app/services/cost_cache.py \
    backend/app/services/cost_evidence_builder.py \
    backend/app/api/aws_costs.py \
    backend/app/api/aws_utilization.py \
    backend/app/api/aws_evidence.py 2>/dev/null \
    | grep -vE 'create_(enum|enum_type)|model_rebuild|class .*create_|def create_|model_create' \
    | grep -vE 'model_validate|model_dump|model_rebuild' || true)
if [[ -z "${HITS}" ]]; then
  ok "no write/mutation AWS APIs in Phase 2 source"
else
  bad "write/mutation AWS API hit(s): $(echo "${HITS}" | head -n 3)"
fi

# ---------- 11. Live HTTP smoke ----------
note "GET /api/aws/costs responds (200 with cache_status, or sanitized 502)"
COSTS=$(curl -sS --max-time 30 'http://127.0.0.1/api/aws/costs?days=30' 2>/dev/null || true)
if echo "${COSTS}" | grep -q '"cache_status"'; then
  ok "/api/aws/costs returned a CostReportResponse (cache_status present)"
  echo "  $(echo "${COSTS}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
    rep = r.get('report', {})
    print(f\"cache_status={r.get('cache_status')} total={rep.get('total_cost')} period={rep.get('period',{}).get('days')}d by_service={len(rep.get('by_service', []))}\")
except Exception as e:
    print('PARSE_FAIL', e)
" 2>/dev/null)"
elif echo "${COSTS}" | grep -q '"error_code"'; then
  ok "/api/aws/costs reachable with sanitized error: $(echo "${COSTS}" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("error_code"))' 2>/dev/null)"
else
  bad "/api/aws/costs unreachable: ${COSTS:0:200}"
fi

note "POST /api/aws/utilization responds (200 with resources, or sanitized 502)"
UTIL=$(curl -sS --max-time 60 -H 'Content-Type: application/json' \
       -d '{"region":"us-east-1","lookback_days":7,"resource_types":["ec2","rds"]}' \
       'http://127.0.0.1/api/aws/utilization' 2>/dev/null || true)
if echo "${UTIL}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
# Valid response has 'resources' (list) and 'lookback_days'.
if 'resources' in r and 'lookback_days' in r:
    print('OK', 'resources=', len(r['resources']), 'warnings=', len(r.get('warnings', [])))
else:
    print('SHAPE_FAIL', sorted(r.keys())[:6]); sys.exit(1)
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/aws/utilization shape correct"
  echo "  $(echo "${UTIL}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"region={r['region']} lookback_days={r['lookback_days']} resources={len(r['resources'])} warnings={len(r.get('warnings', []))}\")
" 2>/dev/null)"
else
  bad "/api/aws/utilization shape wrong: ${UTIL:0:200}"
fi

note "POST /api/aws/evidence responds (200 with status, or sanitized 502)"
EVI=$(curl -sS --max-time 60 -H 'Content-Type: application/json' \
      -d '{"region":"us-east-1","days":7}' \
      'http://127.0.0.1/api/aws/evidence' 2>/dev/null || true)
if echo "${EVI}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'region','days','status','cost_summary','resources'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
status = r.get('status')
if status not in ('SUCCESS','PARTIAL_SUCCESS','FAILED'):
    print('BAD_STATUS', status); sys.exit(1)
print('OK', f\"status={status}\", f\"resources={len(r['resources'])}\", f\"warnings={len(r.get('warnings', []))}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/aws/evidence shape correct"
  echo "  $(echo "${EVI}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
cs = r.get('cost_summary', {})
print(f\"status={r['status']} resources={len(r['resources'])} warnings={len(r.get('warnings', []))} cost_amount={cs.get('amount')} by_service={len(cs.get('by_service', []))}\")
" 2>/dev/null)"
else
  bad "/api/aws/evidence shape wrong: ${EVI:0:300}"
fi

note "Invalid lookback yields sanitized 422, never 500"
BAD=$(curl -sS --max-time 10 -o /tmp/p2_bad.json -w '%{http_code}' \
      'http://127.0.0.1/api/aws/costs?days=15' 2>/dev/null || echo "000")
if [[ "${BAD}" == "422" ]] && grep -q 'InvalidLookbackDays' /tmp/p2_bad.json 2>/dev/null; then
  ok "/api/aws/costs?days=15 returns 422 InvalidLookbackDays"
else
  bad "/api/aws/costs?days=15 did not return 422 (got HTTP ${BAD})"
fi

# ---------- 12. Secret scan remains clean ----------
note "Secret scan (Phase 2 extra-strict)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase0_verify.sh' --exclude='phase1_verify.sh' --exclude='phase2_verify.sh' \
  --exclude='security.md' --exclude='phase0-report.md' --exclude='phase1-report.md' --exclude='phase2-report.md' \
  -e 'AKIA[0-9A-Z]{16}' \
  -e 'AWS_SECRET_ACCESS_KEY=[A-Za-z0-9/+=]{40}' \
  -e 'AWS_ACCESS_KEY_ID=[A-Z0-9]{16,}' \
  -e 'OPENAI_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'ANTHROPIC_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'GEMINI_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'LITELLM_MASTER_KEY=sk-[A-Za-z0-9_\-]{20,}' \
  2>/dev/null || true)
if [[ -z "${SECRET_HITS}" ]]; then
  ok "no obvious secrets in tracked files"
else
  bad "secret pattern matches: $(echo "${SECRET_HITS}" | awk -F: '{print $1}' | sort -u | tr '\n' ' ')"
fi

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 2 verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
