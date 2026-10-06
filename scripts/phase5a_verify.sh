#!/usr/bin/env bash
# phase5a_verify.sh — Phase 5A (Authentication + RBAC Foundation) verifier.
#
# Composes phase4_verify.sh with Phase 5A-specific checks:
#   1. Phase 4 regression (delegated to phase4_verify.sh).
#   2. Phase 5A modules importable.
#   3. Alembic migration applied (idempotent re-run is a no-op).
#   4. Password hashing round-trip (Argon2id).
#   5. JWT issuance + verification happy path.
#   6. JWT failure paths (expired, malformed, invalid signature).
#   7. AUTH_ENABLED=false path: AWS + AI + login anonymous.
#   8. AUTH_ENABLED=true path: 401 without token, 200 with valid token,
#      403 for wrong role, admin-only routes denied to non-admin.
#   9. VIEWER denied AI generation; ANALYST allowed.
#  10. No password hash, JWT, or secret leakage in response bodies.
#  11. Logs do not contain passwords, JWTs, Authorization headers, or hashes.
#  12. No AWS mutation APIs (delegated to phase4_verify).
#  13. Secret scan (extended: no JWT_SECRET values either).
#  14. Only nginx is host-published.
#  15. Docker health (delegated to phase4_verify).
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

# ---------- 1. Phase 4 regression ----------
note "Phase 4 regression check (delegated to phase4_verify.sh)"
if bash scripts/phase4_verify.sh >/tmp/phase4.log 2>&1; then
  P4_PASS=$(grep -oE '[0-9]+ passed' /tmp/phase4.log | tail -n 1 || echo "?")
  ok "phase4_verify.sh exited 0 (${P4_PASS})"
else
  bad "phase4_verify.sh failed - see /tmp/phase4.log"
fi

# ---------- 2. Phase 5A modules importable ----------
note "Phase 5A modules importable"
if docker compose exec -T backend python -c "
from app.core.config import Settings, get_settings
from app.core.security import (
    SecurityCore, TokenClaims,
    TokenError, TokenExpired, TokenInvalid, TokenSignatureInvalid,
    get_security_core, reset_security_core_for_tests,
)
from app.db.models import AppUser, APP_USER_ROLES
from app.services.password_hasher import PasswordHasher
from app.services.auth_service import (
    AuthService, get_auth_service,
    InvalidCredentials, UserAlreadyExists, UserNotFound, UserInactive, InvalidRole,
    normalise_email, validate_role,
)
from app.schemas.auth import (
    RoleName, LoginRequest, LoginResponse, AuthInfoResponse,
    PublicUserView, AdminUserView, AdminUserCreateRequest,
    AdminUserPatchRequest, AdminUserListResponse,
)
from app.api.deps import get_current_user, require_role, require_admin
from app.api.auth import router as auth_router
from app.api.admin_users import router as admin_users_router
print('phase5a_modules_ok')
" >/tmp/p5a_mod.txt 2>&1; then
  ok "Phase 5A modules importable (config, security, models, auth, deps, routers)"
else
  bad "Phase 5A import failed: $(tail -n 10 /tmp/p5a_mod.txt | tr '\n' ' ')"
fi

# ---------- 3. Alembic migration ----------
note "Alembic migration 0002_app_users applied (idempotent re-run OK)"
if docker compose exec -T backend alembic current >/tmp/p5a_alembic_curr.txt 2>&1; then
  CURRENT=$(grep -oE '[0-9a-z_]+' /tmp/p5a_alembic_curr.txt | head -n 1 || true)
  if docker compose exec -T backend alembic upgrade head >/tmp/p5a_alembic_up.txt 2>&1; then
    ok "alembic upgrade head OK (current=${CURRENT})"
  else
    bad "alembic upgrade head failed: $(tail -n 5 /tmp/p5a_alembic_up.txt | tr '\n' ' ')"
  fi
else
  bad "alembic current failed: $(tail -n 5 /tmp/p5a_alembic_curr.txt | tr '\n' ' ')"
fi

note "Alembic 0002 is reversible (downgrade then upgrade)"
if docker compose exec -T backend sh -c "alembic downgrade 0001_cost_cache && alembic upgrade head" \
   >/tmp/p5a_alembic_dg.txt 2>&1; then
  ok "alembic downgrade -> upgrade round-trip OK"
else
  bad "alembic downgrade -> upgrade round-trip failed: $(tail -n 5 /tmp/p5a_alembic_dg.txt | tr '\n' ' ')"
fi

note "app_users table exists with expected columns"
if docker compose exec -T backend python -c "
from sqlalchemy import create_engine, inspect
from app.core.config import get_settings
ins = inspect(create_engine(get_settings().database_url))
tables = ins.get_table_names()
assert 'app_users' in tables, 'app_users missing'
cols = {c['name'] for c in ins.get_columns('app_users')}
for need in ('id','email','password_hash','display_name','role','is_active',
             'created_at','updated_at','last_login_at'):
    assert need in cols, f'missing column {need}'
uniques = {tuple(c['column_names']) for c in ins.get_unique_constraints('app_users')}
assert ('email',) in uniques, 'email must be UNIQUE'
indexes = {i['name'] for i in ins.get_indexes('app_users')}
assert any('role' in n for n in indexes), 'role index missing'
print('app_users_schema_ok')
" >/tmp/p5a_schema.txt 2>&1; then
  ok "app_users schema OK (columns + UNIQUE email + role index)"
else
  bad "app_users schema check failed: $(tail -n 5 /tmp/p5a_schema.txt | tr '\n' ' ')"
fi

# ---------- 4. Password hashing ----------
note "Password hashing round-trip (Argon2id)"
if docker compose exec -T backend python -c "
from app.services.password_hasher import PasswordHasher
h = PasswordHasher(time_cost=1, memory_cost=8*1024, parallelism=1, hash_length=16, salt_length=8)
a = h.hash('correct-horse-battery-staple')
b = h.hash('correct-horse-battery-staple')
assert a != b, 'two hashes of the same password must differ (salt)'
assert h.verify('correct-horse-battery-staple', a)
assert not h.verify('wrong-password', a)
assert a.startswith('\$argon2id\$'), a[:30]
print('password_hash_ok')
" >/tmp/p5a_hash.txt 2>&1; then
  ok "Argon2id hash + verify OK"
else
  bad "password hashing failed: $(tail -n 5 /tmp/p5a_hash.txt | tr '\n' ' ')"
fi

# ---------- 5-6. JWT issuance + verification ----------
note "JWT issuance + verification happy path"
if docker compose exec -T backend python -c "
from app.core.config import Settings
from app.core.security import SecurityCore
s = Settings(
    app_env='test', auth_enabled=True,
    jwt_secret='x'*40,
    jwt_access_token_minutes=5,
)
c = SecurityCore(settings=s)
t = c.issue_access_token(user_id=42, role='ADMIN')
assert t.count('.') == 2
claims = c.decode_token(t)
assert claims.sub == '42'
assert claims.role == 'ADMIN'
print('jwt_happy_ok')
" >/tmp/p5a_jwt.txt 2>&1; then
  ok "JWT happy-path OK"
else
  bad "JWT happy-path failed: $(tail -n 5 /tmp/p5a_jwt.txt | tr '\n' ' ')"
fi

note "JWT failure paths: expired, malformed, invalid signature"
if docker compose exec -T backend python -c "
import time, jwt as pyjwt
from app.core.config import Settings
from app.core.security import (
    SecurityCore, TokenExpired, TokenInvalid, TokenSignatureInvalid,
)
s = Settings(app_env='test', auth_enabled=True, jwt_secret='x'*40, jwt_access_token_minutes=5)
c = SecurityCore(settings=s)
# Expired
now = int(time.time())
expired = pyjwt.encode({'sub':'1','role':'ADMIN','iat':now-3600,'exp':now-60,
                        'iss':s.jwt_issuer,'aud':s.jwt_audience,'jti':'x'},
                       s.jwt_secret, algorithm='HS256')
try:
    c.decode_token(expired)
    raise SystemExit('expected TokenExpired')
except TokenExpired:
    pass
# Malformed
try:
    c.decode_token('not.a.jwt')
    raise SystemExit('expected TokenInvalid')
except TokenInvalid:
    pass
# Invalid signature
t = c.issue_access_token(user_id=1, role='ADMIN')
h, m, sig = t.split('.')
bad_sig = f'{h}.{m}.{sig[:-2]}AA'
try:
    c.decode_token(bad_sig)
    raise SystemExit('expected TokenSignatureInvalid')
except TokenSignatureInvalid:
    pass
# alg=none downgrade
none_tok = pyjwt.encode({'sub':'1','role':'ADMIN','iat':now,'exp':now+60,
                          'iss':s.jwt_issuer,'aud':s.jwt_audience,'jti':'x'},
                         key='', algorithm='none')
try:
    c.decode_token(none_tok)
    raise SystemExit('expected TokenInvalid for alg=none')
except TokenInvalid:
    pass
print('jwt_failure_ok')
" >/tmp/p5a_jwtfail.txt 2>&1; then
  ok "JWT failure paths OK (expired / malformed / bad signature / alg=none)"
else
  bad "JWT failure paths failed: $(tail -n 5 /tmp/p5a_jwtfail.txt | tr '\n' ' ')"
fi

# Ensure the migration is applied before hermetic tests run (the
# earlier downgrade->upgrade round-trip may have left the schema
# in a transient state on some Postgres versions).
docker compose exec -T backend alembic upgrade head >/dev/null 2>&1 || true

# ---------- 7. AUTH_ENABLED=false (backward-compat) ----------
note "AUTH_ENABLED=false path: AWS + AI + auth-info anonymous"
RESP=$(curl -sS --max-time 30 'http://127.0.0.1/api/auth/info' 2>/dev/null || true)
if echo "${RESP}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
assert r.get('auth_enabled') is False, r
assert 'secret' not in json.dumps(r).lower()
print('ok')
" 2>/dev/null | grep -q '^ok'; then
  ok "GET /api/auth/info reports auth_enabled=false + no secret leakage"
else
  bad "/api/auth/info did not report disabled state: ${RESP:0:200}"
fi

# AI status with auth disabled still works (anonymous).
AISTATUS=$(curl -sS --max-time 30 'http://127.0.0.1/api/ai/status' 2>/dev/null || true)
if [[ -n "${AISTATUS}" ]] && echo "${AISTATUS}" | python3 -c "
import json, sys
r = json.load(sys.stdin)
assert 'status' in r
print('ok')
" 2>/dev/null | grep -q '^ok'; then
  ok "GET /api/ai/status reachable with AUTH_ENABLED=false"
else
  bad "GET /api/ai/status unreachable with AUTH_ENABLED=false: ${AISTATUS:0:200}"
fi

# ---------- 8. AUTH_ENABLED=true enforcement ----------
# The full enforcement test (401/403/role-boundary/no-hash-leak)
# runs as a self-contained Python file copied into the backend
# container.  This avoids shell-quoting of inline ``python -c``
# blocks and ensures StaticPool SQLite behaves deterministically.
note "AUTH_ENABLED=true: 401 / 200 / 403 / role boundaries / no hash leak"
if docker compose exec -T -e PYTHONPATH=/app backend sh -c 'cat > /tmp/p5a_enforce.py' \
   < /home/ubuntu/ai-cloud-cost-detective/scripts/_phase5a_enforcement_test.py \
   >/dev/null 2>&1; then
  if docker compose exec -T -e PYTHONPATH=/app backend python /tmp/p5a_enforce.py \
       >/tmp/p5a_enforce.txt 2>&1; then
    ok "AUTH_ENABLED=true enforcement: 401 / 403 / role boundaries OK"
  else
    bad "AUTH_ENABLED=true enforcement failed: $(tail -n 15 /tmp/p5a_enforce.txt | tr '\n' ' ')"
  fi
else
  bad "Could not copy enforcement test into container"
fi

# ---------- 9. Privileged-escalation negative test ----------
note "Forged role claim cannot escalate (auth layer re-loads DB role)"
if docker compose exec -T -e PYTHONPATH=/app backend sh -c 'cat > /tmp/p5a_escalation.py' \
   < /home/ubuntu/ai-cloud-cost-detective/scripts/_phase5a_escalation_test.py \
   >/dev/null 2>&1; then
  if docker compose exec -T -e PYTHONPATH=/app backend python /tmp/p5a_escalation.py \
       >/tmp/p5a_escalation.txt 2>&1; then
    ok "Forged role claim rejected (DB role used for authorization)"
  else
    bad "Privilege-escalation check failed: $(tail -n 10 /tmp/p5a_escalation.txt | tr '\n' ' ')"
  fi
else
  bad "Could not copy escalation test into container"
fi

# ---------- 10. No secrets in response bodies ----------
note "Auth response bodies contain no password_hash, JWT, or Authorization header"
LOGIN_BODY=$(curl -sS --max-time 30 -X POST -H 'Content-Type: application/json' \
             -d '{"email":"nobody@example.com","password":"password-1234"}' \
             'http://127.0.0.1/api/auth/login' 2>/dev/null || true)
if [[ -n "${LOGIN_BODY}" ]] && ! echo "${LOGIN_BODY}" | grep -qiE 'password_hash|argon2id|Authorization' ; then
  ok "Anonymous /api/auth/login response contains no hash / Authorization leak"
else
  bad "/api/auth/login response leaked credentials: ${LOGIN_BODY:0:300}"
fi

# /api/auth/info is the only anonymous path that reveals token metadata; verify it stays sanitized.
INFO=$(curl -sS --max-time 30 'http://127.0.0.1/api/auth/info' 2>/dev/null || true)
if [[ -n "${INFO}" ]] && ! echo "${INFO}" | grep -qiE 'JWT_SECRET|change-me|password' ; then
  ok "/api/auth/info response stays sanitized (no JWT_SECRET / placeholder / password)"
else
  bad "/api/auth/info leaked material: ${INFO:0:300}"
fi

# ---------- 11. Logs do not contain secrets ----------
note "Logs do not contain passwords, JWTs, Authorization headers, or hashes"
# Scan the last 1000 lines of the backend container's stdout for
# any obvious credential patterns.
LEAKS=$(docker compose logs --no-color --tail=1000 backend 2>/dev/null | \
        grep -iE 'password_hash|argon2id\\$v=|eyJ[A-Za-z0-9_-]{10,}|Authorization: Bearer ' || true)
if [[ -z "${LEAKS}" ]]; then
  ok "No password hashes, JWTs, or Authorization headers in backend logs"
else
  bad "Log leak(s): $(echo "${LEAKS}" | head -n 3 | tr '\n' ' ')"
fi

# ---------- 12. No AWS mutation APIs (delegated to phase4_verify) ----------
note "No write/mutation AWS APIs (delegated to phase4_verify.sh)"
if grep -q "no write/mutation AWS APIs in Phase 4 source" /tmp/phase4.log 2>/dev/null \
   && grep -qE "PASS.*no write/mutation AWS APIs" /tmp/phase4.log 2>/dev/null; then
  ok "No write/mutation AWS APIs (delegated)"
else
  bad "phase4_verify did not record the no-mutation-AWS check"
fi

# ---------- 13. Secret scan (extended) ----------
note "Secret scan (extended: no JWT_SECRET values either)"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE \
  --exclude='phase*_verify.sh' \
  --exclude='security.md' \
  --exclude='phase*-report.md' \
  --exclude='docs/phase*-cost-intelligence.md' \
  --exclude='docs/phase3-optimization-intelligence.md' \
  --exclude='docs/phase4-ai-cost-analyst.md' \
  --exclude='docs/phase5a-*' \
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
  ok "No obvious secrets (incl. JWT_SECRET values) in tracked files"
else
  bad "Secret pattern matches: $(echo "${SECRET_HITS}" | awk -F: '{print $1}' | sort -u | tr '\n' ' ')"
fi

# ---------- 14. Only nginx is host-published ----------
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

# ---------- 15. Docker health ----------
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

# ---------- 16. create_admin.py refuses to run with AUTH_ENABLED=false ----------
note "scripts/create_admin.py refuses when AUTH_ENABLED=false"
# The script lives at the repo root.  Run it inside the backend
# container with PYTHONPATH=/app so ``import app.*`` resolves.
cat /home/ubuntu/ai-cloud-cost-detective/scripts/create_admin.py \
  | docker compose exec -T -e AUTH_ENABLED=false -e PYTHONPATH=/app backend sh -c 'cat > /tmp/create_admin.py && python /tmp/create_admin.py --help' \
   >/tmp/p5a_cadmin_help.txt 2>&1
if grep -q 'usage: create_admin' /tmp/p5a_cadmin_help.txt; then
  ok "create_admin.py --help OK"
else
  bad "create_admin.py --help did not print usage: $(cat /tmp/p5a_cadmin_help.txt)"
fi
cat /home/ubuntu/ai-cloud-cost-detective/scripts/create_admin.py \
  | docker compose exec -T -e AUTH_ENABLED=false -e PYTHONPATH=/app backend sh -c 'cat > /tmp/create_admin.py && python /tmp/create_admin.py --email admin@example.com --display-name Admin --password verystrongpassword1234' \
   >/tmp/p5a_cadmin_run.txt 2>&1
if grep -q 'AUTH_ENABLED is false' /tmp/p5a_cadmin_run.txt; then
  ok "create_admin.py refuses when AUTH_ENABLED=false"
else
  bad "create_admin.py refusal message unclear: $(cat /tmp/p5a_cadmin_run.txt)"
fi

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 5A verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
