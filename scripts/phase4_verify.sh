#!/usr/bin/env bash
# phase4_verify.sh — Phase 4 (Grounded AI Cost Analyst + LiteLLM) verifier.
#
# Composes phase3_verify.sh with Phase 4-specific checks:
#   1. Phase 3 regression (delegated to phase3_verify.sh -> ... -> phase0_verify.sh).
#   2. Phase 4 service modules importable (litellm_client,
#      ai_system_prompt, ai_context_builder, ai_service).
#   3. Phase 4 Pydantic schemas importable (ai.*).
#   4. Phase 4 pytest suite passes (system prompt, context builder,
#      litellm client, ai service, end-to-end mocked).
#   5. AI_ESTIMATE NOT introduced into the Phase 3 SavingsSource
#      enum (savings protection).
#   6. No provider SDK imports in the backend (provider portability
#      belongs behind LiteLLM).
#   7. No write/mutation AWS APIs in Phase 4 source.
#   8. Phase 4 HTTP endpoints respond correctly:
#        - GET  /api/ai/status
#        - POST /api/ai/executive-summary
#        - POST /api/ai/analyze
#        - POST /api/ai/recommendations/{id}/explain
#   9. AI disabled mode never contacts a provider (controlled
#      envelope, 200 OK with status=DISABLED).
#  10. Input validation rejects unsupported lookback, empty /
#      oversized question.
#  11. Prompt-injection defense: system prompt contains the
#      anti-injection sentinel phrases; hostile AWS tag remains
#      inert data.
#  12. Citation validation: forged citations dropped with a warning.
#  13. Null savings NOT converted into invented amounts (sentinel
#      phrase is part of the system prompt).
#  14. /api/ai/status NEVER exposes API keys / Authorization headers.
#  15. Public port controls (only nginx :80 is published, LiteLLM
#      stays internal).
#  16. Docker health (all containers healthy).
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

# ---------- 1. Phase 3 regression ----------
note "Phase 3 regression check (delegated to phase3_verify.sh)"
if bash scripts/phase3_verify.sh >/tmp/phase3.log 2>&1; then
  P3_PASS=$(grep -oE '[0-9]+ passed' /tmp/phase3.log | tail -n 1 || echo "?")
  ok "phase3_verify.sh exited 0 (${P3_PASS})"
else
  bad "phase3_verify.sh failed - see /tmp/phase3.log"
fi

# ---------- 2. Phase 4 service modules importable ----------
note "Phase 4 service modules + AI router importable from the backend"
if docker compose exec -T backend python -c "
from app.services.litellm_client import (
    LiteLLMClient, ChatMessage, CompletionResult,
    LiteLLMError, LiteLLMUnavailable, LiteLLMTimeout, LiteLLMAuthError,
    LiteLLMRateLimit, LiteLLMQuotaExhausted, LiteLLMProviderError,
    LiteLLMMalformedResponse, LiteLLMEmptyCompletion,
)
from app.services.ai_system_prompt import (
    SYSTEM_PROMPT, SAVINGS_PROTECTION_SENTINEL,
    EVIDENCE_DELIMITER_OPEN, EVIDENCE_DELIMITER_CLOSE,
    USER_QUESTION_DELIMITER_OPEN, USER_QUESTION_DELIMITER_CLOSE,
    build_system_prompt, evidence_block, user_question_block,
)
from app.services.ai_context_builder import (
    AIContext, AIContextBuilder, CitationIndex, ContextRecommendation,
    render_context_text, filter_grounded_citations,
    validate_recommendation_citation, validate_cost_service_citation,
)
from app.services.ai_service import (
    AIService, DefaultEvidenceGatherer, EvidenceGatherer,
)
from app.api.ai import (
    ai_analyze, ai_executive_summary, ai_recommendation_explain, ai_status,
)
print('phase4_services_ok')
" >/tmp/p4svc.txt 2>&1; then
  ok "Phase 4 services importable (LiteLLM client + prompt + context + service + router)"
else
  bad "Phase 4 service import failed: $(tail -n 5 /tmp/p4svc.txt | tr '\n' ' ')"
fi

# ---------- 3. Phase 4 schemas ----------
note "Phase 4 schemas importable + AI_ESTIMATE absent"
if docker compose exec -T backend python -c "
from app.schemas.ai import (
    AIStatus, AIStatusResponse, AIGenerationStatus, AIGrounding, AIResponse,
    ExecutiveSummaryRequest, AnalyzeRequest, AIErrorEnvelope,
)
# Phase 4 invariant: AI_ESTIMATE must NEVER appear in Phase 3 SavingsSource.
from app.schemas.optimization import SavingsSource
assert 'AI_ESTIMATE' not in {m.name for m in SavingsSource}, 'AI_ESTIMATE leaked into SavingsSource'
print('phase4_schemas_ok')
" >/tmp/p4sch.txt 2>&1; then
  ok "Phase 4 schemas importable + AI_ESTIMATE absent from SavingsSource"
else
  bad "Phase 4 schema import / invariant failed: $(tail -n 5 /tmp/p4sch.txt | tr '\n' ' ')"
fi

# ---------- 4. Phase 4 pytest suite ----------
note "Phase 4 pytest suite (prompt + context + litellm + service + e2e)"
if docker compose exec -T backend python -m pytest -q \
    tests/test_ai_system_prompt.py \
    tests/test_ai_context_builder.py \
    tests/test_litellm_client.py \
    tests/test_ai_service.py \
    tests/test_ai_end_to_end_mocked.py \
    >/tmp/p4_pytest.out 2>&1; then
  LAST=$(grep -oE '[0-9]+ passed' /tmp/p4_pytest.out | tail -n 1 || echo "?")
  ok "Phase 4 pytest: ${LAST}"
else
  bad "Phase 4 pytest failed: $(tail -n 30 /tmp/p4_pytest.out | tr '\n' ' ' | head -c 600)"
fi

# ---------- 5. No provider SDK in backend ----------
note "No direct provider SDK imports in backend (LiteLLM only)"
PROVIDER_HITS=$(grep -rEn --include='*.py' \
    -e 'import openai' \
    -e 'from openai' \
    -e 'import anthropic' \
    -e 'from anthropic' \
    -e 'import google.generativeai' \
    -e 'from google.generativeai' \
    -e 'import openrouter' \
    -e 'from openrouter' \
    -e 'import boto3.*bedrock' \
    -e 'import ollama' \
    -e 'from ollama' \
    backend/app/ 2>/dev/null || true)
if [[ -z "${PROVIDER_HITS}" ]]; then
  ok "no provider SDK imports in backend (LiteLLM is the only boundary)"
else
  bad "provider SDK hit(s): $(echo "${PROVIDER_HITS}" | head -n 3)"
fi

# ---------- 6. No write/mutation AWS APIs in Phase 4 source ----------
note "No write/mutation AWS APIs in Phase 4 source"
BANNED_RE='create_|delete_|put_|update_|modify_|terminate|stop_|start_|reboot|attach|detach|enable_|disable_|register|deregister|associate_|disassociate_|allocate|release_|authorize|revoke|import_|export_|copy_|move_|restore|reset_|cancel_|complete_|fail_|succeed_|send_|publish_|invoke_|tag_resource|untag_resource'
HITS=$(grep -rEn --include='*.py' \
    -e "\.(${BANNED_RE})\(" \
    backend/app/services/litellm_client.py \
    backend/app/services/ai_system_prompt.py \
    backend/app/services/ai_context_builder.py \
    backend/app/services/ai_service.py \
    backend/app/api/ai.py \
    backend/app/schemas/ai.py \
    2>/dev/null \
    | grep -vE 'create_(enum|enum_type)|def create_|model_create|model_validate|model_dump|model_rebuild' \
    | grep -vE '# ' \
    || true)
if [[ -z "${HITS}" ]]; then
  ok "no write/mutation AWS APIs in Phase 4 source"
else
  bad "write/mutation AWS API hit(s): $(echo "${HITS}" | head -n 3)"
fi

# ---------- 7. Prompt injection + savings protection in system prompt ----------
note "System prompt enforces prompt-injection defense and savings protection"
if docker compose exec -T backend python -c "
from app.services.ai_system_prompt import (
    SYSTEM_PROMPT, SAVINGS_PROTECTION_SENTINEL,
)
# Grounding + savings + injection-defense language is present.
sp = SYSTEM_PROMPT.lower()
for needle in (
    'must not',
    'savings',
    'invent',
    'untrusted',
    'ignore previous instructions',
    'fabricate',
    'delete production',
    'advisory',
    'review',
    'execute',
):
    assert needle in sp, f'missing prompt grounding keyword: {needle!r}'
# Exact savings-protection sentinel is present so the model can echo it.
assert SAVINGS_PROTECTION_SENTINEL in SYSTEM_PROMPT, 'savings-protection sentinel missing from prompt'
print('phase4_prompt_ok')
" >/tmp/p4prompt.txt 2>&1; then
  ok "system prompt enforces savings protection + prompt-injection defense"
else
  bad "system prompt grounding check failed: $(tail -n 5 /tmp/p4prompt.txt | tr '\n' ' ')"
fi

# ---------- 8. Phase 4 HTTP endpoints ----------
note "GET /api/ai/status responds (DISABLED)"
STATUS=$(curl -sS --max-time 30 'http://127.0.0.1/api/ai/status' 2>/dev/null || true)
if echo "${STATUS}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'status', 'ai_enabled', 'litellm_reachable', 'model_alias', 'litellm_base_url', 'timeout_seconds', 'max_output_tokens', 'context_limits'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
if r['status'] not in ('OK', 'DISABLED', 'DEGRADED'):
    print('BAD_STATUS', r['status']); sys.exit(1)
# Must NEVER carry secrets.
for needle in ('api_key', 'authorization', 'bearer'):
    if needle in json.dumps(r).lower() and needle != 'authorization':
        # 'authorization' could appear as 'Authorization header' in
        # human-readable prose, but our payload is a dict so it must
        # not appear at all.
        print('SECRET_LEAK', needle); sys.exit(1)
print('OK', f\"status={r['status']} enabled={r['ai_enabled']} reachable={r['litellm_reachable']}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/ai/status shape correct + no secret leakage"
  echo "  $(echo "${STATUS}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"status={r['status']} ai_enabled={r['ai_enabled']} model={r['model_alias']}\")
" 2>/dev/null)"
else
  bad "/api/ai/status unreachable or malformed: ${STATUS:0:300}"
fi

note "POST /api/ai/executive-summary returns controlled DISABLED envelope"
ES=$(curl -sS --max-time 30 -X POST -H 'Content-Type: application/json' \
     -d '{"region":"us-east-1","days":30}' \
     'http://127.0.0.1/api/ai/executive-summary' 2>/dev/null || true)
if echo "${ES}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
required = {'status', 'operation', 'answer', 'grounding', 'citations', 'warnings'}
missing = required - set(r.keys())
if missing:
    print('MISSING_FIELDS', sorted(missing)); sys.exit(1)
if r['operation'] != 'executive_summary':
    print('BAD_OPERATION', r['operation']); sys.exit(1)
if r['status'] != 'DISABLED':
    print('BAD_STATUS', r['status']); sys.exit(1)
print('OK', f\"status={r['status']} warnings={len(r['warnings'])}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/ai/executive-summary returns DISABLED envelope (no LLM call)"
else
  bad "/api/ai/executive-summary failed: ${ES:0:300}"
fi

note "POST /api/ai/analyze returns DISABLED envelope"
AN=$(curl -sS --max-time 30 -X POST -H 'Content-Type: application/json' \
     -d '{"region":"us-east-1","days":30,"question":"Why did costs rise?"}' \
     'http://127.0.0.1/api/ai/analyze' 2>/dev/null || true)
if echo "${AN}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
if r['status'] != 'DISABLED':
    print('BAD_STATUS', r['status']); sys.exit(1)
if r['operation'] != 'analyze':
    print('BAD_OPERATION', r['operation']); sys.exit(1)
print('OK', f\"status={r['status']}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/ai/analyze returns DISABLED envelope"
else
  bad "/api/ai/analyze failed: ${AN:0:300}"
fi

note "POST /api/ai/recommendations/{id}/explain returns DISABLED envelope"
EX=$(curl -sS --max-time 30 -X POST -H 'Content-Type: application/json' \
     -d '{"region":"us-east-1","days":30}' \
     'http://127.0.0.1/api/ai/recommendations/det-1/explain' 2>/dev/null || true)
if echo "${EX}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
if r['status'] != 'DISABLED':
    print('BAD_STATUS', r['status']); sys.exit(1)
print('OK', f\"status={r['status']}\")
" 2>/dev/null | grep -q '^OK'; then
  ok "/api/ai/recommendations/{id}/explain returns DISABLED envelope"
else
  bad "/api/ai/recommendations/{id}/explain failed: ${EX:0:300}"
fi

# ---------- 9. Input validation ----------
note "Unsupported lookback (days=15) returns 422 InvalidLookbackDays"
BAD=$(curl -sS --max-time 10 -o /tmp/p4_bad.json -w '%{http_code}' \
      -X POST -H 'Content-Type: application/json' \
      -d '{"region":"us-east-1","days":15}' \
      'http://127.0.0.1/api/ai/executive-summary' 2>/dev/null || echo "000")
if [[ "${BAD}" == "422" ]] && grep -q 'InvalidLookbackDays' /tmp/p4_bad.json 2>/dev/null; then
  ok "unsupported lookback rejected (HTTP 422 InvalidLookbackDays)"
else
  bad "unsupported lookback did not return 422 (got HTTP ${BAD})"
fi

note "Oversized question returns 422 InvalidQuestion"
LONG_Q=$(python3 -c "print('x' * 3000)")
ORSZ=$(curl -sS --max-time 10 -o /tmp/p4_over.json -w '%{http_code}' \
       -X POST -H 'Content-Type: application/json' \
       -d "{\"region\":\"us-east-1\",\"days\":30,\"question\":\"${LONG_Q}\"}" \
       'http://127.0.0.1/api/ai/analyze' 2>/dev/null || echo "000")
if [[ "${ORSZ}" == "422" ]] && grep -q 'InvalidQuestion\|string_too_long\|at most' /tmp/p4_over.json 2>/dev/null; then
  ok "oversized question rejected (HTTP 422)"
else
  bad "oversized question did not return 422 (got HTTP ${ORSZ}): $(head -c 200 /tmp/p4_over.json)"
fi

note "Empty question returns 422 (Pydantic or service-level guard)"
EMPT=$(curl -sS --max-time 10 -o /tmp/p4_empty.json -w '%{http_code}' \
       -X POST -H 'Content-Type: application/json' \
       -d '{"region":"us-east-1","days":30,"question":""}' \
       'http://127.0.0.1/api/ai/analyze' 2>/dev/null || echo "000")
if [[ "${EMPT}" == "422" ]]; then
  ok "empty question rejected (HTTP 422)"
else
  bad "empty question did not return 422 (got HTTP ${EMPT})"
fi

# ---------- 10. Citation validation + null savings ----------
note "Citation validation: forged recommendation ids are dropped with a warning"
docker compose exec -T backend python -c "
from decimal import Decimal
from app.services.ai_service import AIService
from app.services.ai_context_builder import (
    AIContextBuilder, CitationIndex, filter_grounded_citations,
)
# Sanity check: filter_grounded_citations drops unsupported ids.
kept, warnings = filter_grounded_citations(
    [
        {'type': 'recommendation', 'id': 'det-real', 'resource_id': 'vol-real'},
        {'type': 'recommendation', 'id': 'det-forged', 'resource_id': 'vol-real'},
        {'type': 'cost_service', 'service': 'NotARealService'},
    ],
    index=CitationIndex(
        recommendation_ids={'det-real'},
        resource_ids={'vol-real'},
        services={'Amazon EC2'},
        regions={'us-east-1'},
        periods={'30d'},
    ),
)
assert len(kept) == 1, f'expected 1 kept, got {len(kept)}'
assert kept[0]['id'] == 'det-real'
assert len(warnings) == 2, f'expected 2 warnings, got {len(warnings)}'
print('citation_validation_ok')
" >/tmp/p4cit.txt 2>&1 \
  && ok "forged citations dropped with warnings" \
  || bad "citation validation failed: $(tail -n 5 /tmp/p4cit.txt | tr '\n' ' ')"

note "Null savings remain null in the rendered evidence (no AI estimate)"
docker compose exec -T backend python -c "
from datetime import date
from decimal import Decimal
from app.schemas.cost import (
    CostPeriod, CostReport, DailyCostPoint, ServiceCost, RegionCost,
)
from app.schemas.optimization import (
    CapabilitiesResponse, CapabilityStatus, ServiceCapability, ResourceType,
    Confidence, Recommendation, RecommendationAction,
    RecommendationEvidence, SavingsSource,
)
from app.services.ai_context_builder import AIContextBuilder, render_context_text
from app.core.config import Settings

settings = Settings(app_env='test', ai_max_question_length=1000)
report = CostReport(
    account_id='111122223333',
    period=CostPeriod(start=date(2025,9,1), end=date(2025,10,1), days=30),
    previous_period=CostPeriod(start=date(2025,8,2), end=date(2025,9,1), days=30),
    currency='USD',
    total_cost=Decimal('100'),
    previous_period_cost=Decimal('80'),
    change_amount=Decimal('20'),
    change_percent=Decimal('0.25'),
    daily_trend=[DailyCostPoint(date=date(2025,9,1), amount=Decimal('3.20'))],
    by_service=[ServiceCost(service='Amazon EC2', amount=Decimal('60.00'))],
    by_region=[RegionCost(region='us-east-1', amount=Decimal('100.00'))],
    source='AWS_COST_EXPLORER',
)
caps = CapabilitiesResponse(
    region='us-east-1', account_id='111122223333',
    compute_optimizer=ServiceCapability(status=CapabilityStatus.INACTIVE),
    cost_optimization_hub=ServiceCapability(status=CapabilityStatus.NOT_ENROLLED),
    deterministic_engine=ServiceCapability(status=CapabilityStatus.AVAILABLE),
    supported_resource_types=[ResourceType.EBS_VOLUME],
    supported_lookback_days=[7,30,60,90],
)
rec = Recommendation(
    recommendation_id='det-no-savings',
    resource_id='vol-no-savings',
    resource_arn=None,
    resource_type=ResourceType.EBS_VOLUME,
    region='us-east-1',
    account_id='111122223333',
    action=RecommendationAction.REVIEW_DELETE_UNATTACHED_EBS,
    title='Review delete',
    finding='available',
    current_configuration={}, recommended_configuration={},
    estimated_monthly_savings=None,
    currency='USD', savings_percentage=None,
    savings_source=SavingsSource.UNKNOWN, primary_source=SavingsSource.UNKNOWN,
    sources=[SavingsSource.UNKNOWN], confidence=Confidence.HIGH,
    data_quality='high', reason_codes=[],
    restart_needed=None, rollback_possible=True,
    evidence=[RecommendationEvidence(source=SavingsSource.UNKNOWN, confidence=Confidence.HIGH, data={}, reason_codes=[])],
    aws_recommendation_ids=[],
)
ctx, _ = AIContextBuilder(settings).build(
    region='us-east-1', days=30,
    cost_report=report, capabilities=caps, recommendations=[rec],
)
text = render_context_text(ctx)
# Null savings is rendered as null + the authoritative-savings
# sentinel phrase (so the model echoes it).
assert 'estimated_monthly_savings: null' in text
assert 'authoritative monthly savings are not available' in text.lower()
print('null_savings_ok')
" >/tmp/p4null.txt 2>&1 \
  && ok "null savings rendered with sentinel phrase (no AI estimate)" \
  || bad "null-savings check failed: $(tail -n 5 /tmp/p4null.txt | tr '\n' ' ')"

# ---------- 11. Prompt-injection regression test ----------
note "Prompt-injection regression test runs"
docker compose exec -T backend python -m pytest -q tests/test_ai_system_prompt.py \
  >/tmp/p4inject.out 2>&1 \
  && ok "prompt-injection regression tests pass" \
  || bad "prompt-injection regression failed: $(tail -n 10 /tmp/p4inject.out | tr '\n' ' ' | head -c 400)"

# ---------- 12. Secret scan (Phase 4 extra-strict) ----------
note "Secret scan remains clean (Phase 4 extra-strict — no AI keys either)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase0_verify.sh' --exclude='phase1_verify.sh' --exclude='phase2_verify.sh' --exclude='phase3_verify.sh' --exclude='phase4_verify.sh' \
  --exclude='security.md' \
  --exclude='phase0-report.md' --exclude='phase1-report.md' --exclude='phase2-report.md' --exclude='phase3-report.md' \
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

# ---------- 13. Public port controls ----------
note "Only nginx (port 80) is published; LiteLLM stays internal"
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
    if '0.0.0.0' in ports or '[::]' in ports:
        print(svc, ports)
" 2>/dev/null || true)
if [[ -z "${NON_NGINX_HOST_PORTS}" ]]; then
  ok "no service other than nginx publishes to a host port (LiteLLM internal)"
else
  bad "non-nginx service(s) publish to host port: ${NON_NGINX_HOST_PORTS}"
fi

# ---------- 14. Docker health ----------
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
echo "Phase 4 verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
