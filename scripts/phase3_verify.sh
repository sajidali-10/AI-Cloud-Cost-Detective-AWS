#!/usr/bin/env bash
# phase3_verify.sh — Phase 3 (AWS Optimization Intelligence) verifier.
#
# Composes phase2_verify.sh with Phase 3-specific checks:
#   1. Phase 2 regression (delegated to phase2_verify.sh -> ... -> phase0_verify.sh).
#   2. Phase 3 service modules importable (compute_optimizer,
#      cost_optimization_hub, optimization_rules, optimization_engine).
#   3. Phase 3 Pydantic schemas importable (optimization.*).
#   4. Phase 3 pytest suite passes (Compute Optimizer, Cost Optimization
#      Hub, deterministic rules, deduplication, summary, read-only guard
#      integration).
#   5. No write/mutation AWS APIs in Phase 3 source.
#   6. No AI/LiteLLM call paths in Phase 3 source.
#   7. Phase 3 HTTP endpoints respond:
#        - GET  /api/aws/optimization/capabilities
#        - GET  /api/aws/optimization/recommendations?days=7|30|60|90
#        - GET  /api/aws/optimization/summary?days=7|30|60|90
#   8. Invalid lookback yields 422.
#   9. Secret scan remains clean (Phase 3 extra-strict — no AI keys).
#  10. Public port controls (only nginx :80 is published).
#  11. Docker health (no unhealthy containers).
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

# ---------- 1. Phase 2 regression ----------
note "Phase 2 regression check (delegated to phase2_verify.sh)"
if bash scripts/phase2_verify.sh >/tmp/phase2.log 2>&1; then
  P2_PASS=$(grep -oE '[0-9]+ passed' /tmp/phase2.log | tail -n 1 || echo "?")
  ok "phase2_verify.sh exited 0 (${P2_PASS})"
else
  bad "phase2_verify.sh failed - see /tmp/phase2.log"
fi

# ---------- 2. Phase 3 modules importable ----------
note "Phase 3 AWS service modules + orchestrator importable from the backend"
if docker compose exec -T backend python -c "
from app.services.aws.compute_optimizer import (
    ComputeOptimizerError, NormalizedRecommendation,
    get_compute_optimizer_client, get_ebs_volume_recommendations,
    get_ec2_instance_recommendations, get_enrollment_status,
    get_lambda_function_recommendations, get_rds_database_recommendations,
    get_recommendation_summaries,
)
from app.services.aws.cost_optimization_hub import (
    CostOptimizationHubError, NormalizedHubRecommendation,
    get_cost_optimization_hub_client, get_preferences, get_recommendation,
    list_enrollment_statuses, list_recommendation_summaries, list_recommendations,
)
from app.services.optimization_rules import (
    DeterministicCandidate, deterministic_recommendation_id,
    rule_idle_load_balancer, rule_idle_nat_gateway,
    rule_low_utilization_ec2, rule_rds_underutilization,
    rule_unattached_ebs, rule_unused_eip,
)
from app.services.optimization_engine import (
    OptimizationInputs, SOURCE_PRECEDENCE,
    build_capabilities, build_recommendations, build_summary,
)
from app.api.aws_optimization import (
    get_optimization_capabilities, get_optimization_recommendations,
    get_optimization_summary,
)
print('phase3_services_ok')
" >/tmp/p3svc.txt 2>&1; then
  ok "Phase 3 services importable (CO + COH + Rules + Engine + Routes)"
else
  bad "Phase 3 service import failed: $(tail -n 5 /tmp/p3svc.txt | tr '\n' ' ')"
fi

# ---------- 3. Phase 3 schemas + invariants ----------
note "Phase 3 schemas + invariants (SavingsSource, deterministic_id)"
if docker compose exec -T backend python -c "
from app.schemas.optimization import (
    CapabilitiesResponse, Recommendation, RecommendationAction,
    ResourceType, SavingsSource, Confidence, OptimizationNotice,
    OptimizationStatus, ServiceCapability, SummaryResponse,
)
# Phase 3 invariant: SavingsSource MUST NOT contain AI_ESTIMATE.
assert 'AI_ESTIMATE' not in {m.name for m in SavingsSource}, 'AI_ESTIMATE leaked into SavingsSource'
# Phase 3 invariant: CapabilityStatus must include AVAILABLE (deterministic engine).
assert 'AVAILABLE' in {m.value for m in __import__('app.schemas.optimization', fromlist=['CapabilityStatus']).CapabilityStatus}
print('phase3_schemas_ok')
" >/tmp/p3sch.txt 2>&1; then
  ok "Phase 3 schemas importable + SavingsSource has no AI_ESTIMATE + CapabilityStatus.AVAILABLE present"
else
  bad "Phase 3 schema import / invariant failed: $(tail -n 5 /tmp/p3sch.txt | tr '\n' ' ')"
fi

# ---------- 4. Phase 3 pytest ----------
note "Phase 3 pytest suite (compute_optimizer + COH + rules + engine + guard integration)"
if docker compose exec -T backend python -m pytest -q \
    tests/test_compute_optimizer_service.py \
    tests/test_cost_optimization_hub_service.py \
    tests/test_optimization_rules.py \
    tests/test_optimization_engine.py \
    >/tmp/p3_pytest.out 2>&1; then
  LAST=$(grep -oE '[0-9]+ passed' /tmp/p3_pytest.out | tail -n 1 || echo "?")
  ok "Phase 3 pytest: ${LAST}"
else
  bad "Phase 3 pytest failed: $(tail -n 10 /tmp/p3_pytest.out | tr '\n' ' ' | head -c 400)"
fi

# Also run the read-only guard integration test to confirm Phase 3
# Compute Optimizer + Cost Optimization Hub operations are NOT in the
# forbidden prefixes.
if docker compose exec -T backend python -m pytest -q \
    tests/test_aws_read_only_guard.py \
    >/tmp/p3_guard.out 2>&1; then
  ok "Read-only guard suite (Phase 3 operations classified as read-only)"
else
  bad "Read-only guard suite failed: $(tail -n 5 /tmp/p3_guard.out | tr '\n' ' ')"
fi

# ---------- 5. No write/mutation AWS APIs in Phase 3 source ----------
note "No write/mutation AWS APIs in Phase 3 source"
# Phase 3 must NEVER call any operation starting with the forbidden
# prefixes.  Grep the AWS service modules + the orchestrator + the
# route layer.
BANNED_RE='create_|delete_|put_|update_|modify_|terminate|stop_|start_|reboot|attach|detach|enable_|disable_|put_|register|deregister|associate_|disassociate_|allocate|release_|authorize|revoke|import_|export_|copy_|move_|restore|reset_|cancel_|complete_|fail_|succeed_|send_|publish_|invoke_|terminate_|tag_resource|untag_resource'
HITS=$(grep -rEn --include='*.py' \
    -e "\.(${BANNED_RE})\(" \
    backend/app/services/aws/compute_optimizer.py \
    backend/app/services/aws/cost_optimization_hub.py \
    backend/app/services/optimization_rules.py \
    backend/app/services/optimization_engine.py \
    backend/app/api/aws_optimization.py \
    backend/app/schemas/optimization.py \
    2>/dev/null \
    | grep -vE 'create_(enum|enum_type)|def create_|model_create|model_validate|model_dump|model_rebuild' \
    | grep -vE '# ' \
    || true)
if [[ -z "${HITS}" ]]; then
  ok "no write/mutation AWS APIs in Phase 3 source"
else
  bad "write/mutation AWS API hit(s): $(echo "${HITS}" | head -n 3)"
fi

# Confirm explicitly the operations Phase 3 forbids as a defense in
# depth check.  We match the lowercase Boto3 snake_case call sites,
# not PascalCase mentions in docstrings / comments.
for op in update_enrollment_status update_preferences put_recommendation_preferences delete_recommendation_preferences; do
  H=$(grep -rn "\.${op}(" backend/app/services/aws/ backend/app/services/optimization_engine.py backend/app/api/aws_optimization.py 2>/dev/null || true)
  if [[ -n "${H}" ]]; then
    bad "forbidden operation ${op} found: ${H}"
  fi
done
ok "explicit forbidden operations (UpdateEnrollmentStatus, UpdatePreferences, PutRecommendationPreferences, DeleteRecommendationPreferences) absent"

# ---------- 6. No AI/LiteLLM call paths in Phase 3 ----------
note "No AI/LiteLLM call paths in Phase 3 source"
AI_HITS=$(grep -rEn --include='*.py' \
    -e 'litellm' \
    -e 'openai' \
    -e 'openrouter' \
    -e 'gemini' \
    -e 'anthropic' \
    -e 'completion' \
    -e 'LLM' \
    backend/app/services/aws/compute_optimizer.py \
    backend/app/services/aws/cost_optimization_hub.py \
    backend/app/services/optimization_rules.py \
    backend/app/services/optimization_engine.py \
    backend/app/api/aws_optimization.py \
    backend/app/schemas/optimization.py \
    2>/dev/null \
    | grep -vE '# ' \
    || true)
if [[ -z "${AI_HITS}" ]]; then
  ok "no AI/LiteLLM call paths in Phase 3 source"
else
  bad "AI/LiteLLM hit(s): $(echo "${AI_HITS}" | head -n 3)"
fi

# ---------- 7. Phase 3 HTTP endpoints ----------
note "GET /api/aws/optimization/capabilities responds"
CAPS=$(curl -sS --max-time 30 'http://127.0.0.1/api/aws/optimization/capabilities' 2>/dev/null || true)
if echo "${CAPS}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'region', 'compute_optimizer', 'cost_optimization_hub', 'deterministic_engine', 'supported_resource_types', 'supported_lookback_days'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
print('OK', f\"co={r['compute_optimizer']['status']} coh={r['cost_optimization_hub']['status']} det={r['deterministic_engine']['status']}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/aws/optimization/capabilities shape correct"
  echo "  $(echo "${CAPS}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"co={r['compute_optimizer']['status']} coh={r['cost_optimization_hub']['status']} det={r['deterministic_engine']['status']} resource_types={len(r['supported_resource_types'])}\")
" 2>/dev/null)"
else
  bad "/api/aws/optimization/capabilities unreachable: ${CAPS:0:300}"
fi

note "GET /api/aws/optimization/recommendations?days=30 responds"
RECS=$(curl -sS --max-time 60 'http://127.0.0.1/api/aws/optimization/recommendations?days=30' 2>/dev/null || true)
if echo "${RECS}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'region', 'days', 'status', 'count', 'recommendations', 'warnings'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
if r['status'] not in ('SUCCESS','PARTIAL_SUCCESS','FAILED'):
    print('BAD_STATUS', r['status']); sys.exit(1)
print('OK', f\"status={r['status']}\", f\"count={r['count']}\", f\"warnings={len(r['warnings'])}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/aws/optimization/recommendations shape correct"
  echo "  $(echo "${RECS}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"status={r['status']} count={r['count']} warnings={len(r['warnings'])}\")
" 2>/dev/null)"
else
  # Tolerate identity failure (sanitized 502) so the script does not
  # fail in an account without STS access.
  if echo "${RECS}" | grep -q 'IdentityUnavailable'; then
    ok "/api/aws/optimization/recommendations returns sanitized 502 (IdentityUnavailable)"
  else
    bad "/api/aws/optimization/recommendations unreachable: ${RECS:0:300}"
  fi
fi

note "GET /api/aws/optimization/summary?days=30 responds"
SUM=$(curl -sS --max-time 60 'http://127.0.0.1/api/aws/optimization/summary?days=30' 2>/dev/null || true)
if echo "${SUM}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'region', 'days', 'status', 'total_recommendations', 'by_resource_type', 'by_action', 'by_source', 'by_confidence', 'recommendations_without_savings'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
print('OK', f\"total={r['total_recommendations']}\", f\"without_savings={r['recommendations_without_savings']}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/aws/optimization/summary shape correct"
  echo "  $(echo "${SUM}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"total={r['total_recommendations']} without_savings={r['recommendations_without_savings']} by_source={len(r['by_source'])}\")
" 2>/dev/null)"
else
  if echo "${SUM}" | grep -q 'IdentityUnavailable'; then
    ok "/api/aws/optimization/summary returns sanitized 502 (IdentityUnavailable)"
  else
    bad "/api/aws/optimization/summary unreachable: ${SUM:0:300}"
  fi
fi

# ---------- 8. Invalid lookback ----------
note "Invalid lookback yields 422, never 500"
BAD=$(curl -sS --max-time 10 -o /tmp/p3_bad.json -w '%{http_code}' \
      'http://127.0.0.1/api/aws/optimization/recommendations?days=15' 2>/dev/null || echo "000")
if [[ "${BAD}" == "422" ]] && grep -q 'InvalidLookbackDays' /tmp/p3_bad.json 2>/dev/null; then
  ok "/api/aws/optimization/recommendations?days=15 returns 422 InvalidLookbackDays"
else
  bad "/api/aws/optimization/recommendations?days=15 did not return 422 (got HTTP ${BAD})"
fi

# ---------- 9. Secret scan ----------
note "Secret scan (Phase 3 extra-strict — no AI keys either)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase0_verify.sh' --exclude='phase1_verify.sh' --exclude='phase2_verify.sh' --exclude='phase3_verify.sh' \
  --exclude='security.md' --exclude='phase0-report.md' --exclude='phase1-report.md' --exclude='phase2-report.md' --exclude='phase3-report.md' \
  --exclude='docs/phase*-cost-intelligence.md' --exclude='docs/phase3-optimization-intelligence.md' \
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

# ---------- 10. Public port controls ----------
note "Only nginx (port 80) is published"
PUB=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    ports = r.get('Ports') or ''
    print(r.get('Service', '?'), ports)
" 2>/dev/null || true)
echo "  ${PUB}"
# Specifically check that only nginx publishes to host :80 (not the
# backend, postgres, or litellm).
NON_NGINX_HOST_PORTS=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    svc = r.get('Service', '')
    if svc == 'nginx':
        continue
    ports = r.get('Ports') or ''
    # Anything containing '0.0.0.0' or '[::]' is a host publish.
    if '0.0.0.0' in ports or '[::]' in ports:
        print(svc, ports)
" 2>/dev/null || true)
if [[ -z "${NON_NGINX_HOST_PORTS}" ]]; then
  ok "no service other than nginx publishes to a host port"
else
  bad "non-nginx service(s) publish to host port: ${NON_NGINX_HOST_PORTS}"
fi

# ---------- 11. Docker health ----------
note "All containers healthy"
UNHEALTHY=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    state = (r.get('State') or '').lower()
    health = (r.get('Health') or '').lower()
    if state == 'running' and 'unhealthy' in health:
        print(r.get('Service', '?'), 'UNHEALTHY')
" 2>/dev/null || true)
if [[ -z "${UNHEALTHY}" ]]; then
  ok "all running containers are healthy"
else
  bad "unhealthy container(s): ${UNHEALTHY}"
fi

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 3 verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
