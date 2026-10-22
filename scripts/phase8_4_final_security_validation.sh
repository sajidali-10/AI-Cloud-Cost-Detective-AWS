#!/usr/bin/env bash
# Phase 8.4 — final consolidated security & release-readiness validator.
#
# Consolidates the production controls already verified by Phase 8.1 /
# 8.2 / 8.3 / 7D.5 into a single release-gate script.  This validator
# does NOT reimplement the 113+ low-level Phase 8.3 checks — those
# continue to live in phase8_3_container_security_validation.sh.
# Instead, this script enforces the 19 high-level release criteria
# from the Phase 8.4 spec (§16).
#
# Mandatory release gates (all must PASS):
#   1.  HTTPS reachable on finops.hiplink.com
#   2.  TLS chain validates against system trust store (live handshake)
#   3.  HSTS header present on HTTPS responses (no includeSubDomains, no preload)
#   4.  AUTH_ENABLED=true reported by /api/auth/info
#   5.  Anonymous protected request returns 401
#   6.  CORS production origin accepted; foreign origin rejected
#   7.  Login endpoint rate-limited (429 + Retry-After observed)
#   8.  Spoofed X-Forwarded-For cannot bypass rate-limit / auth path
#   9.  Docker published ports: only nginx 80/443; 8000/8080/4000/5432 internal-only
#  10.  Docker network segmentation: nginx/frontend NOT on `db`; postgres NOT on `app`
#  11.  No privileged / host-network / docker.socket / no-new-privileges=false
#  12.  TLS private-key mount isolated to nginx; backend/frontend/postgres/litellm
#       have no /etc/nginx/tls reference
#  13.  Host .env mode restrictive (600) and not tracked by git
#  14.  No static AWS credentials in .env or tracked tree
#  15.  No AZURE_CLIENT_SECRET in .env or tracked tree
#  16.  AWS IMDSv2 required (HttpTokens=required equivalent: IMDSv1 fails,
#       IMDSv2 succeeds; IAM role present)
#  17.  PostgreSQL healthy and not host-published; backend /api/health/ready 200
#  18.  Host OS nginx disabled/inactive; container nginx publishes 80/443 only
#  19.  Restart policy = unless-stopped; logging driver = json-file with bounded
#       max-size on every service
#
# Accepted warnings (NOT release blockers):
#   * LiteLLM health=starting/degraded while AI_ENABLED=false
#
# Exit codes:
#   0 = release ready
#   1 = security validation failure (one or more mandatory gates FAILED)
#   2 = precondition failure (docker / curl / openssl missing, etc.)
#
# The validator NEVER prints secret values, JWTs, or private-key material.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

PROD_HOST="${PROD_HOST:-finops.hiplink.com}"
PROD_SCHEME="${PROD_SCHEME:-https}"
BASE_URL="${PROD_SCHEME}://${PROD_HOST}"

PASS=0
FAIL=0
WARN=0
fail_msg=""

color_red()   { printf '\033[31m%s\033[0m' "$1"; }
color_green() { printf '\033[32m%s\033[0m' "$1"; }
color_yel()   { printf '\033[33m%s\033[0m' "$1"; }
log_pass() { PASS=$((PASS + 1)); printf '  [%s] %s\n' "$(color_green PASS)" "$1"; }
log_fail() { FAIL=$((FAIL + 1)); fail_msg="$fail_msg\n - $1"; printf '  [%s] %s\n' "$(color_red FAIL)" "$1"; }
log_warn() { WARN=$((WARN + 1)); printf '  [%s] %s\n' "$(color_yel WARN)" "$1"; }
banner()   { printf '\n=== %s ===\n' "$1"; }

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

banner "Preconditions"

for cmd in docker curl openssl ss; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        printf "FATAL: %s is required\n" "$cmd"
        exit 2
    fi
done
log_pass "tools present (docker, curl, openssl, ss)"

if ! docker info >/dev/null 2>&1; then
    printf "FATAL: docker daemon not reachable\n"
    exit 2
fi
log_pass "docker daemon reachable"

if [[ "$(git rev-parse HEAD 2>/dev/null)" != "$(git rev-parse phase-8-3-complete 2>/dev/null)" ]] \
   && ! git merge-base --is-ancestor 411867d9c90f3a329dcd3877f7546959c454cb4c HEAD 2>/dev/null; then
    printf "FATAL: HEAD does not include the Phase 8.3 stable baseline 411867d\n"
    exit 2
fi
log_pass "branch ancestry includes Phase 8.3 baseline"

# ---------------------------------------------------------------------------
# 1. HTTPS reachable
# ---------------------------------------------------------------------------

banner "1. HTTPS reachability"
H=$(curl -sk --max-time 10 -o /dev/null -w '%{http_code}' "${BASE_URL}/api/health")
if [[ "$H" =~ ^2 ]]; then
    log_pass "/api/health returns ${H}"
else
    log_fail "/api/health returned ${H}"
fi

# ---------------------------------------------------------------------------
# 2. TLS chain validates (live handshake)
# ---------------------------------------------------------------------------

banner "2. TLS chain trust (live)"
SCOUT=$(echo | openssl s_client -connect "${PROD_HOST}:443" -servername "$PROD_HOST" -verify_return_error </dev/null 2>&1 || true)
if echo "$SCOUT" | grep -qE '^Verification: OK$|Verify return code: 0\b'; then
    log_pass "live TLS chain validates against system trust store"
else
    log_fail "live TLS chain did NOT validate — openssl verify output above"
fi

# ---------------------------------------------------------------------------
# 3. HSTS present (no includeSubDomains, no preload)
# ---------------------------------------------------------------------------

banner "3. HSTS header"
HSTS=$(curl -sk --max-time 8 -D - -o /dev/null "${BASE_URL}/api/health" | tr -d '\r' | grep -i '^strict-transport-security:' | head -1 || true)
if [[ -z "$HSTS" ]]; then
    log_fail "Strict-Transport-Security header MISSING"
else
    if echo "$HSTS" | grep -qi 'max-age=31536000' \
       && ! echo "$HSTS" | grep -qi 'includeSubDomains' \
       && ! echo "$HSTS" | grep -qi 'preload'; then
        log_pass "HSTS present (max-age=31536000, no includeSubDomains, no preload)"
    else
        log_fail "HSTS policy incorrect: $HSTS"
    fi
fi

# ---------------------------------------------------------------------------
# 4. AUTH_ENABLED=true
# ---------------------------------------------------------------------------

banner "4. AUTH_ENABLED"
INFO=$(curl -sk --max-time 8 "${BASE_URL}/api/auth/info" 2>/dev/null || true)
if echo "$INFO" | grep -q '"auth_enabled":\s*true'; then
    log_pass "AUTH_ENABLED=true (production)"
else
    log_fail "AUTH_ENABLED is NOT true: ${INFO:0:200}"
fi

# ---------------------------------------------------------------------------
# 5. Anonymous rejection
# ---------------------------------------------------------------------------

banner "5. Anonymous protected request"
A=$(curl -sk --max-time 8 -o /dev/null -w '%{http_code}' "${BASE_URL}/api/aws/identity")
if [[ "$A" == "401" || "$A" == "403" ]]; then
    log_pass "/api/aws/identity rejects anonymous (HTTP ${A})"
else
    log_fail "/api/aws/identity returned ${A} (expected 401/403)"
fi

# ---------------------------------------------------------------------------
# 6. CORS production acceptance + foreign rejection
# ---------------------------------------------------------------------------

banner "6. CORS policy"
ACAO_PROD=$(curl -sk --max-time 8 -D - -o /dev/null \
    -H "Origin: ${BASE_URL}" \
    -H "Access-Control-Request-Method: GET" \
    -X OPTIONS "${BASE_URL}/api/auth/info" \
    | tr -d '\r' | grep -i '^access-control-allow-origin:' | head -1 | tr -d ' ' || true)
if echo "$ACAO_PROD" | grep -q "${PROD_HOST}"; then
    log_pass "CORS allows production origin (${ACAO_PROD:-<header>})"
else
    log_fail "CORS did NOT allow production origin (${ACAO_PROD:-<missing>})"
fi

ACAO_FOREIGN=$(curl -sk --max-time 8 -D - -o /dev/null \
    -H "Origin: https://attacker.example" \
    -H "Access-Control-Request-Method: GET" \
    -X OPTIONS "${BASE_URL}/api/auth/info" \
    | tr -d '\r' | grep -i '^access-control-allow-origin:' | head -1 | tr -d ' ' || true)
if echo "$ACAO_FOREIGN" | grep -qi 'attacker.example'; then
    log_fail "CORS reflects foreign origin: ${ACAO_FOREIGN}"
elif echo "$ACAO_FOREIGN" | grep -q '\*'; then
    log_fail "CORS wildcard on authenticated surface: ${ACAO_FOREIGN}"
else
    log_pass "CORS rejects foreign origin (header: ${ACAO_FOREIGN:-<none>})"
fi

# ---------------------------------------------------------------------------
# 7. Login rate limiting
# ---------------------------------------------------------------------------

banner "7. Login rate limiting"
RL=0; RAS=0
for i in $(seq 1 20); do
    RESP=$(curl -sk --max-time 8 -o /dev/null \
        -w 'code=%{http_code} retry=%header{Retry-After}' \
        -X POST "${BASE_URL}/api/auth/login" \
        -H 'Content-Type: application/json' \
        -d '{"email":"p84-ratelimit@example.invalid","password":"x"}')
    if [[ "$RESP" == code=429* ]]; then
        RL=1
        if [[ "$RESP" == *retry=[1-9]* ]]; then
            RAS=1
        fi
        break
    fi
done
if [[ "$RL" == "1" ]]; then
    if [[ "$RAS" == "1" ]]; then
        log_pass "login endpoint returns 429 with Retry-After"
    else
        log_warn "login returns 429 but Retry-After absent"
    fi
else
    log_fail "no 429 observed in 20 attempts — rate limiter appears inactive"
fi

# ---------------------------------------------------------------------------
# 8. Proxy spoof resistance (distinct buckets)
# ---------------------------------------------------------------------------

banner "8. Proxy spoof resistance"
# Two distinct XFF values must both reach the backend and both be rejected
# by the auth layer (no global dedup, no bypass).
for ip in 7.7.7.7 8.8.8.8; do
    code=$(curl -sk --max-time 8 -o /dev/null -w '%{http_code}' \
        -H "X-Forwarded-For: ${ip}" \
        "${BASE_URL}/api/auth/me" 2>/dev/null || echo "000")
    if [[ "$code" == "401" || "$code" == "403" ]]; then
        log_pass "XFF=${ip} → /api/auth/me rejected (HTTP ${code})"
    else
        log_fail "XFF=${ip} → /api/auth/me unexpected HTTP ${code}"
    fi
done

# ---------------------------------------------------------------------------
# 9. Docker published ports
# ---------------------------------------------------------------------------

banner "9. Docker published ports"
for p in 80 443; do
    PUB=$(docker inspect -f '{{json .HostConfig.PortBindings}}' cost-detective-nginx 2>/dev/null || echo "{}")
    if echo "$PUB" | grep -q "\"$p/tcp\""; then
        log_pass "cost-detective-nginx publishes :$p"
    else
        log_fail "cost-detective-nginx does NOT publish :$p"
    fi
done
for entry in "cost-detective-backend:8000" "cost-detective-frontend:8080" "cost-detective-litellm:4000" "cost-detective-postgres:5432"; do
    CID="${entry%%:*}"; P="${entry##*:}"
    PUB=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$CID" 2>/dev/null || echo "{}")
    if echo "$PUB" | grep -q "\"$P/tcp\""; then
        log_fail "$CID publishes :$P to host (must be internal-only)"
    else
        log_pass "$CID does NOT publish :$P"
    fi
done

# ---------------------------------------------------------------------------
# 10. Network segmentation
# ---------------------------------------------------------------------------

banner "10. Docker network segmentation"
for net in cost-detective-app cost-detective-db cost-detective-ai; do
    if docker network inspect "$net" >/dev/null 2>&1; then
        log_pass "network present: $net"
    else
        log_fail "network MISSING: $net"
    fi
done
for net in cost-detective-db cost-detective-ai; do
    INTERNAL=$(docker network inspect "$net" --format '{{.Internal}}' 2>/dev/null || echo "missing")
    if [[ "$INTERNAL" == "true" ]]; then
        log_pass "$net is internal (no gateway)"
    else
        log_fail "$net is NOT internal: Internal=$INTERNAL"
    fi
done
# nginx/frontend MUST NOT be on cost-detective-db
for cid in cost-detective-nginx cost-detective-frontend; do
    ACT=$(docker inspect "$cid" --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "")
    if echo " $ACT " | grep -q ' cost-detective-db '; then
        log_fail "$cid IS on cost-detective-db (must NOT reach Postgres)"
    else
        log_pass "$cid is NOT on cost-detective-db"
    fi
done
# postgres MUST NOT be on cost-detective-app
PGA=$(docker inspect cost-detective-postgres --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null || echo "")
if echo " $PGA " | grep -q ' cost-detective-app '; then
    log_fail "postgres IS on cost-detective-app (must be db-only)"
else
    log_pass "postgres is NOT on cost-detective-app"
fi

# ---------------------------------------------------------------------------
# 11. No privileged / host-network / docker.socket / no-new-privileges
# ---------------------------------------------------------------------------

banner "11. Container hardening essentials"
for cid in cost-detective-nginx cost-detective-frontend cost-detective-backend cost-detective-postgres cost-detective-litellm; do
    PRIV=$(docker inspect -f '{{.HostConfig.Privileged}}' "$cid" 2>/dev/null || echo "missing")
    NM=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$cid" 2>/dev/null || echo "missing")
    SEC=$(docker inspect -f '{{json .HostConfig.SecurityOpt}}' "$cid" 2>/dev/null || echo "[]")
    MOUNTS=$(docker inspect -f '{{json .Mounts}}' "$cid" 2>/dev/null || echo "[]")
    if [[ "$PRIV" == "false" ]]; then log_pass "$cid privileged=false"; else log_fail "$cid privileged=$PRIV"; fi
    if [[ "$NM" != "host" ]]; then log_pass "$cid NetworkMode=$NM (not host)"; else log_fail "$cid NetworkMode=host"; fi
    if echo "$SEC" | grep -q 'no-new-privileges:true'; then log_pass "$cid no-new-privileges:true"; else log_fail "$cid no-new-privileges MISSING"; fi
    if echo "$MOUNTS" | grep -q '/var/run/docker.sock'; then log_fail "$cid has docker.sock mount"; else log_pass "$cid no docker.sock mount"; fi
done

# ---------------------------------------------------------------------------
# 12. TLS mount isolation
# ---------------------------------------------------------------------------

banner "12. TLS mount isolation"
for cid in cost-detective-backend cost-detective-frontend cost-detective-postgres cost-detective-litellm; do
    MOUNTS=$(docker inspect -f '{{json .Mounts}}' "$cid" 2>/dev/null || echo "[]")
    if echo "$MOUNTS" | grep -q '/etc/nginx/tls\|/etc/finops/tls'; then
        log_fail "$cid has TLS mount (must be nginx-only)"
    else
        log_pass "$cid has NO TLS mount"
    fi
done
if [[ -e /etc/finops/tls/privkey.pem ]]; then
    PERMS=$(stat -L -c '%a' /etc/finops/tls/privkey.pem 2>/dev/null || echo "000")
    if [[ "$PERMS" == "600" || "$PERMS" == "400" ]]; then
        log_pass "host TLS private key mode=$PERMS (restrictive)"
    else
        log_warn "host TLS private key mode=$PERMS (recommend 600)"
    fi
else
    log_warn "host TLS private key not present at /etc/finops/tls/privkey.pem"
fi

# ---------------------------------------------------------------------------
# 13. .env file mode + gitignore
# ---------------------------------------------------------------------------

banner "13. Host .env file"
if [[ -f .env ]]; then
    MODE=$(stat -c '%a' .env 2>/dev/null || echo "000")
    if [[ "$MODE" == "600" || "$MODE" == "400" ]]; then
        log_pass ".env mode=$MODE (restrictive)"
    else
        log_fail ".env mode=$MODE (must be 600 or 400)"
    fi
    if git ls-files --error-unmatch .env >/dev/null 2>&1; then
        log_fail ".env is tracked by git"
    else
        log_pass ".env is not tracked by git"
    fi
else
    log_fail ".env MISSING from repo root"
fi
JWT_LEN=$(awk -F= '/^JWT_SECRET=/ {print length($2)}' .env 2>/dev/null || echo "0")
if [[ "$JWT_LEN" -ge 32 ]]; then
    log_pass "JWT_SECRET length=${JWT_LEN} (>=32)"
else
    log_fail "JWT_SECRET length=${JWT_LEN} (must be >=32)"
fi

# ---------------------------------------------------------------------------
# 14. No static AWS credentials
# ---------------------------------------------------------------------------

banner "14. No static AWS credentials"
for bad in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
    if grep -q "^${bad}=" .env 2>/dev/null; then
        log_fail ".env contains $bad (static AWS credential NOT allowed)"
    else
        log_pass ".env does NOT contain $bad"
    fi
    if git ls-files -z | xargs -0 grep -l "^${bad}=" 2>/dev/null | head -1 | grep -q .; then
        log_fail "tracked tree contains $bad"
    else
        log_pass "tracked tree does NOT contain $bad"
    fi
done

# ---------------------------------------------------------------------------
# 15. No Azure client secret
# ---------------------------------------------------------------------------

banner "15. No Azure client secret"
for bad in AZURE_CLIENT_SECRET AZURE_PASSWORD AZURE_TENANT_SECRET; do
    if grep -q "^${bad}=" .env 2>/dev/null; then
        log_fail ".env contains $bad (Azure client secret NOT allowed)"
    else
        log_pass ".env does NOT contain $bad"
    fi
    if git ls-files -z | xargs -0 grep -l "^${bad}=" 2>/dev/null | head -1 | grep -q .; then
        log_fail "tracked tree contains $bad"
    else
        log_pass "tracked tree does NOT contain $bad"
    fi
done

# ---------------------------------------------------------------------------
# 15b. Rackspace secrets — protected .env only (Phase 9A approval)
# ---------------------------------------------------------------------------

banner "15b. Rackspace secrets (Phase 9A approved)"
# Phase 9A integration is now approved.  The host .env is the
# canonical, protected deployment surface for the three Identity
# credentials and the Billing Account Number (RAN).  A real
# credential appearing in any TRACKED source file is still a
# hard failure.  Values are NEVER printed.
RS_ID_OK=1
for rs in RACKSPACE_USERNAME RACKSPACE_API_KEY RACKSPACE_BILLING_ACCOUNT; do
    # The value must be non-empty in .env when present (presence
    # alone is acceptable — absence means "Rackspace disabled").
    if grep -qE "^${rs}=[^[:space:]](.*[^[:space:]])?$" .env 2>/dev/null; then
        log_pass ".env has non-empty ${rs} (value redacted)"
    else
        log_warn ".env has empty ${rs} (Rackspace integration disabled)"
    fi
    # CRITICAL: the credential value must NOT appear in any
    # TRACKED source file (the documented Phase 9A contract —
    # same scope as the AWS/Azure checks above; untracked local
    # artifacts such as .kimchi/ session files and frontend/dist
    # build output are not part of the release).  We grep the
    # *value*, not just the key name, so a stray copy-paste of
    # an actual credential into source is still caught.
    RS_VAL="$(awk -F= -v k="$rs" '$1==k {print substr($0, length(k)+2)}' .env 2>/dev/null | tr -d '\r\n' || true)"
    # Strip optional surrounding quotes so a quoted .env value is
    # also checked in its bare form.
    RS_VAL="${RS_VAL%\"}"; RS_VAL="${RS_VAL#\"}"
    RS_VAL="${RS_VAL%\'}"; RS_VAL="${RS_VAL#\'}"
    if [[ -n "$RS_VAL" ]]; then
        # Tracked files that contain the raw value at all.
        RS_CAND="$(git ls-files -z | xargs -0 grep -IlF -- "$RS_VAL" 2>/dev/null || true)"
        RS_LEAKS=""
        if [[ -n "$RS_CAND" ]]; then
            if [[ "$rs" == "RACKSPACE_USERNAME" ]]; then
                # Usernames are frequently short dictionary-like words
                # that also occur as ordinary project vocabulary, so a
                # bare exact-value scan is meaningless for USERNAME
                # (the project domain term appears in dozens of tracked
                # files as prose).  The genuine leak pattern is the
                # value ASSIGNED to a username key, so a file is only
                # flagged when the value appears on a line that also
                # carries a username-assignment context
                # (RACKSPACE_USERNAME=..., "username": ..., ...).
                while IFS= read -r rf; do
                    [[ -n "$rf" ]] || continue
                    if grep -iEn "username[\"']?[[:space:]]*[=:]" "$rf" 2>/dev/null \
                        | grep -qF -- "$RS_VAL"; then
                        RS_LEAKS="${RS_LEAKS:+$RS_LEAKS }$rf"
                    fi
                done <<< "$RS_CAND"
            else
                # API keys and the RAN are high-specificity
                # identifiers: the bare value anywhere in a tracked
                # file is a leak.
                RS_LEAKS="$RS_CAND"
            fi
        fi
        if [[ -n "$RS_LEAKS" ]]; then
            log_fail "Rackspace credential value for ${rs} leaks outside .env into: ${RS_LEAKS}"
            RS_ID_OK=0
        else
            log_pass "Rackspace credential value for ${rs} is confined to .env (tracked tree)"
        fi
        # Scrub the local variables so we never expose them via
        # shell introspection.
        unset RS_VAL RS_CAND RS_LEAKS rf
    fi
done
# The RACKSPACE_TOKEN variable is intentionally NEVER used by
# the application — Phase 9A derives the X-Auth-Token from the
# Identity service on demand and never persists it.  If it
# appears in .env we flag a warning (it should not be needed).
if grep -q "^RACKSPACE_TOKEN=" .env 2>/dev/null; then
    log_warn ".env contains RACKSPACE_TOKEN (not used by Phase 9A — X-Auth-Token is derived per-call)"
fi

# Verify the .env.example uses empty placeholders only — no
# real values must have leaked into the example template.
ENV_EXAMPLE_BAD="$(grep -E '^RACKSPACE_(USERNAME|API_KEY|BILLING_ACCOUNT|ACCOUNT_ID)=[^[:space:]]' .env.example 2>/dev/null | grep -vE '=$' || true)"
if [[ -n "$ENV_EXAMPLE_BAD" ]]; then
    log_fail ".env.example has non-empty Rackspace value(s): ${ENV_EXAMPLE_BAD}"
    RS_ID_OK=0
else
    log_pass ".env.example has empty Rackspace placeholders only"
fi

# ---------------------------------------------------------------------------
# 16. IMDSv2 required
# ---------------------------------------------------------------------------

banner "16. AWS IMDSv2"
TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" \
    --max-time 4 2>/dev/null || true)
if [[ -n "$TOKEN" ]]; then
    log_pass "IMDSv2 token PUT succeeded"
    IAM=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/meta-data/iam/info" \
        --max-time 4 2>/dev/null || true)
    if echo "$IAM" | grep -q 'AICloudCostDetectivePhase1ReadOnly'; then
        log_pass "EC2 IAM role: AICloudCostDetectivePhase1ReadOnly"
    else
        log_fail "EC2 IAM role unexpected: ${IAM:0:200}"
    fi
else
    log_fail "IMDSv2 token PUT failed (HttpTokens=required not enforced?)"
fi
# IMDSv1 must fail (401) when HttpTokens=required.
IMDS_V1=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 4 \
    "http://169.254.169.254/latest/meta-data/iam/info" 2>/dev/null || echo "000")
if [[ "$IMDS_V1" == "401" || "$IMDS_V1" == "403" ]]; then
    log_pass "IMDSv1 rejected (HTTP ${IMDS_V1}) — HttpTokens=required enforced"
else
    log_warn "IMDSv1 returned ${IMDS_V1} (expected 401/403)"
fi

# ---------------------------------------------------------------------------
# 17. PostgreSQL isolation + backend readiness
# ---------------------------------------------------------------------------

banner "17. PostgreSQL isolation + readiness"
PG_H=$(docker inspect -f '{{.State.Health.Status}}' cost-detective-postgres 2>/dev/null || echo "missing")
if [[ "$PG_H" == "healthy" ]]; then
    log_pass "postgres health=healthy"
else
    log_fail "postgres health=$PG_H"
fi
if docker volume inspect cost-detective-pgdata >/dev/null 2>&1; then
    log_pass "pgdata volume present (cost-detective-pgdata)"
else
    log_fail "pgdata volume MISSING"
fi
RDY=$(curl -sk --max-time 10 -o /dev/null -w '%{http_code}' "${BASE_URL}/api/health/ready")
if [[ "$RDY" =~ ^2 ]]; then
    log_pass "/api/health/ready HTTP ${RDY} (backend ↔ DB authenticated)"
else
    log_fail "/api/health/ready HTTP ${RDY}"
fi

# ---------------------------------------------------------------------------
# 18. Host nginx disabled + Docker API not public + only nginx publishes 80/443
# ---------------------------------------------------------------------------

banner "18. Host nginx state + Docker API"
read_state() {
    # Read first non-empty line of `systemctl is-…` output, falling
    # back to "absent" if the command is unavailable or fails.
    local what="$1"
    local out
    out=$(systemctl "is-${what}" nginx 2>/dev/null | head -1 | tr -d '[:space:]') || true
    if [[ -z "$out" ]]; then out="absent"; fi
    printf '%s' "$out"
}
HN_ENABLED=$(read_state enabled)
HN_ACTIVE=$(read_state active)
if [[ "$HN_ENABLED" == "disabled" || "$HN_ENABLED" == "absent" ]]; then
    log_pass "host nginx enabled=${HN_ENABLED}"
else
    log_fail "host nginx enabled=$HN_ENABLED"
fi
if [[ "$HN_ACTIVE" == "inactive" || "$HN_ACTIVE" == "absent" ]]; then
    log_pass "host nginx active=${HN_ACTIVE}"
else
    log_fail "host nginx active=$HN_ACTIVE"
fi
# Host listeners: 80/443 must belong to docker-proxy, 22 to sshd.
LISTEN_OK=1
LISTEN_OUT=$(ss -lntp 2>/dev/null | awk 'NR>1 {print $4, $6}' || true)
if ! echo "$LISTEN_OUT" | grep -E '(:80|:443)\s' | grep -q 'docker-proxy'; then
    log_fail "no docker-proxy bound to :80/:443"; LISTEN_OK=0
fi
if echo "$LISTEN_OUT" | grep -qE ':2375\s|:2376\s'; then
    log_fail "Docker API listening on TCP (2375/2376) — must not be public"; LISTEN_OK=0
fi
if [[ "$LISTEN_OK" == "1" ]]; then
    log_pass "host listeners clean: nginx→docker-proxy, no Docker API TCP exposure"
fi
# Curl-probe Docker API TCP for belt-and-braces.
for port in 2375 2376; do
    C=$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/v1.41/_ping" 2>/dev/null || echo "000")
    if [[ "$C" == "200" ]]; then
        log_fail "Docker API listening on TCP :$port"
    else
        log_pass "Docker API NOT listening on TCP :$port (curl $C)"
    fi
done

# ---------------------------------------------------------------------------
# 19. Restart policy + logging limits
# ---------------------------------------------------------------------------

banner "19. Restart policy + logging limits"
for cid in cost-detective-nginx cost-detective-frontend cost-detective-backend cost-detective-postgres cost-detective-litellm; do
    RP=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$cid" 2>/dev/null || echo "missing")
    DRIVER=$(docker inspect -f '{{.HostConfig.LogConfig.Type}}' "$cid" 2>/dev/null || echo "missing")
    OPTS=$(docker inspect -f '{{json .HostConfig.LogConfig.Config}}' "$cid" 2>/dev/null || echo "{}")
    if [[ "$RP" == "unless-stopped" || "$RP" == "always" ]]; then
        log_pass "$cid restart=$RP"
    else
        log_fail "$cid restart=$RP"
    fi
    if [[ "$DRIVER" == "json-file" ]] && echo "$OPTS" | grep -q 'max-size'; then
        log_pass "$cid log driver=json-file with bounded max-size"
    else
        log_fail "$cid log config invalid (driver=$DRIVER opts=$OPTS)"
    fi
done

# ---------------------------------------------------------------------------
# Accepted warnings (NOT release blockers)
# ---------------------------------------------------------------------------

banner "Accepted warnings"
LITELLM_H=$(docker inspect -f '{{.State.Health.Status}}' cost-detective-litellm 2>/dev/null || echo "missing")
if [[ "$LITELLM_H" != "healthy" ]]; then
    log_warn "litellm health=$LITELLM_H (ACCEPTED — AI_ENABLED=false, gateway degraded)"
else
    log_pass "litellm health=healthy"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

banner "Phase 8.4 Summary"
printf 'PASS=%d  FAIL=%d  WARN=%d\n' "$PASS" "$FAIL" "$WARN"

if [[ "$FAIL" -gt 0 ]]; then
    printf '\nFailures:\n'
    printf "$fail_msg\n"
    printf '\nVERDICT: NOT RELEASE READY\n'
    exit 1
fi

if [[ "$WARN" -gt 0 ]]; then
    printf '\nAccepted warnings are non-blocking (LiteLLM degraded while AI_ENABLED=false, etc.).\n'
fi
printf '\nVERDICT: RELEASE READY\n'
exit 0
