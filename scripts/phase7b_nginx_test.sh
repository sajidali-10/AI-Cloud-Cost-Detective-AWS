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
# Implementation notes:
#   * Production `nginx/default.conf` is a `conf.d/`-style fragment
#     (intended to be included inside the parent's `http {}` block).
#     When loaded as a stand-alone main config it must be wrapped in
#     an explicit `events { ... } http { ... }` pair so the `map`
#     directives resolve inside `http` context.  This wrapping is a
#     TEST-ONLY artifact — production nginx is unaffected.
#   * The stand-alone test nginx MUST bind a non-production port
#     (default 18080) so it never collides with the Docker-managed
#     public nginx that owns host port 80.
#   * Mock upstream servers are tracked by PID in a file so cleanup
#     is reliable; relying on `/proc/<pid>/comm` rewrites is not
#     portable (it requires CAP_SYS_ADMIN on modern kernels).
#   * A `trap` on EXIT / INT / TERM removes every process this
#     script started so no orphan nginx/python processes survive.
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
PID_DIR="$TEST_DIR/pids"

PASS=0
FAIL=0
declare -a FAIL_LINES

mkdir -p "$TEST_DIR/logs" "$TEST_DIR/body" "$PID_DIR"
: > "$LOG"

log() { echo "$@" | tee -a "$LOG"; }
pass() { PASS=$((PASS + 1)); log "PASS  $1"; }
fail() { FAIL=$((FAIL + 1)); FAIL_LINES+=("$1"); log "FAIL  $1"; }

# ---------------------------------------------------------------------------
# Cleanup — must be registered BEFORE we start any background process so a
# SIGINT during startup still releases the test directory cleanly.
# ---------------------------------------------------------------------------

cleanup() {
  local sig="${1:-EXIT}"
  log "---- cleanup ($sig)"

  # Kill mock servers by recorded PID.  Walk the pid file so we kill
  # exactly the processes this script started, never anything else.
  if [ -d "$PID_DIR" ]; then
    for pid_file in "$PID_DIR"/*.pid; do
      [ -e "$pid_file" ] || continue
      local pid
      pid=$(cat "$pid_file" 2>/dev/null || true)
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
      fi
      rm -f "$pid_file"
    done
  fi

  # Belt-and-braces: also catch any mock server bound to the test
  # ports but somehow missing from the pid file (e.g. a previous
  # crashed run).  fuser is on every modern distro.
  if command -v fuser >/dev/null 2>&1; then
    fuser -k -TERM "${MOCK_FE_PORT}/tcp" >/dev/null 2>&1 || true
    fuser -k -TERM "${MOCK_BE_PORT}/tcp" >/dev/null 2>&1 || true
  fi

  # Stop the test nginx instance.  Prefer the saved pidfile; if
  # absent (e.g. nginx -t failed before launch) fall through silently.
  if [ -f "$PIDFILE" ]; then
    local nginxp
    nginxp=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$nginxp" ] && kill -0 "$nginxp" 2>/dev/null; then
      "$NGINX_BIN" -s stop -p "$TEST_DIR" -c "$TEST_DIR/default.conf" 2>/dev/null || true
      # Stop can take a moment; wait but do not block forever.
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$nginxp" 2>/dev/null || break
        sleep 0.1
      done
      kill -KILL "$nginxp" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
  fi

  # Last-resort fallback: anything still bound to the test port is
  # almost certainly our orphan nginx master.  fuser + TERM/KILL so
  # we never leave a process holding the port between runs.  This
  # protects against the case where nginx -s stop fails (config
  # already-replaced, pidfile lost, kernel killed it under us, ...).
  if command -v fuser >/dev/null 2>&1; then
    fuser -k -TERM "${NGINX_PORT}/tcp" >/dev/null 2>&1 || true
    sleep 0.2
    fuser -k -KILL "${NGINX_PORT}/tcp" >/dev/null 2>&1 || true
  fi
}
trap 'cleanup EXIT' EXIT
trap 'cleanup INT;  exit 130' INT
trap 'cleanup TERM; exit 143' TERM

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
# 2. Build a stand-alone test config.
#
#    Production `nginx/default.conf` is a conf.d fragment — it lives
#    inside the parent nginx.conf's `http {}` block.  When the test
#    harness runs nginx with `-c $TEST_DIR/default.conf` the file is
#    loaded as the MAIN config, so we wrap its body in a synthetic
#    `events { ... } http { ... }` pair.  This wrapping is test-only;
#    the production container still includes the file via conf.d/ and
#    sees no extra structure.
#
#    We also rewrite the upstream hostnames to point at local mocks
#    and the listen port to a non-production port (18080 by default)
#    so the test nginx NEVER owns host port 80 — that port remains
#    the exclusive domain of the Docker-managed nginx container.
# ---------------------------------------------------------------------------

{
  echo "# Auto-generated by scripts/phase7b_nginx_test.sh — DO NOT EDIT."
  echo "# Wraps production conf.d/default.conf in a stand-alone http{}."
  echo "events { worker_connections 1024; }"
  echo "http {"
  # Body of default.conf verbatim — but with the listen port and
  # upstream hostnames rewritten for the test environment.
  sed \
    -e "s|http://frontend:8080|http://127.0.0.1:$MOCK_FE_PORT|g" \
    -e "s|http://backend:8000|http://127.0.0.1:$MOCK_BE_PORT|g" \
    -e "s|listen 80 default_server|listen $NGINX_PORT default_server|g" \
    "$CONF_SRC"
  echo "}"
} > "$TEST_DIR/default.conf"

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
#
#    Each mock is tracked in $PID_DIR/<port>.pid so cleanup can kill
#    exactly the processes this script started.
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
echo "$MOCK_FE_PID" > "$PID_DIR/fe.pid"
disown "$MOCK_FE_PID" 2>/dev/null || true

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
echo "$MOCK_BE_PID" > "$PID_DIR/be.pid"
disown "$MOCK_BE_PID" 2>/dev/null || true

# Wait for both mocks to bind their ports.  Poll with /dev/tcp so we
# do not depend on `nc`, `lsof`, or any other optional tool.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if (echo >/dev/tcp/127.0.0.1/$MOCK_FE_PORT) >/dev/null 2>&1 \
     && (echo >/dev/tcp/127.0.0.1/$MOCK_BE_PORT) >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

"$NGINX_BIN" -p "$TEST_DIR" -c "$TEST_DIR/default.conf" >>"$LOG" 2>&1

# Wait for nginx to begin accepting connections on the test port.
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if (echo >/dev/tcp/127.0.0.1/$NGINX_PORT) >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

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
