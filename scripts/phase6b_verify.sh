#!/usr/bin/env bash
# scripts/phase6b_verify.sh
#
# Phase 6B — Real AWS FinOps Dashboard verification.
#
# Delegates to phase6a_verify.sh and phase5c_verify.sh first so any
# Phase 5/6A regression surfaces immediately.  Then runs the
# Phase 6B-specific structural, data-integration, theme, and
# production-build checks for the HipLink FinOps dashboard.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
declare -a FAIL_LINES

run_check() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $label"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $label"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("$label")
  fi
}

note() {
  echo "---- $1"
}

# ---------------------------------------------------------------------------
# Phase 6A + Phase 5C regression (delegated).
# ---------------------------------------------------------------------------

note "Phase 6A + Phase 5C regression (delegated)"
if bash scripts/phase6a_verify.sh > /tmp/phase6a.out 2>&1; then
  echo "PASS  phase6a_verify.sh delegation"
  PASS=$((PASS + 1))
else
  echo "FAIL  phase6a_verify.sh delegation"
  tail -40 /tmp/phase6a.out
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("phase6a_verify.sh delegation")
fi

# ---------------------------------------------------------------------------
# Phase 6B — Typed FinOps surface.
# ---------------------------------------------------------------------------

note "Typed FinOps surface"

run_check "frontend/src/types/finops.ts exists" test -f frontend/src/types/finops.ts
run_check "frontend/src/lib/format.ts exists" test -f frontend/src/lib/format.ts
run_check "frontend/src/lib/finops/store.ts exists" test -f frontend/src/lib/finops/store.ts
run_check "frontend/src/lib/finops/identity.ts exists" test -f frontend/src/lib/finops/identity.ts
run_check "frontend/src/lib/finops/costs.ts exists" test -f frontend/src/lib/finops/costs.ts
run_check "frontend/src/lib/finops/resources.ts exists" test -f frontend/src/lib/finops/resources.ts
run_check "frontend/src/lib/finops/utilization.ts exists" test -f frontend/src/lib/finops/utilization.ts
run_check "frontend/src/lib/finops/optimization.ts exists" test -f frontend/src/lib/finops/optimization.ts
run_check "frontend/src/lib/finops/period.tsx exists" test -f frontend/src/lib/finops/period.tsx

# Endpoint mapping inside the data layer.
run_check "identity service calls /api/aws/identity" \
  bash -c 'grep -q "/api/aws/identity" frontend/src/lib/finops/identity.ts'
run_check "costs service calls /api/aws/costs" \
  bash -c 'grep -q "/api/aws/costs" frontend/src/lib/finops/costs.ts'
run_check "resources service calls /api/aws/resources" \
  bash -c 'grep -q "/api/aws/resources" frontend/src/lib/finops/resources.ts'
run_check "utilization service POSTs /api/aws/utilization" \
  bash -c 'grep -q "/api/aws/utilization" frontend/src/lib/finops/utilization.ts'
run_check "optimization service hits /api/aws/optimization/*" \
  bash -c 'grep -q "/api/aws/optimization" frontend/src/lib/finops/optimization.ts'

# ALLOWED_LOOKBACK_DAYS lives in the type module.
run_check "ALLOWED_LOOKBACK_DAYS declared as [7,30,60,90]" \
  bash -c 'grep -qE "ALLOWED_LOOKBACK_DAYS.*=\s*\[7,\s*30,\s*60,\s*90\]" frontend/src/types/finops.ts'

# The period context defaults to 30 days.
run_check "FinopsPeriodProvider default days=30" \
  bash -c 'grep -q "initialDays = 30" frontend/src/lib/finops/period.tsx'

# Typed interfaces exist for every backend payload.
run_check "AwsIdentity interface present" bash -c 'grep -q "interface AwsIdentity" frontend/src/types/finops.ts'
run_check "CostReport / CostReportResponse present" bash -c 'grep -q "interface CostReportResponse" frontend/src/types/finops.ts'
run_check "ResourcesResponse present" bash -c 'grep -q "interface ResourcesResponse" frontend/src/types/finops.ts'
run_check "UtilizationResponse present" bash -c 'grep -q "interface UtilizationResponse" frontend/src/types/finops.ts'
run_check "Recommendation + CapabilitiesResponse present" \
  bash -c 'grep -q "interface CapabilitiesResponse" frontend/src/types/finops.ts && grep -q "interface Recommendation " frontend/src/types/finops.ts'

# No `any` lurking in the new types.
run_check "no untyped `any` in frontend/src/types/finops.ts" \
  bash -c '! grep -E ":\s*any(\b|\[|,|\s|$)" frontend/src/types/finops.ts'

# ---------------------------------------------------------------------------
# Phase 6B — chart / selector / badge primitives.
# ---------------------------------------------------------------------------

note "Chart, selector, and badge primitives"

run_check "CostTrendChart component exists" test -f frontend/src/components/CostTrendChart.tsx
run_check "HorizontalBarChart component exists" test -f frontend/src/components/HorizontalBarChart.tsx
run_check "PeriodSelector component exists" test -f frontend/src/components/PeriodSelector.tsx
run_check "RegionFilter component exists" test -f frontend/src/components/RegionFilter.tsx
run_check "PartialWarning component exists" test -f frontend/src/components/PartialWarning.tsx
run_check "RefreshButton component exists" test -f frontend/src/components/RefreshButton.tsx
run_check "UtilizationPanel component exists" test -f frontend/src/components/UtilizationPanel.tsx
run_check "CapabilityStatusRow component exists" test -f frontend/src/components/CapabilityStatusRow.tsx
run_check "RecommendationSourceBadge / ConfidenceBadge" \
  bash -c 'grep -q "RecommendationSourceBadge" frontend/src/components/RecommendationSourceBadge.tsx && grep -q "ConfidenceBadge" frontend/src/components/RecommendationSourceBadge.tsx'

# Charts consume semantic tokens — no hardcoded palette.
run_check "CostTrendChart uses semantic stroke/fill classes" \
  bash -c 'grep -q "stroke-border" frontend/src/components/CostTrendChart.tsx && grep -q "stroke-primary" frontend/src/components/CostTrendChart.tsx'
run_check "HorizontalBarChart uses bg-primary token" \
  bash -c 'grep -q "bg-primary" frontend/src/components/HorizontalBarChart.tsx'

# Screen-reader mirror in CostTrendChart.
run_check "CostTrendChart exposes sr-only table mirror" \
  bash -c 'grep -q "sr-only" frontend/src/components/CostTrendChart.tsx'

# Utilisation panel renders "No data" — never 0.
run_check "UtilizationPanel returns No data fallback (not 0)" \
  bash -c 'grep -q "No data" frontend/src/components/UtilizationPanel.tsx'

# ---------------------------------------------------------------------------
# Phase 6B — pages wired to real data.
# ---------------------------------------------------------------------------

note "Pages wired to real FinOps data"

run_check "DashboardPage imports useFinopsQuery" \
  bash -c 'grep -q "useFinopsQuery" frontend/src/pages/DashboardPage.tsx'
run_check "DashboardPage uses fetchIdentity" \
  bash -c 'grep -q "fetchIdentity" frontend/src/pages/DashboardPage.tsx'
run_check "DashboardPage uses fetchCostReport" \
  bash -c 'grep -q "fetchCostReport" frontend/src/pages/DashboardPage.tsx'
run_check "DashboardPage uses fetchResources" \
  bash -c 'grep -q "fetchResources" frontend/src/pages/DashboardPage.tsx'
run_check "DashboardPage uses fetchCapabilities + fetchOptimizationSummary" \
  bash -c 'grep -q "fetchCapabilities" frontend/src/pages/DashboardPage.tsx && grep -q "fetchOptimizationSummary" frontend/src/pages/DashboardPage.tsx'
run_check "CostsPage uses fetchCostReport" \
  bash -c 'grep -q "fetchCostReport" frontend/src/pages/CostsPage.tsx'
run_check "ResourcesPage uses fetchResources" \
  bash -c 'grep -q "fetchResources" frontend/src/pages/ResourcesPage.tsx'
run_check "ResourcesPage uses fetchUtilization" \
  bash -c 'grep -q "fetchUtilization" frontend/src/pages/ResourcesPage.tsx'
run_check "OptimizationPage uses fetchRecommendations + fetchCapabilities + fetchOptimizationSummary" \
  bash -c 'grep -q "fetchRecommendations" frontend/src/pages/OptimizationPage.tsx && grep -q "fetchCapabilities" frontend/src/pages/OptimizationPage.tsx && grep -q "fetchOptimizationSummary" frontend/src/pages/OptimizationPage.tsx'

# Account-id masking in headers.
run_check "DashboardPage masks account id" \
  bash -c 'grep -q "maskAccountId" frontend/src/pages/DashboardPage.tsx'

# KPI 4 null-savings rule.
run_check "OptimizationPage renders Not available for null savings" \
  bash -c 'grep -q "Not available" frontend/src/pages/OptimizationPage.tsx'

# Per-resource utilization UI exists.
run_check "UtilizationPanel integrated into ResourcesPage" \
  bash -c 'grep -q "UtilizationPanel" frontend/src/pages/ResourcesPage.tsx'

# Capability status surfaced on Dashboard + Optimization.
run_check "CapabilityStatusRow integrated on Dashboard" \
  bash -c 'grep -q "CapabilityStatusRow" frontend/src/pages/DashboardPage.tsx'
run_check "CapabilityStatusRow integrated on Optimization" \
  bash -c 'grep -q "CapabilityStatusRow" frontend/src/pages/OptimizationPage.tsx'

# Partial-success UI present on Dashboard + Optimization.
run_check "DashboardPage renders PartialWarning" \
  bash -c 'grep -q "PartialWarning" frontend/src/pages/DashboardPage.tsx'
run_check "OptimizationPage renders PartialWarning" \
  bash -c 'grep -q "PartialWarning" frontend/src/pages/OptimizationPage.tsx'

# Refresh button + period selector present on each page.
run_check "DashboardPage has PeriodSelector + RefreshButton" \
  bash -c 'grep -q "PeriodSelector" frontend/src/pages/DashboardPage.tsx && grep -q "RefreshButton" frontend/src/pages/DashboardPage.tsx'
run_check "CostsPage has PeriodSelector + RefreshButton + RegionFilter" \
  bash -c 'grep -q "PeriodSelector" frontend/src/pages/CostsPage.tsx && grep -q "RefreshButton" frontend/src/pages/CostsPage.tsx && grep -q "RegionFilter" frontend/src/pages/CostsPage.tsx'
run_check "OptimizationPage has PeriodSelector + RefreshButton" \
  bash -c 'grep -q "PeriodSelector" frontend/src/pages/OptimizationPage.tsx && grep -q "RefreshButton" frontend/src/pages/OptimizationPage.tsx'

# Period context is shared — Dashboard + Costs + Optimization + Resources
# all consume it via the same provider.
run_check "App wraps tree in FinopsPeriodProvider" \
  bash -c 'grep -q "FinopsPeriodProvider" frontend/src/App.tsx'

# "global" / "no_region" rendered as "Global / No Region" — not dropped.
run_check "RegionFilter labels Global / No Region for global entries" \
  bash -c 'grep -q "Global / No Region" frontend/src/components/RegionFilter.tsx'

# ---------------------------------------------------------------------------
# Phase 6B — no fake fixture data leaked into production source.
# ---------------------------------------------------------------------------

note "No fake FinOps data in production source"

# Static-grep: any hardcoded numeric spend string in a page that is
# wired to live data is a fabrication.  Allow fixtures in tests/.
if grep -RnE "USD\s*[0-9]{3,5}\.[0-9]{2}" \
     frontend/src/pages \
     frontend/src/components/UtilizationPanel.tsx 2>/dev/null \
     | grep -vE "://|comment|\\\\bplaceholder\\\\b" >/tmp/phase6b_fake.out; then
  if [ -s /tmp/phase6b_fake.out ]; then
    echo "FAIL  no hardcoded USD numeric fixtures in src/pages or UtilizationPanel"
    cat /tmp/phase6b_fake.out | head -5
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("no hardcoded USD numeric fixtures")
  else
    echo "PASS  no hardcoded USD numeric fixtures in src/pages or UtilizationPanel"
    PASS=$((PASS + 1))
  fi
else
  echo "PASS  no hardcoded USD numeric fixtures in src/pages or UtilizationPanel"
  PASS=$((PASS + 1))
fi

# Optimization title pattern — the spec says "27/3" or similar
# category counts MUST come from API responses, not literals.
run_check "no hardcoded '27 EBS / 3 EIP' style literals" \
  bash -c "! grep -REn \"\\b27\\b.*\\bEBS\\b\" frontend/src/pages"

# ---------------------------------------------------------------------------
# Phase 6B — dark/light themes + no ad-hoc palettes.
# ---------------------------------------------------------------------------

note "Dark / light themes"

run_check "CostTrendChart consumes semantic tokens (no raw hex)" \
  bash -c "! grep -E '#[0-9a-fA-F]{3,6}' frontend/src/components/CostTrendChart.tsx"
run_check "HorizontalBarChart consumes semantic tokens (no raw hex)" \
  bash -c "! grep -E '#[0-9a-fA-F]{3,6}' frontend/src/components/HorizontalBarChart.tsx"
run_check "UtilizationPanel consumes semantic tokens (no raw hex)" \
  bash -c "! grep -E '#[0-9a-fA-F]{3,6}' frontend/src/components/UtilizationPanel.tsx"
run_check "DashboardPage consumes semantic tokens (no raw hex)" \
  bash -c "! grep -E '#[0-9a-fA-F]{3,6}' frontend/src/pages/DashboardPage.tsx"

# Backend caching is preserved — no new code bypasses cost_cache.
run_check "no frontend code touches /api/aws/cost-cache directly" \
  bash -c "! grep -REn \"cost-cache\" frontend/src"

# No localhost / private IPs in any Phase 6B frontend source
# (excluding comments and tests — the spec forbids them in shipped
# code; comments and test patterns are intentionally allowed).
run_check "no localhost / 127.0.0.1 in shipped frontend source" \
  bash -c "! grep -REn 'localhost|127\\.0\\.0\\.1' frontend/src --include='*.ts' --include='*.tsx' --exclude-dir=tests | grep -vE ':[0-9]+:(\\s*//|\\s*\\*)'"

# No AWS / LiteLLM / JWT secrets in shipped Phase 6B frontend source
# (excluding test regex patterns that legitimately mention `jwt-secret`
# as a string-literal example).
run_check "no AWS / LiteLLM / JWT secret strings in shipped frontend" \
  bash -c "! grep -REn 'AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,}|jwt[_-]?secret\\s*[:=]\\s*'\"'\"'['\"'\"'][A-Za-z0-9_\\-]{8,}'\"'\"'' frontend/src --include='*.ts' --include='*.tsx' --exclude-dir=tests"

# ---------------------------------------------------------------------------
# Phase 6B — frontend tests + production build.
# ---------------------------------------------------------------------------

note "Frontend tests + production build"

run_check "phase6b frontend tests file present" \
  test -f frontend/src/tests/format.test.ts
run_check "phase6b finops store tests present" \
  test -f frontend/src/tests/finops-store.test.tsx
run_check "phase6b charts tests present" \
  test -f frontend/src/tests/charts.test.tsx
run_check "phase6b badges tests present" \
  test -f frontend/src/tests/badges.test.tsx
run_check "phase6b period selector tests present" \
  test -f frontend/src/tests/period-selector.test.tsx
run_check "phase6b dashboard tests present" \
  test -f frontend/src/tests/dashboard.test.tsx
run_check "phase6b costs-page tests present" \
  test -f frontend/src/tests/costs-page.test.tsx
run_check "phase6b resources-page tests present" \
  test -f frontend/src/tests/resources-page.test.tsx
run_check "phase6b optimization-page tests present" \
  test -f frontend/src/tests/optimization-page.test.tsx

# Production build.  Run from host (frontend container is the
# runtime image and does not carry package.json).
run_check "frontend production build succeeds" \
  bash -c 'cd frontend && npm run build 2>&1 > /tmp/frontend-build.out'
run_check "frontend build emits dist/index.html" test -f frontend/dist/index.html

# ---------------------------------------------------------------------------
# Phase 6B — live backend integration sanity (best-effort, non-blocking).
# ---------------------------------------------------------------------------

note "Live backend integration (best-effort)"

if curl -sSf -o /tmp/identity.json http://localhost/api/aws/identity 2>/dev/null; then
  if python3 -c "import json,sys; d=json.load(open('/tmp/identity.json')); assert 'account' in d and 'region' in d, d; sys.exit(0)"; then
    echo "PASS  /api/aws/identity reachable and shaped correctly"
    PASS=$((PASS + 1))
  else
    echo "FAIL  /api/aws/identity response missing required fields"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("/api/aws/identity shape")
  fi
else
  echo "SKIP  /api/aws/identity not reachable (backend down?)"
fi

if curl -sSf -o /tmp/costs.json "http://localhost/api/aws/costs?days=30" 2>/dev/null; then
  if python3 -c "import json,sys; d=json.load(open('/tmp/costs.json')); assert 'report' in d and 'daily_trend' in d['report'], d; sys.exit(0)"; then
    echo "PASS  /api/aws/costs reachable and shaped correctly"
    PASS=$((PASS + 1))
  else
    echo "FAIL  /api/aws/costs response missing report/daily_trend"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("/api/aws/costs shape")
  fi
else
  echo "SKIP  /api/aws/costs not reachable (backend down?)"
fi

if curl -sSf -o /tmp/recs.json "http://localhost/api/aws/optimization/recommendations?days=30" 2>/dev/null; then
  if python3 -c "import json,sys; d=json.load(open('/tmp/recs.json')); assert 'recommendations' in d and 'count' in d, d; sys.exit(0)"; then
    echo "PASS  /api/aws/optimization/recommendations reachable"
    PASS=$((PASS + 1))
  else
    echo "FAIL  /api/aws/optimization/recommendations response missing count"
    FAIL=$((FAIL + 1))
    FAIL_LINES+=("/optimization/recommendations shape")
  fi
else
  echo "SKIP  /api/aws/optimization/recommendations not reachable"
fi

# ---------------------------------------------------------------------------
# Phase 6B — Docker / nginx.
# ---------------------------------------------------------------------------

note "Docker + nginx"

run_check "docker-compose.yml present" test -f docker-compose.yml
run_check "only nginx publishes a host port" \
  bash -c 'PUB=$(docker compose ps --format "table {{.Service}}|{{.Ports}}" 2>/dev/null | awk -F"|" "NR>1 && \$2 ~ /->/ {print \$1}" | sort -u | tr "\n" "," | sed "s/,$//"); test "$PUB" = "nginx"'
run_check "compose declares ports only on nginx" \
  bash -c 'BAD=$(awk "
    /^  [a-zA-Z_-]+:/ { svc = \$2; sub(/:$/, \"\", svc); next }
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*\$/ { next }
    /^[[:space:]]+ports:/ { if (svc != \"nginx\") { print svc; exit 1 } }
  " docker-compose.yml); test -z "$BAD"'

# All 5 containers healthy.
run_check "all docker containers healthy" \
  bash -c 'UNHEALTHY=$(docker compose ps --format "{{.Service}}:{{.State}}" 2>/dev/null | awk -F: "\$2 != \"healthy\" && \$2 != \"running\" {print \$0}"); test -z "$UNHEALTHY"'

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "============================================"
echo "Phase 6B verification summary"
echo "============================================"
echo "PASS=$PASS"
echo "FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo "Failed checks:"
  for line in "${FAIL_LINES[@]}"; do
    echo "  - $line"
  done
  echo "OVERALL: FAIL"
  exit 1
fi
echo "OVERALL: PASS"
exit 0
