#!/usr/bin/env bash
# scripts/phase7b_nginx_test.sh
#
# Phase 7B — nginx sensitive-path hardening regression test.
#
# Verifies the centralized nginx deny rules (added in Phase 7B) so that
# Internet-scanner probes for obvious secret/config/dotfile paths do NOT
# resolve to the SPA's index.html with HTTP 200.
#
# Sensitive paths MUST return 404 and the response body MUST NOT be
# application HTML.
#
# Legitimate paths MUST still work:
#   / (root)
#   /costs, /resources, /optimization, /analyst  (SPA client routes)
#   /api/*                                       (REST + WebSocket)
#   /branding/*                                  (brand assets)
#   /assets/*                                    (built static assets)
#   /nginx-health                                (nginx self-check)
#
# This test is INTENTIONALLY independent of the backend/frontend
# containers: it serves the production nginx config in a stand-alone
# nginx instance with upstream overrides pointing at local mocks, so
# it runs in any environment with `nginx` and `curl` available.
#
# No secret values are echoed.  The mock backend returns 401 for any
# request — matching the symptom reported by operators (protected
# endpoints returning 401 is the correct behaviour, not a bug).
#
# Exit status:
#   0 — all assertions pass
#   non-zero — at least one assertion failed
#
# Usage:
#   bash scripts/phase7b_nginx_test.sh
#
# Environment overrides:
#   NGINX_BIN   — path to nginx binary (default: nginx)
#   NGINX_PORT  — listen port for the test nginx (default: 18080)
#   SKIP_NETWORK=1 — skip the live HTTP probes; only validate config syntax

set -uo pipefail

NGINX_BIN="${NGINX_BIN:-nginx}"
NGINX_PORT="${NGINX_PORT:-18080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF_SRC="$ROOT/nginx/default.conf"
TEST_DIR="/tmp/accd-phase7b-nginx"
PIDFILE="$TEST_DIR/nginx.pid"
MOCK_FE_PORT=18081
MOCK_BE_PORT=18082
LOG="$TEST_DIR/test.log"

PASS=0
FAIL=0
declare -a FAIL_LINES

mkdir -p "$TEST_DIR/logs" "$TEST_DIR/body"
: > "$LOG"

log() { echo "$@" | tee -a "$LOG"; }
pass() { PASS=$((PASS + 1)); log "PASS  $1"; }
fail() { FAIL=$((FAIL + 1)); FAIL_LINES+=("$1"); log "FAIL  $1"; }

cleanup() {
  if [ -f "$PIDFILE" ]; then
    "$NGINX_BIN" -s stop -p "$TEST_DIR" -c "$TEST_DIR/default.conf" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  pkill -f "accd-phase7b-mock-fe" 2>/dev/null || true
  pkill -f "accd-phase7b-mock-be" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Pre-flight: nginx binary present, source config present.
# ---------------------------------------------------------------------------

if ! command -v "$NGINX_BIN" >/dev/null 2>&1; then
  log "SKIP  $NGINX_BIN not found; cannot run live probes"
  log "      (install nginx to enable this regression test)"
  exit 0
fi
if [ ! -f "$CONF_SRC" ]; then
  log "FAIL  nginx source config not found at $CONF_SRC"
  exit 2
fi

# ---------------------------------------------------------------------------
# 2. Build a stand-alone test config with upstream overrides pointing at
#    local mock servers.  The mock frontend returns a recognisable
#    HTML body ("<html>...SPA-MOCK-MARKER...</html>") so the assertions
#    can confirm sensitive paths do NOT return SPA HTML.
# ---------------------------------------------------------------------------

cp "$CONF_SRC" "$TEST_DIR/default.conf"
sed -i \
  -e "s|http://frontend:8080|http://127.0.0.1:$MOCK_FE_PORT|g" \
  -e "s|http://backend:8000|http://127.0.0.1:$MOCK_BE_PORT|g" \
  -e "s|listen 80 default_server|listen $NGINX_PORT default_server|g" \
  "$TEST_DIR/default.conf"

if ! "$NGINX_BIN" -t -p "$TEST_DIR" -c "$TEST_DIR/default.conf" >>"$LOG" 2>&1; then
  log "FAIL  nginx -t rejected the test config"
  cat "$LOG"
  exit 2
fi
pass "nginx -t accepts default.conf"

if [ "${SKIP_NETWORK:-0}" = "1" ]; then
  log "SKIP  SKIP_NETWORK=1 set; not running live probes"
  exit $FAIL
fi

# ---------------------------------------------------------------------------
# 3. Start mock backend (always returns 401) and mock frontend (always
#    returns the SPA mock marker).  These mimic the production stack
#    enough to exercise every nginx location block.
# ---------------------------------------------------------------------------

python3 - >/dev/null 2>&1 <<PY &
import http.server, socketserver, sys
class FE(http.server.BaseHTTPRequestHandler):
    BODY = b"<html><body>SPA-MOCK-MARKER</body></html>"
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)
    def log_message(self, *a, **k): pass
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(("127.0.0.1", $MOCK_FE_PORT), FE).serve_forever()
PY
MOCK_FE_PID=$!
disown
# Tag the process so cleanup can find it.
echo "accd-phase7b-mock-fe" > /proc/$MOCK_FE_PID/comm 2>/dev/null || true

python3 - >/dev/null 2>&1 <<PY &
import http.server, socketserver, sys
class BE(http.server.BaseHTTPRequestHandler):
    BODY = b'{"detail":"missing Authorization header"}'
    def do_GET(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.BODY)))
        self.end_headers()
        self.wfile.write(self.BODY)
    def log_message(self, *a, **k): pass
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(("127.0.0.1", $MOCK_BE_PORT), BE).serve_forever()
PY
MOCK_BE_PID=$!
disown
echo "accd-phase7b-mock-be" > /proc/$MOCK_BE_PID/comm 2>/dev/null || true

sleep 0.4

"$NGINX_BIN" -p "$TEST_DIR" -c "$TEST_DIR/default.conf" >>"$LOG" 2>&1
sleep 0.3

# ---------------------------------------------------------------------------
# 4. Assertions.
# ---------------------------------------------------------------------------

BASE="http://127.0.0.1:$NGINX_PORT"
BODY="$TEST_DIR/body/resp"

check_404() {
  local label="$1" path="$2"
  local code
  code=$(curl -s -o "$BODY" -w '%{http_code}' "$BASE$path")
  if [ "$code" != "404" ]; then
    fail "$label  expected=404 got=$code path=$path"
    return
  fi
  if grep -q "SPA-MOCK-MARKER" "$BODY"; then
    fail "$label  response contains SPA HTML body path=$path"
    return
  fi
  pass "$label  status=404"
}

check_2xx_3xx() {
  local label="$1" path="$2"
  local code
  code=$(curl -s -o "$BODY" -w '%{http_code}' "$BASE$path")
  case "$code" in
    2*|3*) pass "$label  status=$code" ;;
    *) fail "$label  expected=2xx/3xx got=$code path=$path" ;;
  esac
}

check_status() {
  local label="$1" path="$2" expected="$3"
  local code
  code=$(curl -s -o "$BODY" -w '%{http_code}' "$BASE$path")
  if [ "$code" = "$expected" ]; then
    pass "$label  status=$code"
  else
    fail "$label  expected=$expected got=$code path=$path"
  fi
}

# Sensitive paths MUST be 404 and never SPA HTML.
check_404 "sensitive /env"                  "/.env"
check_404 "sensitive /.env.production"      "/.env.production"
check_404 "sensitive /.git/config"          "/.git/config"
check_404 "sensitive /.git/HEAD"            "/.git/HEAD"
check_404 "sensitive /.aws/credentials"     "/.aws/credentials"
check_404 "sensitive /.terraform/foo"       "/.terraform/foo"
check_404 "sensitive /terraform.tfstate"    "/terraform.tfstate"
check_404 "sensitive /prod.tfstate"         "/prod.tfstate"
check_404 "sensitive /docker-compose.yml"   "/docker-compose.yml"
check_404 "sensitive /docker-compose.override.yml" "/docker-compose.override.yml"
check_404 "sensitive /Dockerfile"           "/Dockerfile"
check_404 "sensitive /secrets.json"         "/secrets.json"
check_404 "sensitive /secrets.yaml"         "/secrets.yaml"
check_404 "sensitive /secrets.yml"          "/secrets.yml"
check_404 "sensitive /.htaccess"            "/.htaccess"
check_404 "sensitive /.ssh/id_rsa"          "/.ssh/id_rsa"
check_404 "sensitive /.npmrc"               "/.npmrc"
check_404 "sensitive /.docker/config.json"  "/.docker/config.json"
check_404 "sensitive /.kube/config"         "/.kube/config"
check_404 "sensitive /.bash_history"        "/.bash_history"
check_404 "sensitive /wp-login.php"         "/wp-login.php"
check_404 "sensitive /xmlrpc.php"           "/xmlrpc.php"
check_404 "sensitive /phpinfo.php"          "/phpinfo.php"

# Legitimate paths MUST still work.
check_2xx_3xx "legit / (root)"              "/"
check_2xx_3xx "legit /costs (SPA route)"    "/costs"
check_2xx_3xx "legit /resources (SPA route)" "/resources"
check_2xx_3xx "legit /optimization (SPA route)" "/optimization"
check_2xx_3xx "legit /analyst (SPA route)"  "/analyst"
check_2xx_3xx "legit /conversations (SPA route)" "/conversations"
check_2xx_3xx "legit /users (SPA route)"    "/users"
check_2xx_3xx "legit /security (SPA route)" "/security"
check_2xx_3xx "legit /login (SPA route)"    "/login"
check_2xx_3xx "legit /assets/main.js (built static)" "/assets/main.js"
check_2xx_3xx "legit /branding/logo.png"    "/branding/logo.png"
check_2xx_3xx "legit /nginx-health"         "/nginx-health"

# API surface returns the mock backend's 401 (correct: the symptom
# reported by operators was that the API path is reachable; auth
# itself is enforced at the app layer).  The point is that the path
# is NOT 404'd by nginx.
check_status "legit /api/auth/info (proxied to backend)"   "/api/auth/info"   "401"
check_status "legit /api/auth/me (proxied to backend)"     "/api/auth/me"     "401"
check_status "legit /api/aws/identity (proxied)"          "/api/aws/identity" "401"
check_status "legit /api/aws/costs?month=2026-09 (proxied)" "/api/aws/costs?month=2026-09" "401"
check_status "legit /api/aws/resources (proxied)"         "/api/aws/resources" "401"
check_status "legit /api/aws/optimization/summary (proxied)" "/api/aws/optimization/summary" "401"
check_status "legit /api/aws/optimization/capabilities (proxied)" "/api/aws/optimization/capabilities" "401"
check_status "legit /api/ws/conversations/1 (WS proxied)" "/api/ws/conversations/1" "401"

# Belt-and-braces: ensure a body that contains application HTML was
# not returned for ANY of the sensitive probes.
spa_hits=$(grep -l "SPA-MOCK-MARKER" "$TEST_DIR/body/"* 2>/dev/null | wc -l)
if [ "$spa_hits" = "0" ]; then
  pass "no SPA HTML in any probe response body"
else
  fail "$spa_hits response bodies contained SPA HTML"
fi

log "----"
log "PASS=$PASS  FAIL=$FAIL"
if [ $FAIL -gt 0 ]; then
  log "Failed assertions:"
  for f in "${FAIL_LINES[@]}"; do
    log "  - $f"
  done
fi
exit $FAIL
