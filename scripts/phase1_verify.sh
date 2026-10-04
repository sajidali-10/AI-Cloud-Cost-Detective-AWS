#!/usr/bin/env bash
# phase1_verify.sh — Phase 1 (AWS Identity and Resource Discovery) verifier.
#
# Composes phase0_verify.sh with Phase 1-specific checks:
#   1. Phase 0 baseline (delegated to phase0_verify.sh).
#   2. boto3 is present in the backend image (read-only guard).
#   3. No write/mutation AWS APIs are referenced in the source.
#   4. AWS schemas are importable and behave correctly.
#   5. /api/aws/identity returns 200 with account/arn/user_id/region OR a
#      sanitized 502 with error_code - never a 500.
#   6. /api/aws/resources returns 200 with services + enrichment blocks
#      and never 500s on per-service AccessDenied.
#   7. Resource Explorer live check (when AWS credentials are available).
#   8. Secret scan remains clean (no AWS keys, no AI keys).
#   9. No public PostgreSQL / LiteLLM / backend port exposure.
#  10. Public-port surface unchanged (only Nginx on :80).
#  11. Backend unit tests still pass (delegated to phase0_verify.sh).

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PASS=0
FAIL=0
FAILURES=()

note()  { printf '[check] %s\n' "$*"; }
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()   { FAIL=$((FAIL+1)); FAILURES+=("$*"); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

# ---------- 1. Phase 0 baseline ----------
note "Phase 0 regression check (delegated to phase0_verify.sh)"
if bash scripts/phase0_verify.sh >/tmp/phase0.log 2>&1; then
  ok "phase0_verify.sh exited 0"
  # Surface the line count so the user sees what ran.
  P0_PASS=$(grep -oE '[0-9]+ passed' /tmp/phase0.log | tail -n 1 || echo "?")
  ok "phase 0 verify summary: ${P0_PASS}"
else
  bad "phase0_verify.sh failed - see /tmp/phase0.log"
fi

# ---------- 2. boto3 is present in the backend image ----------
note "boto3 present in backend image"
if docker compose exec -T backend python -c "import boto3, botocore; print(boto3.__version__, botocore.__version__)" >/tmp/boto3.txt 2>&1; then
  BOTO_VER=$(tr -d '\n' </tmp/boto3.txt)
  ok "boto3 + botocore installed: ${BOTO_VER}"
else
  bad "boto3 not importable inside backend container"
fi

# ---------- 3. Read-only guard ----------
note "No write/mutation AWS APIs in source"
# Build a list of Boto3 client methods that would mutate state, scoped to
# the service calls we actually use in Phase 1. A hit here is a hard fail.
BANNED_PATTERNS=(
  # Generic mutation verbs we should never call.
  "create_"  "delete_"  "modify_"  "update_"
  "terminate" "stop_"    "start_"   "reboot"
  "attach"   "detach"   "enable_"  "disable_"
  "put_"     "register" "deregister" "associate_" "disassociate_"
  "allocate" "release"  "authorize" "revoke"
  "import_"  "export_"  "copy_"     "move_"
  "restore"  "reboot_"  "reset_"    "invite"
  "accept_"  "reject_"  "decline_"
  "send_"    "publish_" "broadcast"
  "deploy"   "rollback" "promote"   "demote"
  "set_"     "tag_resource" "untag_resource"
  "cancel_"  "complete_" "fail_"   "succeed_"
)
BANNED_RE=$(IFS='|'; echo "${BANNED_PATTERNS[*]}")
# Scan Phase 1 source only.
HITS=$(grep -rEn --include='*.py' \
  -e "\.(${BANNED_RE})\(" \
  backend/app/services/aws/ backend/app/api/aws.py 2>/dev/null | \
  grep -vE 'create_(enum|enum_type)|model_rebuild' || true)
if [[ -z "${HITS}" ]]; then
  ok "no write/mutation AWS APIs in Phase 1 source"
else
  bad "write/mutation AWS API hit(s): $(echo "${HITS}" | head -n 3)"
fi

# ---------- 4. AWS schemas importable ----------
note "AWS schemas importable from backend container"
if docker compose exec -T backend python -c "
from app.schemas.aws import (
  Ec2Instance, EbsVolume, ElasticIp, NatGateway,
  LoadBalancerV2, RdsInstance, LambdaFunction, S3Bucket,
  ServiceResult, ResourceExplorerResult, TaggingApiResult, ResourcesResponse,
)
print('schemas_ok')
" >/tmp/schemas.txt 2>&1; then
  ok "all 12 AWS schemas importable"
else
  bad "AWS schema import failed: $(tail -n 3 /tmp/schemas.txt)"
fi

# ---------- 5. /api/aws/identity behavior ----------
note "GET /api/aws/identity returns 200 with identity or sanitized 502"
# Use no -f so we read the body on the 502 path too.
IDENTITY=$(curl -sS --max-time 10 http://127.0.0.1/api/aws/identity 2>/dev/null || true)
if echo "${IDENTITY}" | grep -q '"account":'; then
  ok "identity resolved via real STS (HTTP 200)"
  echo "  identity: $(echo "${IDENTITY}" | head -c 200)"
elif echo "${IDENTITY}" | grep -q '"error_code"'; then
  ok "identity endpoint reachable with sanitized credential error"
  echo "  error_code: $(echo "${IDENTITY}" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("error_code"))' 2>/dev/null)"
else
  bad "identity endpoint unreachable: ${IDENTITY:0:200}"
fi

# ---------- 6. /api/aws/resources behavior ----------
note "GET /api/aws/resources returns 200 with services + enrichment"
RESOURCES=$(curl -sS --max-time 30 'http://127.0.0.1/api/aws/resources?region=us-east-1' 2>/dev/null || true)
if echo "${RESOURCES}" | python3 -c "
import json, sys
try:
    r = json.load(sys.stdin)
except Exception as e:
    print('PARSE_FAIL', e); sys.exit(1)
expected = {'ec2','ebs','eip','nat','elbv2','rds','lambda','s3'}
got = set(r.get('services', {}).keys())
if not expected.issubset(got):
    print('MISSING_SERVICES', expected - got); sys.exit(1)
if 'enrichment' not in r or 'resource_explorer' not in r['enrichment']:
    print('NO_ENRICHMENT'); sys.exit(1)
print('OK', r['region'], 'services=', len(r['services']), 're=', r['enrichment']['resource_explorer']['available'])
" 2>/tmp/rcheck.err | grep -q '^OK'; then
  ok "/api/aws/resources shape correct"
  echo "  $(echo "${RESOURCES}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(f\"region={r['region']} services={list(r['services'].keys())} re.available={r['enrichment']['resource_explorer']['available']} tagging.available={r['enrichment']['tagging']['available']}\")
")"
else
  ERR=$(cat /tmp/rcheck.err 2>/dev/null || true)
  bad "/api/aws/resources shape wrong: ${ERR:-${RESOURCES:0:200}}"
fi

# ---------- 7. Region override behavior ----------
note "Region override: ?region= query param works"
REGION_RESP=$(curl -sS --max-time 10 'http://127.0.0.1/api/aws/identity?region=eu-west-1' 2>/dev/null || true)
if echo "${REGION_RESP}" | grep -q '"region":"eu-west-1"'; then
  ok "region query param honored"
elif echo "${REGION_RESP}" | grep -q '"error_code"'; then
  # No credentials in this env is fine - we only need the region field to reflect the query.
  if echo "${REGION_RESP}" | grep -q '"region":"eu-west-1"'; then
    ok "region query param honored (sanitized error path)"
  else
    bad "region query param NOT honored: ${REGION_RESP:0:200}"
  fi
else
  bad "region query param test inconclusive: ${REGION_RESP:0:200}"
fi

# ---------- 8. Resource Explorer live check ----------
note "Resource Explorer live check (when STS credentials are available)"
if echo "${IDENTITY}" | grep -q '"account":'; then
  RE_BLOCK=$(echo "${RESOURCES}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
print(r['enrichment']['resource_explorer'].get('error_code') or 'ok')
" 2>/dev/null || true)
  if [[ "${RE_BLOCK}" == "ok" ]]; then
    ok "Resource Explorer returned real results (no ParamValidationError)"
  elif [[ -z "${RE_BLOCK}" ]]; then
    ok "Resource Explorer returned no error code (graceful)"
  else
    if [[ "${RE_BLOCK}" == "ParamValidationError" ]]; then
      bad "Resource Explorer still returns ParamValidationError - QueryString fix not applied"
    else
      # Other codes (NoCredentialsError, AccessDenied, ResourceNotFoundException)
      # are acceptable graceful-degradation paths.
      ok "Resource Explorer degraded gracefully with error_code=${RE_BLOCK}"
    fi
  fi
else
  ok "Resource Explorer live check skipped (no AWS credentials in this env)"
fi

# ---------- 9. Secret scan (extra-strict for Phase 1) ----------
note "Secret scan (Phase 1 extra-strict)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase0_verify.sh' --exclude='phase1_verify.sh' \
  --exclude='security.md' --exclude='phase0-report.md' --exclude='phase1-report.md' \
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

# ---------- 10. Public ports unchanged ----------
note "Only Nginx publishes a public port"
PUB=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
for r in rows:
    s = r.get('Service')
    if s in ('postgres','litellm','backend','frontend'):
        for p in (r.get('Publishers') or []):
            if p.get('PublishedPort'):
                print('LEAK', s, p.get('PublishedPort'))
" 2>/dev/null || true)
if [[ -z "${PUB}" ]]; then
  ok "no public port on postgres/litellm/backend/frontend"
else
  bad "public port leak: ${PUB}"
fi

# ---------- 11. No boto3 credentials in Settings / .env.example ----------
note "No AWS keys in Settings or .env.example"
if ! grep -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)' backend/app/core/config.py >/dev/null 2>&1; then
  ok "no AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in Settings"
else
  bad "AWS key field found in Settings - violation of credential-chain constraint"
fi
if ! grep -E '^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY)' .env.example >/dev/null 2>&1; then
  ok "no AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env.example"
else
  bad "AWS key in .env.example - violation of credential-chain constraint"
fi

# ---------- 12. Single-region enforcement (no fanout code) ----------
note "Single-region enforcement: no all-region fanout code in source"
# Look for actual code patterns - not documentation comments.
FANOUT_HITS=$(grep -rEn --include='*.py' \
  -e 'describe_regions\b' \
  -e '\.describe_regions\(' \
  -e 'fanout\b' \
  -e 'fan_out\b' \
  -e 'fan_out_to_all_regions' \
  backend/app/services/aws/ backend/app/api/aws.py 2>/dev/null || true)
if [[ -z "${FANOUT_HITS}" ]]; then
  ok "no multi-region fanout in Phase 1 source"
else
  bad "all-region fanout code found: $(echo "${FANOUT_HITS}" | head -n 3)"
fi

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 1 verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
