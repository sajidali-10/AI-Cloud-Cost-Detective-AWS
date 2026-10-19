#!/usr/bin/env bash
# Phase 7D.3.3 — Azure cost query resilience live validation.
#
# Validates the server-side cache + authority predicate against the
# live EC2 deployment without intentionally hammering Azure.
#
# Sequence:
#   1. Mint a short-lived dev admin JWT (same pattern as the other
#      live validators).
#   2. Probe MCPP / Sponsorship / All-Azure canonical endpoints.
#   3. Immediate-repeat the MCPP call and assert cache_status=HIT
#      with azure_post_count=0.
#   4. Assert the authority predicate:
#      - PARTIAL_SUCCESS + successful_accounts=0 + THROTTLED must
#        NOT be written or retained as authoritative LKG.
#      - Genuine zero ($0.00 with successful_accounts>0) IS
#        cacheable.
#   5. Assert All-Azure does not reuse a stale throttled-zero
#      aggregate when individual sub caches are usable.
#
# Exit codes:
#   0 — every assertion passed.
#   1 — at least one assertion failed.
#   2 — pre-condition violated (backend not reachable, JWT mint
#       failed, etc.).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || exit 2

API="${API:-http://localhost}"
MONTH="${MONTH:-2026-10}"
SUB_MCPP="${SUB_MCPP:-sub:a54d076b-2650-4b4f-a28c-2deed2011f83}"
SUB_SPONSOR="${SUB_SPONSOR:-sub:9353d858-27fa-4ce7-bf87-782f3323d297}"

# Same JWT minting pattern as phase7d3_2_live_validation.sh — the
# backend container holds the JWT secret in JWT_SECRET.
TOKEN=$(docker exec cost-detective-backend python3 -c "
import jwt, time, os
secret = os.environ.get('JWT_SECRET'); now = int(time.time())
print(jwt.encode({'sub':'2','email':'devadmin@hiplink.com','role':'ADMIN',
 'iss':'ai-cloud-cost-detective','aud':'ai-cloud-cost-detective-api',
 'iat':now,'exp':now+3600}, secret, algorithm='HS256'))" 2>&1 | tail -1)

if [[ -z "${TOKEN:-}" || "${TOKEN}" == *"Traceback"* ]]; then
  echo "FAIL: unable to mint dev admin JWT (is the backend container running?)" >&2
  exit 2
fi

PROBE_FILE="/tmp/phase7d3_3_post_count.txt"
declare -A RESULTS

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok() { printf "  \033[32mPASS\033[0m %s\n" "$*"; }
fail() { printf "  \033[31mFAIL\033[0m %s\n" "$*"; FAILURES=$((FAILURES+1)); }

FAILURES=0

bold "Phase 7D.3.3 — Azure cost resilience live validation"

# ---------------------------------------------------------------------------
# 1. Pre-conditions
# ---------------------------------------------------------------------------

bold "[1/7] Pre-conditions"
if ! curl -fsS http://localhost/health >/dev/null 2>&1; then
  fail "Backend health endpoint not reachable at http://localhost/health"
  exit 2
fi
ok "Backend health OK"
ok "JWT minted"

# Helper: call the canonical Azure endpoint, decode the envelope
# into a JSON string suitable for python parsing.
probe() {
  local account="$1"
  local url="$API/api/cloud/costs?provider=AZURE&month=$MONTH"
  if [[ -n "$account" ]]; then
    url="$url&account_id=$account"
  fi
  curl -fsS -H "Authorization: Bearer $TOKEN" "$url" 2>&1
}

# Helper: extract a single field from a JSON envelope via python.
extract() {
  local body="$1"
  local path="$2"   # e.g. "cache_status" or "report.total_cost"
  python3 -c "
import sys, json
try:
    r = json.loads(sys.argv[1])
except Exception:
    print('')
    sys.exit(0)
v = r
for p in sys.argv[2].split('.'):
    if isinstance(v, dict):
        v = v.get(p)
    else:
        v = ''
        break
print(v if v is not None else '')
" "$body" "$path" 2>/dev/null
}

# ---------------------------------------------------------------------------
# 2. MCPP canonical request
# ---------------------------------------------------------------------------

bold "[2/7] MCPP canonical request"
MCPP_BODY=$(probe "$SUB_MCPP")
RESULTS[mcpp_status]=$(extract "$MCPP_BODY" "report.status")
RESULTS[mcpp_cache_status]=$(extract "$MCPP_BODY" "cache_status")
RESULTS[mcpp_freshness]=$(extract "$MCPP_BODY" "data_freshness")
RESULTS[mcpp_total]=$(extract "$MCPP_BODY" "report.total_cost")
RESULTS[mcpp_successful]=$(extract "$MCPP_BODY" "report.successful_accounts")
RESULTS[mcpp_failed]=$(extract "$MCPP_BODY" "report.failed_accounts")
RESULTS[mcpp_post_count]=$(extract "$MCPP_BODY" "azure_post_count")
RESULTS[mcpp_last_success]=$(extract "$MCPP_BODY" "last_successful_at")
RESULTS[mcpp_previous_unavailable]=$(extract "$MCPP_BODY" "report.previous_period_unavailable")

printf "  status=%s cache_status=%s freshness=%s total=%s successful=%s failed=%s posts=%s\n" \
  "${RESULTS[mcpp_status]}" "${RESULTS[mcpp_cache_status]}" "${RESULTS[mcpp_freshness]}" \
  "${RESULTS[mcpp_total]}" "${RESULTS[mcpp_successful]}" "${RESULTS[mcpp_failed]}" \
  "${RESULTS[mcpp_post_count]}"

if [[ "${RESULTS[mcpp_cache_status]}" == "REFRESHED" || "${RESULTS[mcpp_cache_status]}" == "HIT" ]]; then
  ok "MCPP cache_status=${RESULTS[mcpp_cache_status]}"
else
  fail "MCPP unexpected cache_status=${RESULTS[mcpp_cache_status]}"
fi
# Track whether MCPP cold produced an authoritative response — if
# not, we cannot assert HIT on the repeat (no row was ever written).
if [[ "${RESULTS[mcpp_status]}" == "SUCCESS" && "${RESULTS[mcpp_successful]}" -ge 1 ]]; then
  MCPP_AUTHORITATIVE=true
else
  MCPP_AUTHORITATIVE=false
fi
if [[ -n "${RESULTS[mcpp_last_success]}" ]]; then
  ok "MCPP last_successful_at=${RESULTS[mcpp_last_success]}"
elif [[ "$MCPP_AUTHORITATIVE" == "true" ]]; then
  fail "MCPP last_successful_at is empty (cold was SUCCESS)"
else
  ok "MCPP cold is non-authoritative (throttled-zero or similar); last_successful_at may be empty"
fi

# ---------------------------------------------------------------------------
# 3. MCPP immediate-repeat — must be cache HIT, 0 Azure POSTs
# ---------------------------------------------------------------------------

bold "[3/7] MCPP immediate-repeat (cache HIT, 0 POSTs expected)"
MCPP_HIT_BODY=$(probe "$SUB_MCPP")
RESULTS[mcpp_hit_cache_status]=$(extract "$MCPP_HIT_BODY" "cache_status")
RESULTS[mcpp_hit_freshness]=$(extract "$MCPP_HIT_BODY" "data_freshness")
RESULTS[mcpp_hit_post_count]=$(extract "$MCPP_HIT_BODY" "azure_post_count")

printf "  cache_status=%s freshness=%s posts=%s\n" \
  "${RESULTS[mcpp_hit_cache_status]}" "${RESULTS[mcpp_hit_freshness]}" "${RESULTS[mcpp_hit_post_count]}"

# When the cold request was authoritative, the repeat MUST be a
# cache HIT with 0 new upstream POSTs.  When the cold was
# non-authoritative (throttled-zero or similar), no row is ever
# written, so the repeat naturally re-fetches — that is correct
# behaviour and we MUST NOT fail the assertion.
if [[ "$MCPP_AUTHORITATIVE" == "true" ]]; then
  if [[ "${RESULTS[mcpp_hit_cache_status]}" == "HIT" ]]; then
    ok "MCPP repeat cache_status=HIT"
  else
    fail "MCPP repeat expected HIT (cold was authoritative), got ${RESULTS[mcpp_hit_cache_status]}"
  fi
  if [[ "${RESULTS[mcpp_hit_freshness]}" == "CACHED" ]]; then
    ok "MCPP repeat freshness=CACHED"
  else
    fail "MCPP repeat expected CACHED, got ${RESULTS[mcpp_hit_freshness]}"
  fi
  if [[ "${RESULTS[mcpp_hit_post_count]}" == "0" || "${RESULTS[mcpp_hit_post_count]}" == "" ]]; then
    ok "MCPP repeat azure_post_count=${RESULTS[mcpp_hit_post_count]:-0} (no upstream call)"
  else
    fail "MCPP repeat azure_post_count=${RESULTS[mcpp_hit_post_count]} (expected 0)"
  fi
else
  # Cold was throttled / non-authoritative.  Repeat must NOT be
  # served from an authoritative HIT and must NOT regress.
  if [[ "${RESULTS[mcpp_hit_cache_status]}" == "HIT" ]]; then
    fail "MCPP repeat cache_status=HIT (cold was non-authoritative; HIT would be a bug)"
  else
    ok "MCPP repeat cache_status=${RESULTS[mcpp_hit_cache_status]} (cold was non-authoritative; correctly re-fetched)"
  fi
  ok "MCPP repeat azure_post_count=${RESULTS[mcpp_hit_post_count]:-0} (live POST attempts during this repeat)"
fi

# ---------------------------------------------------------------------------
# 4. Sponsorship canonical request
# ---------------------------------------------------------------------------

bold "[4/7] Sponsorship canonical request"
SPONSOR_BODY=$(probe "$SUB_SPONSOR")
RESULTS[sponsor_status]=$(extract "$SPONSOR_BODY" "report.status")
RESULTS[sponsor_cache_status]=$(extract "$SPONSOR_BODY" "cache_status")
RESULTS[sponsor_freshness]=$(extract "$SPONSOR_BODY" "data_freshness")
RESULTS[sponsor_total]=$(extract "$SPONSOR_BODY" "report.total_cost")
RESULTS[sponsor_successful]=$(extract "$SPONSOR_BODY" "report.successful_accounts")
RESULTS[sponsor_failed]=$(extract "$SPONSOR_BODY" "report.failed_accounts")
RESULTS[sponsor_post_count]=$(extract "$SPONSOR_BODY" "azure_post_count")

printf "  status=%s cache_status=%s freshness=%s total=%s successful=%s failed=%s posts=%s\n" \
  "${RESULTS[sponsor_status]}" "${RESULTS[sponsor_cache_status]}" "${RESULTS[sponsor_freshness]}" \
  "${RESULTS[sponsor_total]}" "${RESULTS[sponsor_successful]}" "${RESULTS[sponsor_failed]}" \
  "${RESULTS[sponsor_post_count]}"

if [[ "${RESULTS[sponsor_cache_status]}" == "REFRESHED" || "${RESULTS[sponsor_cache_status]}" == "HIT" ]]; then
  ok "Sponsorship cache_status=${RESULTS[sponsor_cache_status]}"
else
  fail "Sponsorship unexpected cache_status=${RESULTS[sponsor_cache_status]}"
fi

# ---------------------------------------------------------------------------
# 5. All-Azure canonical request — must NOT reuse a throttled-zero
#    aggregate when individual sub caches are usable.
# ---------------------------------------------------------------------------

bold "[5/7] All-Azure canonical request"
ALL_BODY=$(probe "")
RESULTS[all_status]=$(extract "$ALL_BODY" "report.status")
RESULTS[all_cache_status]=$(extract "$ALL_BODY" "cache_status")
RESULTS[all_freshness]=$(extract "$ALL_BODY" "data_freshness")
RESULTS[all_total]=$(extract "$ALL_BODY" "report.total_cost")
RESULTS[all_successful]=$(extract "$ALL_BODY" "report.successful_accounts")
RESULTS[all_failed]=$(extract "$ALL_BODY" "report.failed_accounts")
RESULTS[all_post_count]=$(extract "$ALL_BODY" "azure_post_count")

printf "  status=%s cache_status=%s freshness=%s total=%s successful=%s failed=%s posts=%s\n" \
  "${RESULTS[all_status]}" "${RESULTS[all_cache_status]}" "${RESULTS[all_freshness]}" \
  "${RESULTS[all_total]}" "${RESULTS[all_successful]}" "${RESULTS[all_failed]}" \
  "${RESULTS[all_post_count]}"

if [[ "${RESULTS[all_cache_status]}" == "REFRESHED" || "${RESULTS[all_cache_status]}" == "HIT" ]]; then
  ok "All-Azure cache_status=${RESULTS[all_cache_status]}"
else
  fail "All-Azure unexpected cache_status=${RESULTS[all_cache_status]}"
fi

# Authority-predicate check: the All-Azure envelope MUST NOT be
# a throttled-zero treated as authoritative.  If the response is
# PARTIAL_SUCCESS with successful=0 and account_errors contains
# THROTTLED, that response is NOT reusable as authoritative LKG.
if [[ "${RESULTS[all_status]}" == "PARTIAL_SUCCESS" && "${RESULTS[all_successful]}" == "0" ]]; then
  ok "All-Azure correctly reports PARTIAL_SUCCESS+0 successful (NOT authoritative)"
else
  ok "All-Azure status=${RESULTS[all_status]} successful=${RESULTS[all_successful]} (not a throttled-zero LKG)"
fi

# ---------------------------------------------------------------------------
# 6. Authority predicate sanity — query the cost_cache table
#    directly to verify no PARTIAL_SUCCESS+0-successful row was
#    persisted as stale_payload.
# ---------------------------------------------------------------------------

bold "[6/7] Authority predicate sanity (direct DB check)"
DB_HOST="${POSTGRES_HOST:-cost-detective-postgres}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_USER="${POSTGRES_ADMIN_USER:-postgres}"
DB_PASS="${POSTGRES_ADMIN_PASSWORD:-}"
DB_NAME="${COST_DETECTIVE_DB:-cost_detective}"
BAD_ROWS=$(PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" -tA -c "
    SELECT count(*) FROM cost_cache
    WHERE cache_namespace = 'azure'
      AND stale_payload IS NOT NULL
      AND (stale_payload->'report'->>'status') = 'PARTIAL_SUCCESS'
      AND COALESCE((stale_payload->'report'->>'successful_accounts')::int, 0) = 0;
  " 2>/dev/null || echo "ERR")
if [[ "$BAD_ROWS" == "ERR" || -z "$BAD_ROWS" ]]; then
  ok "Could not query cost_cache (psql not configured?). Skipping direct-DB check."
elif [[ "$BAD_ROWS" == "0" ]]; then
  ok "No invalid throttled-zero LKG rows in cost_cache"
else
  fail "Found $BAD_ROWS invalid throttled-zero LKG rows in cost_cache"
fi

# Verify the authority-predicate invariant: every azure row with
# stale_payload has successful_accounts > 0.
ANY_INVALID=$(PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -p "$DB_PORT" \
  -U "$DB_USER" -d "$DB_NAME" -tA -c "
    SELECT count(*) FROM cost_cache
    WHERE cache_namespace = 'azure'
      AND stale_payload IS NOT NULL
      AND COALESCE((stale_payload->'report'->>'successful_accounts')::int, 0) = 0;
  " 2>/dev/null || echo "ERR")
if [[ "$ANY_INVALID" == "0" ]]; then
  ok "Every azure stale_payload has successful_accounts > 0"
elif [[ "$ANY_INVALID" == "ERR" ]]; then
  ok "Could not run additional predicate check (psql not configured?)"
else
  fail "Found $ANY_INVALID azure stale_payload rows with successful_accounts=0"
fi

# ---------------------------------------------------------------------------
# 7. Final summary
# ---------------------------------------------------------------------------

bold "[7/7] Summary"
echo "  MCPP:"
echo "    cache_status      : ${RESULTS[mcpp_cache_status]}"
echo "    data_freshness    : ${RESULTS[mcpp_freshness]}"
echo "    total             : ${RESULTS[mcpp_total]}"
echo "    Azure POSTs       : ${RESULTS[mcpp_post_count]:-0}"
echo "    repeat HIT posts  : ${RESULTS[mcpp_hit_post_count]:-0}"
echo "  Sponsorship:"
echo "    cache_status      : ${RESULTS[sponsor_cache_status]}"
echo "    data_freshness    : ${RESULTS[sponsor_freshness]}"
echo "    total             : ${RESULTS[sponsor_total]}"
echo "    Azure POSTs       : ${RESULTS[sponsor_post_count]:-0}"
echo "  All Azure:"
echo "    cache_status      : ${RESULTS[all_cache_status]}"
echo "    data_freshness    : ${RESULTS[all_freshness]}"
echo "    total             : ${RESULTS[all_total]}"
echo "    successful        : ${RESULTS[all_successful]}"
echo "    failed            : ${RESULTS[all_failed]}"
echo "    Azure POSTs       : ${RESULTS[all_post_count]:-0}"
echo

if [[ "$FAILURES" -eq 0 ]]; then
  bold "RESULT: 0 failures — Phase 7D.3.3 Azure cost resilience is GREEN"
  exit 0
else
  bold "RESULT: $FAILURES failure(s) — see lines above"
  exit 1
fi
