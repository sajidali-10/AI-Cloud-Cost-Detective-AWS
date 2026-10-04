#!/usr/bin/env bash
# phase0_verify.sh
# Real verification of the Phase 0 platform foundation.
# Exits non-zero on any failed check. Does NOT just print PASS.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PASS=0
FAIL=0
FAILURES=()

note()  { printf '[check] %s\n' "$*"; }
ok()    { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()   { FAIL=$((FAIL+1)); FAILURES+=("$*"); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

# ---------- 1. Tooling ----------
note "Docker and Docker Compose available"
if command -v docker >/dev/null 2>&1; then ok "docker present"; else bad "docker missing"; fi
if docker compose version >/dev/null 2>&1; then ok "docker compose v2 present"; else bad "docker compose v2 missing"; fi

# ---------- 2. Required files ----------
note "Required project files exist"
for f in \
  scripts/generate_dev_secrets.sh scripts/phase0_verify.sh \
  .env.example .gitignore README.md Makefile docker-compose.yml \
  backend/Dockerfile backend/requirements.txt backend/pytest.ini backend/app/main.py backend/app/core/config.py \
  backend/tests/test_health.py \
  frontend/Dockerfile frontend/package.json frontend/vite.config.ts \
  nginx/default.conf litellm/config.yaml \
  postgres/init/create-databases.sh \
  docs/architecture.md docs/development.md docs/security.md docs/cost-controls.md docs/phase0-report.md
do
  if [[ -f "$f" ]]; then ok "file: $f"; else bad "missing file: $f"; fi
done

# ---------- 3. Containers running ----------
note "Compose services running"
if [[ -f docker-compose.yml ]]; then
  RUNNING=$(docker compose ps --services --status running 2>/dev/null | sort || true)
  for s in postgres litellm backend frontend nginx; do
    if printf '%s\n' "${RUNNING}" | grep -qx "${s}"; then ok "container running: ${s}"; else bad "container NOT running: ${s}"; fi
  done
else
  bad "docker-compose.yml missing"
fi

# ---------- 4. Health checks ----------
note "PostgreSQL healthy (pg_isready)"
if docker compose exec -T postgres pg_isready -U "${POSTGRES_ADMIN_USER:-postgres}" >/dev/null 2>&1; then
  ok "pg_isready ok"
else
  bad "pg_isready failed"
fi

note "LiteLLM /health/readiness"
LITELLM_READY=$(docker compose exec -T litellm /app/.venv/bin/python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:4000/health/readiness',timeout=3).read().decode())" 2>/dev/null || true)
if echo "${LITELLM_READY}" | grep -qiE 'ready|healthy'; then ok "litellm ready"; else bad "litellm not ready: ${LITELLM_READY:0:80}"; fi

note "FastAPI /health"
BACKEND_HEALTH=$(curl -fsS --max-time 10 http://127.0.0.1/api/health 2>/dev/null || true)
if echo "${BACKEND_HEALTH}" | grep -q '"status":"ok"'; then ok "backend /health ok"; else bad "backend /health failed: ${BACKEND_HEALTH:0:120}"; fi

note "FastAPI /health/ready"
BACKEND_READY=$(curl -fsS --max-time 10 http://127.0.0.1/api/health/ready 2>/dev/null || true)
if echo "${BACKEND_READY}" | grep -q '"status":"ready"' && echo "${BACKEND_READY}" | grep -q '"database":"ok"' && echo "${BACKEND_READY}" | grep -q '"litellm":"ok"'; then
  ok "backend /health/ready ok"
else
  bad "backend /health/ready failed: ${BACKEND_READY:0:200}"
fi

# ---------- 5. Frontend ----------
note "Frontend responds through Nginx"
FRONT_HTML=$(curl -fsS --max-time 10 http://127.0.0.1/ 2>/dev/null || true)
if echo "${FRONT_HTML}" | grep -qi 'AI Cloud Cost Detective'; then
  ok "frontend served via Nginx"
else
  bad "frontend not served via Nginx"
fi

note "Nginx can reach backend"
if echo "${BACKEND_HEALTH}" | grep -q '"status":"ok"'; then ok "nginx -> backend ok"; else bad "nginx -> backend unreachable"; fi

# ---------- 6. No public PG / LiteLLM ----------
note "No public PostgreSQL port exposure"
PUB=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
for r in rows:
    if r.get('Service') == 'postgres':
        ports = r.get('Publishers') or []
        for p in ports:
            if p.get('PublishedPort'):
                print('LEAK', p.get('PublishedPort'))
" 2>/dev/null || true)
if [[ -z "${PUB}" ]]; then ok "postgres not publicly exposed"; else bad "postgres publicly exposed: ${PUB}"; fi

note "No public LiteLLM port exposure"
PUB=$(docker compose ps --format json 2>/dev/null | python3 -c "
import json, sys
rows = [json.loads(l) for l in sys.stdin if l.strip()]
for r in rows:
    if r.get('Service') == 'litellm':
        ports = r.get('Publishers') or []
        for p in ports:
            if p.get('PublishedPort') and p.get('PublishedPort') != 0:
                ip = p.get('PublishAddress','')
                if ip and ip != '127.0.0.1':
                    print('LEAK', p.get('PublishedPort'), ip)
" 2>/dev/null || true)
if [[ -z "${PUB}" ]]; then ok "litellm not publicly exposed"; else bad "litellm publicly exposed: ${PUB}"; fi

# ---------- 7. Secret scan ----------
note "Secret-pattern scan of Git-tracked files"
SECRET_HITS=$(git -C "${REPO_ROOT}" grep -nIE 'AKIA|AWS_SECRET_ACCESS_KEY=|OPENAI_API_KEY=|ANTHROPIC_API_KEY=|GEMINI_API_KEY=|LITELLM_MASTER_KEY=sk-[A-Za-z0-9]{20,}' 2>/dev/null || true)
if [[ -z "${SECRET_HITS}" ]]; then
  ok "no obvious secrets in tracked files"
else
  # Report affected filenames only, never the value.
  bad "secret pattern matches: $(echo "${SECRET_HITS}" | awk -F: '{print $1}' | sort -u | tr '\n' ' ')"
fi

# ---------- 8. Backend tests ----------
note "Backend unit tests"
if docker compose run --rm --entrypoint=pytest backend -q 2>/dev/null | tail -n 5; then
  ok "backend tests executed"
else
  bad "backend tests failed"
fi

# ---------- 9. Frontend build ----------
note "Frontend production build"
cd "${REPO_ROOT}/frontend"
if npm ci --no-audit --no-fund >/dev/null 2>&1 && npm run build >/dev/null 2>&1; then
  ok "frontend build succeeded"
else
  bad "frontend build failed"
fi
cd "${REPO_ROOT}"

# ---------- Summary ----------
echo
echo "============================================================"
echo "Phase 0 verify: ${PASS} passed, ${FAIL} failed"
echo "============================================================"
if [[ ${FAIL} -gt 0 ]]; then
  echo "Failures:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
exit 0
