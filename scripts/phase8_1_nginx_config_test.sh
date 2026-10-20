#!/usr/bin/env bash
# Phase 8.1 — deterministic Nginx configuration test.
#
# Runs `nginx -t` against the live configuration WITHOUT restarting
# the production nginx container.  Required by Phase 8.1 §15:
#
#   > Add/use deterministic config validation before restarting
#   > production Nginx.  Required:
#   >   docker compose run --rm --no-deps nginx nginx -t
#   > If this command cannot work before TLS files exist, create
#   > a safe deterministic equivalent and clearly explain why.
#
# Exit codes:
#   0  configuration is valid
#   1  configuration is invalid
#   2  pre-condition violated (compose missing / image absent)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mPASS\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31mFAIL\033[0m %s\n" "$*"; }
info() { printf "  \033[36mINFO\033[0m %s\n" "$*"; }

bold "Phase 8.1 — Nginx configuration test"

# ---------------------------------------------------------------------------
# Pre-conditions
# ---------------------------------------------------------------------------

if [[ ! -f docker-compose.yml ]]; then
    bad "docker-compose.yml not found in $(pwd)"
    exit 2
fi
if [[ ! -f nginx/default.conf ]]; then
    bad "nginx/default.conf not found"
    exit 2
fi
if [[ ! -d /etc/finops/tls ]]; then
    bad "/etc/finops/tls does not exist — TLS files have not been installed yet"
    info "See docs/phase8-1-https-nginx.md §5 for the install procedure."
    info "Until /etc/finops/tls/{fullchain.pem,privkey.pem} exist, nginx -t"
    info "will fail because ssl_certificate points at missing files."
    exit 1
fi
if [[ ! -s /etc/finops/tls/fullchain.pem || ! -s /etc/finops/tls/privkey.pem ]]; then
    bad "/etc/finops/tls/fullchain.pem or /etc/finops/tls/privkey.pem is missing or empty"
    info "Install the production *.hiplink.com wildcard at /etc/finops/tls/."
    info "See docs/phase8-1-https-nginx.md §5."
    exit 1
fi

ok "docker-compose.yml present"
ok "nginx/default.conf present"
ok "/etc/finops/tls/fullchain.pem and privkey.pem present"

# ---------------------------------------------------------------------------
# Validate via compose so the test container can resolve backend/frontend
# upstream hostnames on the internal Docker network.
# ---------------------------------------------------------------------------

OUT=$(docker compose run --rm --no-deps nginx nginx -t 2>&1)
RC=$?

if [[ $RC -eq 0 ]]; then
    ok "nginx -t: configuration valid"
    # Show non-fatal warnings (e.g. ssl_stapling when issuer cert absent).
    WARN=$(echo "$OUT" | grep -E '\[warn\]|\[emerg\]|\[crit\]' || true)
    if [[ -n "$WARN" ]]; then
        info "Warnings (non-fatal):"
        echo "$WARN" | sed 's/^/    /'
    fi
    exit 0
else
    bad "nginx -t: configuration INVALID"
    echo "$OUT" | sed 's/^/    /'
    exit 1
fi
