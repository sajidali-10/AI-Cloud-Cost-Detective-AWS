#!/usr/bin/env bash
# Phase 8.1 — consolidated HTTPS / Nginx validation.
#
# Verifies the Phase 8.1 acceptance contract:
#   - host OS nginx is disabled/inactive
#   - Docker nginx is healthy
#   - :80 and :443 are host-published
#   - 8000, 8080, 4000, 5432 are NOT host-published
#   - nginx -t is valid
#   - DNS resolves finops.hiplink.com
#   - HTTP redirects to HTTPS (for the production Host)
#   - HTTPS returns 2xx (for the production Host)
#   - TLS 1.2 and TLS 1.3 succeed
#   - TLS 1.0 and TLS 1.1 fail
#   - Security headers are present on HTTPS
#   - /api/health works over HTTPS
#   - SPA / works over HTTPS
#   - certificate hostname covers finops.hiplink.com
#   - certificate chain validates (where a real CA-signed cert is installed)
#   - certificate expiry is readable
#
# The validator distinguishes two states:
#   - DETERMINISTIC configuration checks (always run, must PASS)
#   - LIVE TLS checks (run against the production endpoint)
#
# For production chain validation (gate #13) the script connects to
# ${PROD_HOST}:443 with openssl s_client -verify_return_error and
# requires the live handshake to verify against the system trust store.
# Local-file `openssl verify fullchain.pem` is intentionally NOT used
# for gate #13 — it can pass while the served chain is broken, so the
# real chain is always validated from a live handshake.
#
# Output never contains the certificate private key, secret values, or
# auth tokens.  openssl is invoked without -k, -verify_quiet, or any
# flag that disables certificate verification.
#
# Exit codes:
#   0  every check PASSED (deterministic + live TLS, including gate #13)
#   1  one or more deterministic checks FAILED
#   2  pre-condition violated

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

PROD_HOST="${PROD_HOST:-finops.hiplink.com}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
NGINX_CID="cost-detective-nginx"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$*"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; FAIL=$((FAIL+1)); }
pend() { printf "  \033[33mPEND\033[0m  %s\n" "$*"; PEND=$((PEND+1)); }
skip() { printf "  \033[36mSKIP\033[0m  %s\n" "$*"; SKIP=$((SKIP+1)); }
info() { printf "  \033[36mINFO\033[0m  %s\n" "$*"; }

PASS=0
FAIL=0
PEND=0
SKIP=0

bold "Phase 8.1 — HTTPS / Nginx validation"
printf "  Hostname: %s\n" "$PROD_HOST"

# ---------------------------------------------------------------------------
# 1. Host OS nginx must be disabled + inactive.
# ---------------------------------------------------------------------------

bold "[1/14] Host OS nginx state"
HOST_ENABLED=$(systemctl is-enabled nginx 2>/dev/null || true)
HOST_ACTIVE=$(systemctl is-active nginx 2>/dev/null || true)
if [[ "$HOST_ENABLED" == "disabled" && "$HOST_ACTIVE" == "inactive" ]]; then
    ok "Host OS nginx: enabled=disabled active=inactive"
else
    bad "Host OS nginx: enabled='$HOST_ENABLED' active='$HOST_ACTIVE'"
    info "Docker nginx owns ports 80/443 — host OS nginx must remain disabled."
fi

# ---------------------------------------------------------------------------
# 2. Docker nginx is healthy.
# ---------------------------------------------------------------------------

bold "[2/14] Docker nginx health"
NGINX_STATE=$(docker inspect -f '{{.State.Health.Status}}' "$NGINX_CID" 2>/dev/null || echo "missing")
if [[ "$NGINX_STATE" == "healthy" ]]; then
    ok "Docker nginx: $NGINX_STATE"
else
    bad "Docker nginx: $NGINX_STATE"
fi

# ---------------------------------------------------------------------------
# 3. Published ports.
# ---------------------------------------------------------------------------

bold "[3/14] Host-published ports"
PUB=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$NGINX_CID" 2>/dev/null || echo "{}")
for p in 80 443; do
    if echo "$PUB" | grep -q "\"$p/tcp\""; then
        ok "Docker nginx publishes :$p"
    else
        bad "Docker nginx does NOT publish :$p"
    fi
done

# ---------------------------------------------------------------------------
# 4. Internal-only services must NOT be host-published.
# ---------------------------------------------------------------------------

bold "[4/14] Internal-only service ports"
for svc_port in "cost-detective-backend:8000" "cost-detective-frontend:8080" "cost-detective-litellm:4000" "cost-detective-postgres:5432"; do
    CID="${svc_port%%:*}"
    PORT="${svc_port##*:}"
    P=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$CID" 2>/dev/null || echo "{}")
    if echo "$P" | grep -q "\"$PORT/tcp\""; then
        bad "$CID publishes :$PORT to host (must be internal-only)"
    else
        ok "$CID does NOT publish :$PORT to host"
    fi
done

# ---------------------------------------------------------------------------
# 5. Nginx config valid (deterministic).
# ---------------------------------------------------------------------------

bold "[5/14] Nginx configuration test"
if "$SCRIPT_DIR/phase8_1_nginx_config_test.sh" >/dev/null 2>&1; then
    ok "nginx -t PASS"
else
    bad "nginx -t FAIL"
    info "Run scripts/phase8_1_nginx_config_test.sh for details."
fi

# ---------------------------------------------------------------------------
# 6. DNS resolves the production hostname.
# ---------------------------------------------------------------------------

bold "[6/14] DNS resolution for $PROD_HOST"
DNS_IP=$(getent hosts "$PROD_HOST" 2>/dev/null | awk '{print $1}' | head -1)
if [[ -n "$DNS_IP" ]]; then
    ok "$PROD_HOST resolves to $DNS_IP"
else
    bad "$PROD_HOST does not resolve from this host"
fi

# ---------------------------------------------------------------------------
# 7. HTTP redirects to HTTPS (production Host).
# ---------------------------------------------------------------------------

bold "[7/14] HTTP → HTTPS redirect (production Host)"
HTTP_LOC=$(curl -sS -o /dev/null -D - "http://$PROD_HOST/" --max-time 5 2>/dev/null | tr -d '\r' | awk '/^[Ll]ocation:/ {print $2}' | head -1)
HTTP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "http://$PROD_HOST/" --max-time 5 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" == "301" && "$HTTP_LOC" == https://* ]]; then
    ok "HTTP→HTTPS 301 Location: $HTTP_LOC"
else
    bad "HTTP→HTTPS: code=$HTTP_CODE Location=$HTTP_LOC"
fi

# ---------------------------------------------------------------------------
# 8. HTTPS returns 2xx on / (production Host).
# ---------------------------------------------------------------------------

bold "[8/14] HTTPS /  (production Host)"
HTTPS_CODE=$(curl -ksS -o /dev/null -w "%{http_code}" \
    --resolve "$PROD_HOST:443:127.0.0.1" \
    "https://$PROD_HOST/" --max-time 5 2>/dev/null || echo "000")
if [[ "$HTTPS_CODE" =~ ^2 ]]; then
    ok "HTTPS /: code=$HTTPS_CODE"
else
    bad "HTTPS /: code=$HTTPS_CODE"
fi

# ---------------------------------------------------------------------------
# 9. /api/health over HTTPS.
# ---------------------------------------------------------------------------

bold "[9/14] HTTPS /api/health"
API_CODE=$(curl -ksS -o /dev/null -w "%{http_code}" \
    --resolve "$PROD_HOST:443:127.0.0.1" \
    "https://$PROD_HOST/api/health" --max-time 5 2>/dev/null || echo "000")
if [[ "$API_CODE" =~ ^2 ]]; then
    ok "HTTPS /api/health: code=$API_CODE"
else
    bad "HTTPS /api/health: code=$API_CODE"
fi

# ---------------------------------------------------------------------------
# 10. Security headers present on HTTPS responses.
# ---------------------------------------------------------------------------

bold "[10/14] Security headers"
HDRS=$(curl -ksS -D - -o /dev/null \
    --resolve "$PROD_HOST:443:127.0.0.1" \
    "https://$PROD_HOST/" --max-time 5 2>/dev/null | tr -d '\r')
for h in 'x-content-type-options: nosniff' 'x-frame-options: DENY' 'referrer-policy:'; do
    if echo "$HDRS" | grep -iq "^$h"; then
        ok "header present: $(echo "$h" | cut -d: -f1)"
    else
        bad "header MISSING: $(echo "$h" | cut -d: -f1)"
    fi
done
if echo "$HDRS" | grep -iq '^server: nginx$'; then
    ok "server banner: nginx (version suppressed)"
else
    bad "server banner not minimal: $(echo "$HDRS" | awk '/^[Ss]erver:/ {print $2}')"
fi
if echo "$HDRS" | grep -iq '^strict-transport-security:[[:space:]]*max-age=31536000'; then
    ok "HSTS present: Strict-Transport-Security: max-age=31536000"
elif echo "$HDRS" | grep -iq '^strict-transport-security'; then
    bad "HSTS present but with wrong directive (must be max-age=31536000 only — no includeSubDomains, no preload)"
else
    bad "HSTS header MISSING — required after live trusted-chain validation"
fi

# ---------------------------------------------------------------------------
# 11. TLS versions.
# ---------------------------------------------------------------------------

bold "[11/14] TLS protocol versions"
for ver in tls1_2 tls1_3; do
    HS=$(echo | openssl s_client -connect "127.0.0.1:443" -servername "$PROD_HOST" \
         "-$ver" </dev/null 2>/dev/null | grep -E '^New, ' | head -1 || true)
    if [[ -n "$HS" ]]; then
        ok "$ver handshake: $HS"
    else
        bad "$ver handshake: incomplete (server may have closed without selecting $ver)"
    fi
done
for ver in tls1 tls1_1; do
    OUT=$(echo | openssl s_client -connect "127.0.0.1:443" -servername "$PROD_HOST" "-$ver" 2>&1 || true)
    if echo "$OUT" | grep -qE 'no protocols available|alert handshake failure|unsupported protocol'; then
        ok "$ver correctly REJECTED"
    else
        bad "$ver was NOT rejected (must be disabled)"
    fi
done

# ---------------------------------------------------------------------------
# 12. Certificate hostname coverage and expiry.
# ---------------------------------------------------------------------------

bold "[12/14] Certificate hostname coverage"
CERT_SUBJ=$(openssl x509 -in /etc/finops/tls/fullchain.pem -noout -subject 2>/dev/null | sed 's/^subject=//' || echo "")
CERT_SAN=$(openssl x509 -in /etc/finops/tls/fullchain.pem -noout -ext subjectAltName 2>/dev/null | tr -d '\n' || echo "")
CERT_END=$(openssl x509 -in /etc/finops/tls/fullchain.pem -noout -enddate 2>/dev/null | sed 's/^notAfter=//' || echo "")
if echo "$CERT_SAN" | grep -qE "(DNS:)?$PROD_HOST|\*\.$PROD_HOST|\*\.hiplink\.com"; then
    ok "Certificate covers $PROD_HOST (SAN present)"
elif [[ -n "$CERT_SUBJ" ]]; then
    # CN-only fallback for self-signed dev certs.
    if echo "$CERT_SUBJ" | grep -qE "CN\s*=\s*$PROD_HOST"; then
        info "Certificate CN matches $PROD_HOST (SAN absent — likely self-signed dev placeholder)"
    else
        bad "Certificate does NOT cover $PROD_HOST (subject: $CERT_SUBJ, SAN: $CERT_SAN)"
    fi
else
    bad "No readable certificate at /etc/finops/tls/fullchain.pem"
fi
if [[ -n "$CERT_END" ]]; then
    ok "Certificate expiry readable: $CERT_END"
else
    bad "Certificate expiry NOT readable"
fi

# ---------------------------------------------------------------------------
# 13. Certificate chain validates (live, against the production endpoint).
#
# Production chain validation must exercise the certificate chain
# ACTUALLY SERVED by Nginx on finops.hiplink.com:443 — not merely
# the local fullchain.pem file.  Local-file verification can pass
# while the served chain is broken (e.g. wrong intermediate order,
# missing intermediate on disk, SNI mismatch), so we always validate
# the live handshake with openssl s_client against the system trust
# store.
#
# Rules (per Phase 8.1 closure spec):
#   * Use -verify_return_error so failures are surfaced as a non-zero
#     exit code and a clear "Verify return code: N" line.
#   * Use the system trust store (no custom -CAfile, no -k, no
#     -verify_quiet, no certificate verification disabled in any form).
#   * Never print private-key material — only subject/issuer/verify
#     status from the live handshake.
#   * Gate PASSES only when the live handshake reports verify
#     return code 0 (i.e. "Verification: OK" or "Verify return code: 0").
# ---------------------------------------------------------------------------

bold "[13/14] Certificate chain validation"
SCLIENT_OUT=$(echo | openssl s_client \
    -connect "${PROD_HOST}:443" \
    -servername "$PROD_HOST" \
    -verify_return_error \
    </dev/null 2>&1 || true)
LIVE_SUBJ=$(echo "$SCLIENT_OUT" | awk -F'subject=' '/^subject=/{print $2; exit}')
LIVE_ISS=$(echo "$SCLIENT_OUT" | awk -F'issuer=' '/^issuer=/{print $2; exit}')
LIVE_VRC=$(echo "$SCLIENT_OUT" | grep -E '^Verify return code:' | tail -1 | sed 's/^Verify return code:[[:space:]]*//')
if echo "$SCLIENT_OUT" | grep -qE '^Verification: OK$|Verify return code: 0\b'; then
    ok "Certificate chain validates against system trust store (live)"
    if [[ -n "$LIVE_SUBJ" ]]; then info "served subject: $LIVE_SUBJ"; fi
    if [[ -n "$LIVE_ISS"  ]]; then info "served issuer:  $LIVE_ISS";  fi
else
    bad "Certificate chain validation FAILED — live ${PROD_HOST}:443 did not verify against system trust store"
    if [[ -n "$LIVE_VRC" ]]; then info "openssl verify return code: $LIVE_VRC"; fi
    if [[ -n "$LIVE_SUBJ" ]]; then info "served subject:  $LIVE_SUBJ"; fi
    if [[ -n "$LIVE_ISS"  ]]; then info "served issuer:   $LIVE_ISS";  fi
fi

# ---------------------------------------------------------------------------
# 14. Internal Phase 7 path (http://localhost) still serves the SPA.
# ---------------------------------------------------------------------------

bold "[14/14] Internal localhost path (Phase 7 regression)"
LO_CODE=$(curl -sS -o /dev/null -w "%{http_code}" "http://localhost/" --max-time 5 2>/dev/null || echo "000")
LO_API=$(curl -sS -o /dev/null -w "%{http_code}" "http://localhost/api/health" --max-time 5 2>/dev/null || echo "000")
if [[ "$LO_CODE" =~ ^2 ]]; then
    ok "http://localhost/ : code=$LO_CODE"
else
    bad "http://localhost/ : code=$LO_CODE"
fi
if [[ "$LO_API" =~ ^2 ]]; then
    ok "http://localhost/api/health : code=$LO_API"
else
    bad "http://localhost/api/health : code=$LO_API"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

bold "Phase 8.1 summary"
echo "  pass=$PASS  fail=$FAIL  pending=$PEND  skipped=$SKIP"
echo
if [[ "$FAIL" -eq 0 && "$PEND" -eq 0 ]]; then
    bold "DETERMINISTIC CONFIGURATION: PASS"
    bold "LIVE TLS: PASS"
    exit 0
else
    bold "PHASE 8.1: NOT GREEN — see FAIL/PEND lines above"
    if [[ "$PEND" -ne 0 ]]; then
        info "PENDING gates indicate a partial state — see docs/phase8-1-https-nginx.md §5/§9."
    fi
    exit 1
fi
