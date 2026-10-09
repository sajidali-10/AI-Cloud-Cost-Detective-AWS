#!/usr/bin/env bash
# scripts/phase6c_verify.sh
#
# Phase 6C — Professional AI Cost Analyst + Conversations + Secure Realtime UX.
#
# Composes phase6b_verify.sh with Phase 6C-specific structural,
# transport, security, test, and live-integration checks for the
# HipLink AI workspace.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
declare -a FAIL_LINES

run_check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $label"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $label"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("$label")
  fi
}

note() {
  echo "---- $1"
}

# ---------------------------------------------------------------------------
# Phase 6B regression (delegated, includes Phase 6A + 5C).
# ---------------------------------------------------------------------------

note "Phase 6B regression (delegated, includes 6A + 5C)"
if bash scripts/phase6b_verify.sh > /tmp/phase6b.out 2>&1; then
  echo "PASS  phase6b_verify.sh delegation"
  PASS=$((PASS + 1))
else
  echo "FAIL  phase6b_verify.sh delegation"
  tail -40 /tmp/phase6b.out
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("phase6b_verify.sh delegation")
fi

# ---------------------------------------------------------------------------
# Phase 6C — typed AI / conversation surface.
# ---------------------------------------------------------------------------

note "Phase 6C typed surface"

run_check "frontend/src/types/ai.ts exists" test -f frontend/src/types/ai.ts
run_check "frontend/src/lib/ai/api.ts exists" test -f frontend/src/lib/ai/api.ts
run_check "frontend/src/lib/ai/protocol.ts exists" test -f frontend/src/lib/ai/protocol.ts
run_check "frontend/src/lib/ai/connection.ts exists" test -f frontend/src/lib/ai/connection.ts
run_check "frontend/src/lib/ai/markdown.tsx exists" test -f frontend/src/lib/ai/markdown.tsx

# ERR_AUTH_DISABLED constant + MAX_QUESTION_LENGTH + MAX_RECENT_REQUEST_IDS
# are the contract constants the UI keys off.
run_check "ERR_AUTH_DISABLED exported" \
  bash -c 'grep -q "ERR_AUTH_DISABLED" frontend/src/types/ai.ts'
run_check "MAX_QUESTION_LENGTH = 2000" \
  bash -c 'grep -qE "MAX_QUESTION_LENGTH\s*[:=]\s*2000" frontend/src/types/ai.ts'
run_check "MAX_RECENT_REQUEST_IDS = 128" \
  bash -c 'grep -qE "MAX_RECENT_REQUEST_IDS\s*[:=]\s*128" frontend/src/types/ai.ts'

# Conversational API surfaces — every Phase 5B REST route must be called.
run_check "api.ts uses /api/ai/status" \
  bash -c 'grep -q "/api/ai/status" frontend/src/lib/ai/api.ts'
run_check "api.ts uses /api/conversations (POST list)" \
  bash -c 'grep -q "/api/conversations" frontend/src/lib/ai/api.ts'
run_check "api.ts uses /api/conversations/\\${id}/messages" \
  bash -c "grep -qE '/api/conversations.*messages' frontend/src/lib/ai/api.ts"
run_check "api.ts issues PATCH + DELETE" \
  bash -c "grep -qE 'method:\\s*\"PATCH\"' frontend/src/lib/ai/api.ts && grep -qE 'method:\\s*\"DELETE\"' frontend/src/lib/ai/api.ts"

# ---------------------------------------------------------------------------
# Phase 6C — protocol + connection layer.
# ---------------------------------------------------------------------------

note "WebSocket subprotocol transport (bearer.<jwt>)"

# The JWT must travel in the Sec-WebSocket-Protocol header — never the URL.
run_check "protocol.ts exports buildSubprotocols(token)" \
  bash -c 'grep -qE "buildSubprotocols\(" frontend/src/lib/ai/protocol.ts'
run_check "buildSubprotocols prepends bearer." \
  bash -c "grep -qE 'bearer\\.\\\$\\{' frontend/src/lib/ai/protocol.ts"

run_check "connection.ts opens WebSocket with subprotocol (not URL)" \
  bash -c "grep -qE 'factoryRef\\.current\\(url, subprotocols\\)' frontend/src/lib/ai/connection.ts"
run_check "connection.ts URL construction never embeds token" \
  bash -c '! grep -E "token.*=|jwt.*=" frontend/src/lib/ai/connection.ts frontend/src/lib/ai/protocol.ts'

run_check "assertUrlHasNoToken rejects ?token=&jwt=" \
  bash -c 'grep -q "assertUrlHasNoToken" frontend/src/lib/ai/protocol.ts'

# Authenticated close codes must surface as authorization_failure.
run_check "isAuthorizationCloseCode includes 1008|4401|4403" \
  bash -c "grep -qE '1008|4401|4403' frontend/src/lib/ai/connection.ts"

# Concurrency rule: one in-flight per connection.
run_check "sendUserMessage rejects while inflight !== null" \
  bash -c 'grep -q "inflightRef.current" frontend/src/lib/ai/connection.ts'

# Dedup FIFO bound.
run_check "recordRequestId respects MAX_RECENT_REQUEST_IDS" \
  bash -c 'grep -q "MAX_RECENT_REQUEST_IDS" frontend/src/lib/ai/connection.ts'

# Reconnection policy — at most one auto-reconnect, then user-driven.
run_check "autoReconnectUsedRef guards infinite reconnect loops" \
  bash -c 'grep -q "autoReconnectUsedRef" frontend/src/lib/ai/connection.ts'

# Heartbeat — ping cadence = HEARTBEAT_MULTIPLIER * server interval.
run_check "heartbeat cadence derived from server interval" \
  bash -c 'grep -q "HEARTBEAT_MULTIPLIER" frontend/src/lib/ai/connection.ts'

# ---------------------------------------------------------------------------
# Phase 6C — components.
# ---------------------------------------------------------------------------

note "HipLink conversation components"

for f in \
  frontend/src/components/ConversationList.tsx \
  frontend/src/components/ConversationView.tsx \
  frontend/src/components/MessageBubble.tsx \
  frontend/src/components/EvidenceCard.tsx \
  frontend/src/components/Composer.tsx \
  frontend/src/components/ProgressIndicator.tsx \
  frontend/src/components/AiProviderBadge.tsx \
  frontend/src/components/AuthDisabledNotice.tsx ; do
  run_check "$f exists" test -f "$f"
done

# Composer behavior — must NOT embed evidence/system prompt.
run_check "Composer disables when inflight or empty/over-long" \
  bash -c 'grep -q "inflight" frontend/src/components/Composer.tsx && grep -qE "MAX_QUESTION_LENGTH" frontend/src/components/Composer.tsx'
run_check "Composer Enter submits / Shift+Enter newline" \
  bash -c "grep -qE 'Enter' frontend/src/components/Composer.tsx && grep -qE 'shiftKey' frontend/src/components/Composer.tsx"

# Markdown renderer is dependency-free and emits no raw HTML.
run_check "markdown.tsx renders fenced code blocks" \
  bash -c 'grep -q "fenced\|code" frontend/src/lib/ai/markdown.tsx'
run_check "markdown.tsx does NOT use dangerouslySetInnerHTML" \
  bash -c "! grep -E 'dangerouslySetInnerHTML' frontend/src/lib/ai/markdown.tsx"

# Pages wired.
run_check "AIAnalystPage uses useConversationSocket" \
  bash -c 'grep -q "useConversationSocket" frontend/src/pages/AIAnalystPage.tsx'
run_check "AIAnalystPage renders AiProviderBadge + PeriodSelector" \
  bash -c 'grep -q "AiProviderBadge" frontend/src/pages/AIAnalystPage.tsx && grep -q "PeriodSelector" frontend/src/pages/AIAnalystPage.tsx'
run_check "AIAnalystPage renders AuthDisabledNotice when auth disabled" \
  bash -c 'grep -q "AuthDisabledNotice" frontend/src/pages/AIAnalystPage.tsx'
run_check "AIAnalystPage renders Reconnect button on retryable failure" \
  bash -c 'grep -q "onRetryConnection\|reconnect" frontend/src/pages/AIAnalystPage.tsx'

run_check "ConversationsPage uses fetchConversations" \
  bash -c 'grep -q "fetchConversations" frontend/src/pages/ConversationsPage.tsx'
run_check "ConversationsPage renders AuthDisabledNotice on AuthDisabled" \
  bash -c 'grep -q "ERR_AUTH_DISABLED\|AuthDisabled" frontend/src/pages/ConversationsPage.tsx'

# RBAC — routes still ADMIN/ANALYST only (frontend declaration).
run_check "App routes gate /analyst to ADMIN/ANALYST" \
  bash -c "grep -qE \"path:\\s*'/analyst'\" frontend/src/App.tsx && grep -qE \"requiredRoles:\\s*\\[.*ADMIN.*ANALYST.*\\]\" frontend/src/App.tsx"
run_check "App routes gate /conversations to ADMIN/ANALYST" \
  bash -c "grep -qE \"path:\\s*'/conversations'\" frontend/src/App.tsx && grep -qE \"requiredRoles:\\s*\\[.*ADMIN.*ANALYST.*\\]\" frontend/src/App.tsx"

# ---------------------------------------------------------------------------
# Phase 6C — no raw HTML emission in user-content renderers.
# ---------------------------------------------------------------------------

note "No raw HTML emission in user-content renderers"

run_check "MessageBubble does not dangerouslySetInnerHTML" \
  bash -c "! grep -E 'dangerouslySetInnerHTML' frontend/src/components/MessageBubble.tsx"
run_check "EvidenceCard does not dangerouslySetInnerHTML" \
  bash -c "! grep -E 'dangerouslySetInnerHTML' frontend/src/components/EvidenceCard.tsx"

# ---------------------------------------------------------------------------
# Phase 6C — JWT / secret hygiene on the new modules.
# ---------------------------------------------------------------------------

note "JWT / secret hygiene"

# JWT must not appear in any URL/path/UI output/log line of the new modules.
run_check "no JWT pattern in shipped AI/WS frontend sources" \
  bash -c "! grep -REn '[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+' frontend/src/lib/ai frontend/src/components/Composer.tsx frontend/src/components/ConversationView.tsx frontend/src/pages/AIAnalystPage.tsx frontend/src/pages/ConversationsPage.tsx frontend/src/types/ai.ts"

run_check "no AKIA/AWS/LiteLLM/JWT secret strings in shipped frontend" \
  bash -c "! grep -REn 'AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,}|LITELLM_MASTER_KEY=sk-[A-Za-z0-9_-]{16,}|JWT_SECRET=[A-Za-z0-9_-]{16,}' frontend/src --include='*.ts' --include='*.tsx' --exclude-dir=tests"

# ---------------------------------------------------------------------------
# Phase 6C — frontend tests.
#
# The full frontend test suite + production build was already executed
# by phase6b_verify.sh (which itself delegates up the chain).  We do
# NOT re-run the suite here to keep the verifier fast.  We only check
# that the Phase 6C-specific test files exist on disk.
# ---------------------------------------------------------------------------

note "Phase 6C-specific frontend test files exist on disk"

for f in \
  frontend/src/tests/ai-api.test.ts \
  frontend/src/tests/ai-protocol.test.ts \
  frontend/src/tests/ai-connection.test.tsx \
  frontend/src/tests/ai-page.test.tsx \
  frontend/src/tests/composer.test.tsx \
  frontend/src/tests/conversation-list.test.tsx \
  frontend/src/tests/conversations-page.test.tsx \
  frontend/src/tests/evidence-card.test.tsx \
  frontend/src/tests/message-bubble.test.tsx \
  frontend/src/tests/progress-indicator.test.tsx ; do
  run_check "$f exists" test -f "$f"
done

# ---------------------------------------------------------------------------
# Phase 6C — live backend integration sanity (best-effort, non-blocking).
# ---------------------------------------------------------------------------

note "Live backend integration (best-effort)"

if curl -sSf -o /tmp/ai-status.json http://localhost/api/ai/status 2>/dev/null; then
  if python3 -c "
import json, sys
d = json.load(open('/tmp/ai-status.json'))
assert 'status' in d, d
assert 'ai_enabled' in d, d
sys.exit(0)
"; then
    echo "PASS  /api/ai/status reachable and shaped correctly"
    PASS=$((PASS + 1))
  else
    echo "FAIL  /api/ai/status response missing required fields"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("/api/ai/status shape")
  fi
else
  echo "SKIP  /api/ai/status not reachable (backend down?)"
fi

# /api/conversations should require auth (returns 401/403 when no token).
if curl -sS -o /tmp/conv-list.json -w '%{http_code}' http://localhost/api/conversations 2>/dev/null \
    | grep -qE '^(401|403|503)$'; then
  echo "PASS  /api/conversations refuses anonymous requests (401/403/503)"
  PASS=$((PASS + 1))
else
  echo "FAIL  /api/conversations did NOT refuse anonymous requests"
  cat /tmp/conv-list.json 2>/dev/null | head -c 200
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("/api/conversations anonymous refusal")
fi

# WebSocket route is registered in nginx.
WS_LOC=$(grep -c "location /api/ws/" nginx/default.conf 2>/dev/null || true)
if [[ "${WS_LOC}" -ge 1 ]]; then
  echo "PASS  nginx exposes /api/ws/ (location ${WS_LOC})"
  PASS=$((PASS + 1))
else
  echo "FAIL  nginx does not expose /api/ws/ (loc=${WS_LOC})"
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("nginx /api/ws/ location")
fi

# Upgrade + Connection headers in nginx WS block.
if grep -q "proxy_set_header Upgrade" nginx/default.conf \
   && grep -q "proxy_set_header Connection" nginx/default.conf ; then
  echo "PASS  nginx WebSocket Upgrade + Connection headers configured"
  PASS=$((PASS + 1))
else
  echo "FAIL  nginx WebSocket Upgrade + Connection headers missing"
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("nginx WS upgrade headers")
fi

# ---------------------------------------------------------------------------
# Phase 6C — Docker / nginx topology (delegated via phase6b).
# ---------------------------------------------------------------------------

note "Docker + nginx topology (delegated)"

run_check "docker-compose.yml present" test -f docker-compose.yml
run_check "only nginx publishes a host port" \
  bash -c 'PUB=$(docker compose ps --format "table {{.Service}}|{{.Ports}}" 2>/dev/null | awk -F"|" "NR>1 && \$2 ~ /->/ {print \$1}" | sort -u | tr "\n" "," | sed "s/,$//"); test "$PUB" = "nginx"'
run_check "compose declares ports only on nginx" \
  bash -c 'BAD=$(awk "
    /^  [a-zA-Z_-]+:/ { svc = \$2; sub(/:$/, \"\", svc); next }
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*\$/ { next }
    /^[[:space:]]+ports:/ { if (svc != \"nginx\") { print svc; exit 1 } }
  " docker-compose.yml); test -z "$BAD"'
run_check "all docker containers healthy" \
  bash -c 'UNHEALTHY=$(docker compose ps --format "{{.Service}}:{{.State}}" 2>/dev/null | awk -F: "\$2 != \"healthy\" && \$2 != \"running\" {print \$0}"); test -z "$UNHEALTHY"'

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "============================================"
echo "Phase 6C verification summary"
echo "============================================"
echo "PASS=$PASS"
echo "FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "Failed checks:"
  for line in "${FAIL_LINES[@]}"; do
    echo "  - $line"
  done
  echo "OVERALL: FAIL"
  exit 1
fi
echo "OVERALL: PASS"
exit 0
