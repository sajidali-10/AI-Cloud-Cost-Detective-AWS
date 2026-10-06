#!/usr/bin/env bash
# phase5b_verify.sh — Phase 5B (Conversation & AI History Persistence) verifier.
#
# Composes phase5a_verify.sh with Phase 5B-specific checks:
#   1. Phase 5A regression (delegated to phase5a_verify.sh).
#   2. New modules importable.
#   3. Alembic migration 0003 applied (idempotent re-run OK).
#   4. Alembic 0003 reversible (downgrade -> upgrade round-trip).
#   5. Schema introspection: conversations + messages tables,
#      FKs, indexes, CHECK constraints.
#   6. CASCADE behaviour: deleting a conversation removes its messages.
#   7. Hermetic end-to-end IDOR / ownership / RBAC / AI conversation
#      / prompt-injection / null-savings / auth-disabled script.
#   8. Phase 5A regression (re-run after migration changes).
#   9. No AWS mutation APIs (delegated to phase5a_verify.sh).
#  10. No AI_ESTIMATE anywhere in tracked files.
#  11. No WebSocket / streaming / SSE / Redis pub/sub introduced.
#  12. Secret scan (extended: no JWT_SECRET values either).
#  13. Only nginx publishes to a host port.
#  14. All containers healthy.
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

# ---------- 1. Phase 5A regression ----------
note "Phase 5A regression check (delegated to phase5a_verify.sh)"
if bash scripts/phase5a_verify.sh >/tmp/phase5a.log 2>&1; then
  P5A_PASS=$(grep -oE '[0-9]+ passed, [0-9]+ failed' /tmp/phase5a.log | tail -n 1 || echo "?")
  ok "phase5a_verify.sh exited 0 (${P5A_PASS})"
else
  bad "phase5a_verify.sh failed - see /tmp/phase5a.log"
fi

# Ensure migration is at head before any hermetic checks.
docker compose exec -T backend alembic upgrade head >/dev/null 2>&1 || true

# ---------- 2. Phase 5B modules importable ----------
note "Phase 5B modules importable"
if docker compose exec -T backend python -c "
from app.db.models import (
    Conversation, ConversationMessage,
    CONVERSATION_MESSAGE_ROLES, DEFAULT_CONVERSATION_TITLE,
)
from app.services.conversation_service import (
    ConversationService, ConversationError, ConversationNotFound,
    HistoryTurn, render_history_block,
    InvalidConversationTitle, InvalidMessageContent, InvalidMessageRole,
    InvalidPagination, get_conversation_service,
)
from app.schemas.conversation import (
    ConversationView, ConversationCreateRequest, ConversationPatchRequest,
    ConversationDetailResponse, ConversationListResponse,
    ConversationMessageView, ConversationMessagesResponse,
    ConversationAnalyzeRequest, MessageRole,
)
from app.api.conversations import (
    router, create_conversation, list_conversations,
    get_conversation, patch_conversation, delete_conversation,
    list_conversation_messages, analyze_conversation,
)
from app.services.ai_system_prompt import (
    HISTORY_DELIMITER_OPEN, HISTORY_DELIMITER_CLOSE, history_block,
)
from app.services.ai_service import AIService
print('phase5b_modules_ok')
print('roles:', list(CONVERSATION_MESSAGE_ROLES))
print('default_title:', DEFAULT_CONVERSATION_TITLE)
print('history_open:', HISTORY_DELIMITER_OPEN)
print('history_close:', HISTORY_DELIMITER_CLOSE)
" >/tmp/p5b_mod.txt 2>&1; then
  ok "Phase 5B modules importable (models, service, schemas, router, history delimiters)"
else
  bad "Phase 5B import failed: $(tail -n 10 /tmp/p5b_mod.txt | tr '\n' ' ')"
fi

# ---------- 3. Alembic migration 0003 applied ----------
note "Alembic migration 0003_conversations applied (idempotent re-run OK)"
if docker compose exec -T backend alembic current >/tmp/p5b_curr.txt 2>&1; then
  # ``alembic current`` prints a header line followed by the revision
  # (e.g. "Rev: 0003_conversations (head)").  Filter to lines that
  # look like revisions and pick the last match.
  CURRENT=$(grep -oE '[0-9]{4}[a-z_]+' /tmp/p5b_curr.txt | tail -n 1 || true)
  if [[ "${CURRENT}" == "0003_conversations" ]]; then
    ok "alembic current is 0003_conversations"
  else
    bad "alembic current is ${CURRENT:-<none>} (expected 0003_conversations)"
  fi
  if docker compose exec -T backend alembic upgrade head >/tmp/p5b_up.txt 2>&1; then
    ok "alembic upgrade head OK (idempotent)"
  else
    bad "alembic upgrade head failed: $(tail -n 5 /tmp/p5b_up.txt | tr '\n' ' ')"
  fi
else
  bad "alembic current failed: $(tail -n 5 /tmp/p5b_curr.txt | tr '\n' ' ')"
fi

# ---------- 4. Alembic 0003 reversible ----------
note "Alembic 0003 is reversible (downgrade then upgrade)"
if docker compose exec -T backend sh -c "alembic downgrade 0002_app_users && alembic upgrade head" \
   >/tmp/p5b_dg.txt 2>&1; then
  ok "alembic 0003 downgrade -> upgrade round-trip OK"
else
  bad "alembic round-trip failed: $(tail -n 5 /tmp/p5b_dg.txt | tr '\n' ' ')"
fi

# ---------- 5. Schema introspection ----------
note "conversations + conversation_messages schema (columns / FKs / indexes / CHECK)"
if docker compose exec -T backend python -c "
from sqlalchemy import create_engine, inspect
from app.core.config import get_settings
ins = inspect(create_engine(get_settings().database_url))
for table in ('conversations', 'conversation_messages'):
    assert table in ins.get_table_names(), f'{table} missing'
# conversations columns
cols = {c['name'] for c in ins.get_columns('conversations')}
for need in ('id','user_id','title','created_at','updated_at','last_message_at','is_archived'):
    assert need in cols, f'conversations missing column {need}'
# messages columns
mcols = {c['name'] for c in ins.get_columns('conversation_messages')}
for need in ('id','conversation_id','role','content','created_at','operation_type',
             'model_alias','grounding_metadata','evidence_references',
             'warnings','token_usage','error_code'):
    assert need in mcols, f'conversation_messages missing column {need}'
# FKs with CASCADE — SQLAlchemy exposes ``options`` as a dict
# (e.g. {'ondelete': 'CASCADE'}) on modern releases.
def _has_cascade(fk: dict) -> bool:
    opts = fk.get('options') or {}
    if isinstance(opts, str):
        return 'cascade' in opts.lower()
    if isinstance(opts, dict):
        for key in ('ondelete', 'onupdate'):
            val = opts.get(key)
            if isinstance(val, str) and 'cascade' in val.lower():
                return True
    return False

fk_conv = ins.get_foreign_keys('conversations')
fk_msg = ins.get_foreign_keys('conversation_messages')
assert any(f['referred_table']=='app_users' and _has_cascade(f)
           for f in fk_conv), 'conversations.user_id must CASCADE to app_users'
assert any(f['referred_table']=='conversations' and _has_cascade(f)
           for f in fk_msg), 'messages.conversation_id must CASCADE to conversations'
# Indexes
idx_conv = {i['name'] for i in ins.get_indexes('conversations')}
for need in ('ix_conversations_user_id','ix_conversations_user_updated',
             'ix_conversations_user_archived_lastmsg'):
    assert need in idx_conv, f'conversations missing index {need}'
idx_msg = {i['name'] for i in ins.get_indexes('conversation_messages')}
for need in ('ix_conversation_messages_conv_created','ix_conversation_messages_conv_role'):
    assert need in idx_msg, f'messages missing index {need}'
# CHECK constraints
chk_msg = {c['sqltext'] for c in ins.get_check_constraints('conversation_messages')}
assert any('USER' in s and 'ASSISTANT' in s and 'SYSTEM_EVENT' in s for s in chk_msg), \
    'messages role CHECK missing'
chk_conv = {c['sqltext'] for c in ins.get_check_constraints('conversations')}
assert any('length' in s for s in chk_conv), 'conversations title length CHECK missing'
print('schema_ok')
" >/tmp/p5b_schema.txt 2>&1; then
  ok "Schema OK (columns + CASCADE FKs + indexes + role CHECK + title length CHECK)"
else
  bad "Schema check failed: $(tail -n 10 /tmp/p5b_schema.txt | tr '\n' ' ')"
fi

# ---------- 6. CASCADE behaviour ----------
note "CASCADE behaviour: deleting a conversation removes its messages"
if docker compose exec -T backend python -c "
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings
eng = create_engine(get_settings().database_url)
Session = sessionmaker(bind=eng, future=True)
s = Session()
# Create a synthetic user row directly (cleaned up at the end).
s.execute(text(\"INSERT INTO app_users (email, password_hash, display_name, role) VALUES ('p5b-cascade@example.com', 'x', 'Cascade', 'ADMIN') ON CONFLICT (email) DO NOTHING\"))
s.commit()
uid = s.execute(text(\"SELECT id FROM app_users WHERE email='p5b-cascade@example.com'\")).scalar_one()
s.execute(text(\"INSERT INTO conversations (user_id, title) VALUES (:uid, 'cascade-test')\"), {'uid': uid})
s.commit()
cid = s.execute(text(\"SELECT id FROM conversations WHERE user_id=:uid ORDER BY id DESC LIMIT 1\"), {'uid': uid}).scalar_one()
s.execute(text(\"INSERT INTO conversation_messages (conversation_id, role, content) VALUES (:cid, 'USER', 'hello'), (:cid, 'ASSISTANT', 'hi')\"), {'cid': cid})
s.commit()
# Count messages before delete.
before = s.execute(text(\"SELECT count(*) FROM conversation_messages WHERE conversation_id=:cid\"), {'cid': cid}).scalar_one()
assert before == 2, before
# Delete conversation -> CASCADE removes messages.
s.execute(text(\"DELETE FROM conversations WHERE id=:cid\"), {'cid': cid})
s.commit()
after = s.execute(text(\"SELECT count(*) FROM conversation_messages WHERE conversation_id=:cid\"), {'cid': cid}).scalar_one()
assert after == 0, f'CASCADE failed: {after} messages survived'
# Cleanup the synthetic user (cascades to any leftover conversations).
s.execute(text(\"DELETE FROM app_users WHERE email='p5b-cascade@example.com'\"))
s.commit()
print('cascade_ok')
" >/tmp/p5b_cascade.txt 2>&1; then
  ok "ON DELETE CASCADE removes messages (2 messages -> 0 after conversation delete)"
else
  bad "CASCADE behaviour check failed: $(tail -n 10 /tmp/p5b_cascade.txt | tr '\n' ' ')"
fi

# ---------- 7. Hermetic end-to-end Phase 5B behaviour ----------
note "Hermetic Phase 5B end-to-end (IDOR / RBAC / AI conversation / prompt injection / auth-disabled)"
if docker compose exec -T -e PYTHONPATH=/app backend python -m pytest \
   tests/test_conversation_models.py \
   tests/test_conversation_migration_ddl.py \
   tests/test_conversation_service.py \
   tests/test_conversation_routes.py \
   tests/test_conversation_ai_integration.py \
   tests/test_conversation_security.py \
   --no-header -q >/tmp/p5b_pytest.txt 2>&1; then
  P5B_PASS=$(grep -oE '[0-9]+ passed' /tmp/p5b_pytest.txt | tail -n 1 || echo "?")
  ok "Phase 5B pytest suite green (${P5B_PASS})"
else
  bad "Phase 5B pytest failed: $(tail -n 30 /tmp/p5b_pytest.txt | tr '\n' ' ')"
fi

# ---------- 8. Phase 5A regression (re-run after migration changes) ----------
note "Phase 5A regression (re-run after migration)"
if bash scripts/phase5a_verify.sh >/tmp/phase5a2.log 2>&1; then
  P5A2_PASS=$(grep -oE '[0-9]+ passed, [0-9]+ failed' /tmp/phase5a2.log | tail -n 1 || echo "?")
  ok "phase5a_verify.sh still green after Phase 5B (${P5A2_PASS})"
else
  bad "phase5a_verify.sh regressed after Phase 5B - see /tmp/phase5a2.log"
fi

# ---------- 9. No AWS mutation APIs ----------
note "No write/mutation AWS APIs (delegated to phase5a_verify.sh)"
if grep -qE "PASS.*No write/mutation AWS APIs" /tmp/phase5a.log 2>/dev/null; then
  ok "No write/mutation AWS APIs (delegated)"
else
  bad "phase5a_verify did not record the no-mutation-AWS check"
fi

# ---------- 10. No AI_ESTIMATE anywhere ----------
note "No AI_ESTIMATE token in tracked source / docs"
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

# ---------- 11. No WebSockets / streaming / SSE / Redis pub/sub introduced ----------
note "No WebSockets / streaming / SSE / Redis pub/sub in Phase 5B"
HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase*_verify.sh' \
  --exclude='docs/phase*-report.md' \
  --exclude='docs/phase5b-conversation-persistence.md' \
  -e 'WebSocket' \
  -e 'websocket' \
  -e 'websockets' \
  -e 'StreamingResponse' \
  -e 'EventSourceResponse' \
  -e 'Socket.IO' \
  -e 'socketio' \
  -e 'redis.pubsub' \
  -e 'aioredis' \
  backend/app 2>/dev/null || true)
if [[ -z "${HITS}" ]]; then
  ok "No WebSockets / streaming / SSE / Socket.IO / Redis pub/sub in backend/app"
else
  bad "WebSocket / streaming surface introduced: ${HITS}"
fi

# ---------- 12. Secret scan ----------
note "Secret scan (extended: no JWT_SECRET values either)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase*_verify.sh' \
  --exclude='security.md' \
  --exclude='phase*-report.md' \
  --exclude='docs/phase*-cost-intelligence.md' \
  --exclude='docs/phase3-optimization-intelligence.md' \
  --exclude='docs/phase4-ai-cost-analyst.md' \
  --exclude='docs/phase5a-*' \
  --exclude='docs/phase5b-*' \
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

# ---------- 13. Only nginx publishes to a host port ----------
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

# ---------- 14. All containers healthy ----------
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

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 5B verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
