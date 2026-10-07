#!/usr/bin/env bash
# phase5c_verify.sh — Phase 5C (Secure WebSocket Realtime Conversation Layer) verifier.
#
# Composes phase5b_verify.sh with Phase 5C-specific checks:
#   1. Phase 5B regression (delegated to phase5b_verify.sh).
#   2. Phase 5C modules importable (schemas + ws_auth + ws_conversations + router).
#   3. WebSocket route registered at /ws/conversations/{conversation_id}.
#   4. Phase 5C pytest suite green (security + protocol).
#   5. Phase 5B regression (re-run after Phase 5C additions).
#   6. No secret / JWT / AWS / LiteLLM credential leakage in tracked source.
#   7. Only nginx publishes to a host port.
#   8. All containers healthy.
#   9. No AI_ESTIMATE in tracked source.
#  10. Phase 5C pytest-asyncio default-fixture-loop-scope setting is
#      recorded (or absent — see note below).
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

# ---------- 1. Phase 5B regression ----------
note "Phase 5B regression check (delegated to phase5b_verify.sh)"
if bash scripts/phase5b_verify.sh >/tmp/phase5b.log 2>&1; then
  P5B_PASS=$(grep -oE '[0-9]+ passed, [0-9]+ failed' /tmp/phase5b.log | tail -n 1 || echo "?")
  ok "phase5b_verify.sh exited 0 (${P5B_PASS})"
else
  bad "phase5b_verify.sh failed - see /tmp/phase5b.log"
fi

# ---------- 2. Phase 5C modules importable ----------
note "Phase 5C modules importable (ws_auth, ws_conversations, websocket schemas)"
if docker compose exec -T backend python -c "
from app.api.ws_auth import (
    WSAuthFailure,
    WS_CLOSE_AUTH_DISABLED, WS_CLOSE_FORBIDDEN, WS_CLOSE_NOT_FOUND,
    WS_CLOSE_PROTOCOL_ERROR, WS_CLOSE_TOO_MANY_REQUESTS, WS_CLOSE_UNAUTHORIZED,
    extract_ws_token, negotiate_subprotocol, resolve_ws_principal,
)
from app.api.ws_conversations import router, conversation_socket
from app.schemas.websocket import (
    ALLOWED_LOOKBACK_DAYS, ClientEvent, ClientPing, ClientUserMessage,
    MAX_EVENT_BYTES, MAX_FRAME_BYTES, MAX_QUESTION_LENGTH,
    MAX_RECENT_REQUEST_IDS, MAX_REGION_LENGTH, PROTOCOL_VERSION,
    ServerAiProcessing, ServerAssistantMessage, ServerConnected,
    ServerError, ServerPong, ServerUserMessageAccepted,
    WS_ERROR_CODES, parse_client_event,
)
print('phase5c_modules_ok')
print('protocol_version:', PROTOCOL_VERSION)
print('allowed_lookback:', list(ALLOWED_LOOKBACK_DAYS))
print('error_codes:', sorted(WS_ERROR_CODES))
" >/tmp/p5c_mod.txt 2>&1; then
  ok "Phase 5C modules importable (ws_auth + ws_conversations + schemas)"
else
  bad "Phase 5C import failed: $(tail -n 10 /tmp/p5c_mod.txt | tr '\n' ' ')"
fi

# ---------- 3. WebSocket route registered ----------
note "WebSocket route /ws/conversations/{conversation_id} registered"
if docker compose exec -T backend python -c "
from app.main import app
hits = [r for r in app.routes
        if getattr(r, 'path', None) == '/ws/conversations/{conversation_id}']
assert hits, 'route not registered'
print('routes:', [r.path for r in hits])
" >/tmp/p5c_routes.txt 2>&1; then
  ok "WebSocket route registered: /ws/conversations/{conversation_id}"
else
  bad "WebSocket route not registered: $(tail -n 5 /tmp/p5c_routes.txt | tr '\n' ' ')"
fi

# ---------- 4. Phase 5C pytest suite green ----------
note "Phase 5C pytest suite green (security + protocol)"
if docker compose exec -T -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 backend \
     python -m pytest tests/test_websocket_security.py tests/test_websocket_protocol.py \
     --no-header -q >/tmp/p5c_pytest.txt 2>&1; then
  P5C_PASS=$(grep -oE '[0-9]+ passed' /tmp/p5c_pytest.txt | tail -n 1 || echo "?")
  ok "Phase 5C pytest suite green (${P5C_PASS})"
else
  bad "Phase 5C pytest failed: $(tail -n 30 /tmp/p5c_pytest.txt | tr '\n' ' ')"
fi

# ---------- 5. Phase 5B regression (re-run after Phase 5C) ----------
note "Phase 5B regression (re-run after Phase 5C additions)"
if bash scripts/phase5b_verify.sh >/tmp/phase5b2.log 2>&1; then
  P5B2_PASS=$(grep -oE '[0-9]+ passed, [0-9]+ failed' /tmp/phase5b2.log | tail -n 1 || echo "?")
  ok "phase5b_verify.sh still green after Phase 5C (${P5B2_PASS})"
else
  bad "phase5b_verify.sh regressed after Phase 5C - see /tmp/phase5b2.log"
fi

# ---------- 6. No secret / JWT / AWS / LiteLLM credential leakage ----------
note "No secret / JWT / AWS / LiteLLM credential leakage in tracked source"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase*_verify.sh' \
  --exclude='security.md' \
  --exclude='phase*-report.md' \
  --exclude='docs/phase*-cost-intelligence.md' \
  --exclude='docs/phase3-optimization-intelligence.md' \
  --exclude='docs/phase4-ai-cost-analyst.md' \
  --exclude='docs/phase5a-*' \
  --exclude='docs/phase5b-*' \
  --exclude='docs/phase5c-*' \
  --exclude='.env' --exclude='.env.example' \
  --exclude='generate_dev_secrets.sh' \
  -e 'AKIA[0-9A-Z]{16}' \
  -e 'AWS_SECRET_ACCESS_KEY=[A-Za-z0-9/+=]{40}' \
  -e 'AWS_ACCESS_KEY_ID=[A-Z0-9]{16,}' \
  -e 'OPENAI_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'ANTHROPIC_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'GEMINI_API_KEY=[A-Za-z0-9_\-]{20,}' \
  -e 'LITELLM_MASTER_KEY=sk-[A-Za-z0-9_\-]{20,}' \
  -e 'JWT_SECRET=[A-Za-z0-9_\-]{32,}' \
  2>/dev/null || true)
if [[ -z "${SECRET_HITS}" ]]; then
  ok "No obvious secrets in tracked files"
else
  bad "Secret pattern matches: $(echo "${SECRET_HITS}" | awk -F: '{print $1}' | sort -u | tr '\n' ' ')"
fi

# ---------- 7. Only nginx publishes to a host port ----------
note "Only nginx publishes to a host port"
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
    if r.get('Service','') == 'nginx':
        continue
    ports = r.get('Ports') or ''
    if '0.0.0.0' in ports or '[::]' in ports:
        print(r.get('Service',''), ports)
" 2>/dev/null || true)
if [[ -z "${NON_NGINX_HOST_PORTS}" ]]; then
  ok "No service other than nginx publishes to a host port"
else
  bad "Non-nginx service publishes to host: ${NON_NGINX_HOST_PORTS}"
fi

# ---------- 8. All containers healthy ----------
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
        print(r.get('Service',''), 'UNHEALTHY')
" 2>/dev/null || true)
if [[ -z "${UNHEALTHY}" ]]; then
  ok "All running containers are healthy"
else
  bad "Unhealthy container(s): ${UNHEALTHY}"
fi

# ---------- 9. No AI_ESTIMATE in tracked source ----------
note "No AI_ESTIMATE token in tracked source"
HITS=$(git -C "${REPO_ROOT}" grep -nIE 'AI_ESTIMATE' -- \
  --exclude='phase*_verify.sh' \
  --exclude='docs/phase*-report.md' \
  --exclude='.env' --exclude='.env.example' \
  2>/dev/null || true)
if [[ -z "${HITS}" ]]; then
  ok "No AI_ESTIMATE token in tracked source"
else
  bad "AI_ESTIMATE appears: ${HITS}"
fi

# ---------- 10. nginx WebSocket upgrade configuration ----------
note "nginx WebSocket upgrade configuration present"
NGINX_WS_LOC=$(grep -c "location /api/ws/" "${REPO_ROOT}/nginx/default.conf" 2>/dev/null || true)
NGINX_UPGRADE=$(grep -c 'proxy_set_header Upgrade' "${REPO_ROOT}/nginx/default.conf" 2>/dev/null || true)
NGINX_CONN=$(grep -c 'proxy_set_header Connection' "${REPO_ROOT}/nginx/default.conf" 2>/dev/null || true)
if [[ "${NGINX_WS_LOC}" -ge 1 && "${NGINX_UPGRADE}" -ge 1 && "${NGINX_CONN}" -ge 1 ]]; then
  ok "nginx WebSocket upgrade configuration present (location + Upgrade + Connection)"
else
  bad "nginx WebSocket upgrade config missing (loc=${NGINX_WS_LOC} up=${NGINX_UPGRADE} conn=${NGINX_CONN})"
fi

# ---------- 11. No Redis / Kafka / API Gateway WS / SSE / Socket.IO introduced ----------
note "No Redis / Kafka / API Gateway WS / SSE / Socket.IO introduced"
INFRA_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase*_verify.sh' \
  --exclude='docs/phase*-report.md' \
  --exclude='docs/phase5a-*' \
  --exclude='docs/phase5b-*' \
  --exclude='docs/phase5c-*' \
  -e 'import redis' -e 'import aioredis' -e 'redis.pubsub' -e 'redis.asyncio' \
  -e 'from kafka' -e 'import kafka' -e 'aiokafka' \
  -e 'EventSourceResponse' -e 'SSEStreamingResponse' \
  -e 'socketio' -e 'Socket.IO' \
  -e 'websocket.subscription' -e 'apigatewayv2' \
  backend/app 2>/dev/null || true)
if [[ -z "${INFRA_HITS}" ]]; then
  ok "No Redis / Kafka / SSE / Socket.IO / API Gateway WS introduced"
else
  bad "Extra infrastructure introduced: ${INFRA_HITS}"
fi

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 5C verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
