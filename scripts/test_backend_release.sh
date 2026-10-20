#!/usr/bin/env bash
# Phase 7D.5 — canonical backend release test wrapper.
#
# Runs the full backend pytest suite inside the backend container
# so the test runner has access to the docker-compose network
# (postgres, litellm reachable as their service names).
#
# We deliberately do NOT hardcode ``POSTGRES_HOST=postgres`` in
# the conftest: tests/conftest.py respects $POSTGRES_HOST first and
# only falls back when unset + unresolvable.  This wrapper sets
# POSTGRES_HOST=postgres only when invoking the docker exec path;
# direct host pytest invocations continue to use whatever the host
# environment provides.
#
# Exit codes:
#   0 — every test passed.
#   1 — one or more tests failed.
#   2 — pre-condition violated (backend container not running).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

bold() { printf "\033[1m%s\033[0m\n" "$*"; }

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^cost-detective-backend$'; then
    bold "[test_backend_release] running pytest inside backend container (docker-compose network)"
    # Inside the backend container, the postgres service is
    # reachable as ``postgres`` (the docker-compose service name).
    # We set the env vars ONLY for this invocation; the conftest
    # reads them via os.environ.setdefault so existing values win.
    docker exec \
        -e POSTGRES_HOST=postgres \
        -e POSTGRES_PORT=5432 \
        -e LITELLM_HOST=litellm \
        -e LITELLM_PORT=4000 \
        cost-detective-backend python -m pytest -q
    RC=$?
elif command -v pytest >/dev/null 2>&1; then
    bold "[test_backend_release] backend container not present, running local pytest (uses host env)"
    cd backend && python -m pytest -q
    RC=$?
else
    echo "[test_backend_release] FAIL: backend container not running and no local pytest found" >&2
    exit 2
fi

if [[ "$RC" -eq 0 ]]; then
    bold "[test_backend_release] RESULT: 0 failures — backend release suite GREEN"
else
    bold "[test_backend_release] RESULT: failures detected (pytest exit $RC)"
fi
exit "$RC"
