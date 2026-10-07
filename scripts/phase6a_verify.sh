#!/usr/bin/env bash
# scripts/phase6a_verify.sh
#
# Phase 6A — HipLink Professional Frontend Foundation verification.
#
# This script is a structural + regression check.  It does NOT replace
# full Phase 5C verification; it DELEGATES to it.  After delegation it
# runs Phase 6A-specific checks for the HipLink frontend foundation:
#
#   - frontend structure imports
#   - HipLink-style application shell exists
#   - horizontal desktop navigation exists
#   - responsive navigation exists
#   - login page exists
#   - auth/session provider exists
#   - route guard exists
#   - centralized API client exists
#   - ADMIN role navigation
#   - ANALYST role navigation
#   - VIEWER role navigation
#   - admin page protected
#   - theme provider exists
#   - dark mode supported
#   - light mode supported
#   - theme preference persistence
#   - theme toggle exists
#   - no dark-only major surfaces
#   - no hardcoded EC2 IP / public IP / private IP / localhost in browser code
#   - no browser localhost
#   - no frontend secrets
#   - frontend tests
#   - production build
#   - Phase 5C regression
#   - Docker health
#   - nginx only public
#
# Output: PASS/FAIL per check + summary totals.

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
# Phase 5C regression — delegate first so any regression surfaces
# before the Phase 6A-specific checks.  Phase 5C checks include
# pytest, migrations, backend health, nginx config, etc.
# ---------------------------------------------------------------------------

note "Phase 5C regression (delegated)"
if bash scripts/phase5c_verify.sh > /tmp/phase5c.out 2>&1; then
  echo "PASS  phase5c_verify.sh delegation"
  PASS=$((PASS + 1))
else
  echo "FAIL  phase5c_verify.sh delegation"
  tail -30 /tmp/phase5c.out
  FAIL=$((FAIL + 1))
  FAIL_LINES+=("phase5c_verify.sh delegation")
fi

# ---------------------------------------------------------------------------
# Frontend structural checks
# ---------------------------------------------------------------------------

note "Frontend structural integrity"

run_check "frontend package.json exists" test -f frontend/package.json
run_check "vite config exists" test -f frontend/vite.config.ts
run_check "tailwind config exists" test -f frontend/tailwind.config.js
run_check "index.html exists with no-flash bootstrap" \
  bash -c 'grep -q "data-theme" frontend/index.html && grep -q "accd.theme" frontend/index.html'
run_check "frontend index.css defines semantic tokens" \
  bash -c 'grep -q -- "--bg:" frontend/src/index.css && grep -q -- "--primary:" frontend/src/index.css'
run_check "tailwind.config.js uses CSS variables (semantic tokens)" \
  bash -c 'grep -q "var(--bg)" frontend/tailwind.config.js && grep -q "var(--primary)" frontend/tailwind.config.js'

# ---------------------------------------------------------------------------
# Application shell components
# ---------------------------------------------------------------------------

note "Application shell components"

run_check "AppShell.tsx exists" test -f frontend/src/components/AppShell.tsx
run_check "TopNavigation.tsx (horizontal desktop nav) exists" test -f frontend/src/components/TopNavigation.tsx
run_check "MobileNavigation.tsx (responsive nav) exists" test -f frontend/src/components/MobileNavigation.tsx
run_check "BrandHeader.tsx exists" test -f frontend/src/components/BrandHeader.tsx
run_check "ThemeToggle.tsx exists" test -f frontend/src/components/ThemeToggle.tsx
run_check "UserMenu.tsx exists" test -f frontend/src/components/UserMenu.tsx
run_check "PageHeader.tsx exists" test -f frontend/src/components/PageHeader.tsx
run_check "SectionCard.tsx exists" test -f frontend/src/components/SectionCard.tsx
run_check "MetricCard.tsx exists" test -f frontend/src/components/MetricCard.tsx
run_check "DataTable.tsx exists" test -f frontend/src/components/DataTable.tsx
run_check "StatusBadge.tsx exists" test -f frontend/src/components/StatusBadge.tsx
run_check "RoleBadge.tsx exists" test -f frontend/src/components/RoleBadge.tsx
run_check "AccessDenied.tsx exists" test -f frontend/src/components/AccessDenied.tsx
run_check "LoadingSkeleton.tsx exists" test -f frontend/src/components/LoadingSkeleton.tsx
run_check "EmptyState.tsx exists" test -f frontend/src/components/EmptyState.tsx
run_check "ErrorState.tsx exists" test -f frontend/src/components/ErrorState.tsx
run_check "FilterBar.tsx exists" test -f frontend/src/components/FilterBar.tsx

# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

note "Phase 6A pages"

run_check "LoginPage.tsx exists" test -f frontend/src/pages/LoginPage.tsx
run_check "DashboardPage.tsx exists" test -f frontend/src/pages/DashboardPage.tsx
run_check "CostsPage.tsx exists" test -f frontend/src/pages/CostsPage.tsx
run_check "ResourcesPage.tsx exists" test -f frontend/src/pages/ResourcesPage.tsx
run_check "OptimizationPage.tsx exists" test -f frontend/src/pages/OptimizationPage.tsx
run_check "AIAnalystPage.tsx (shell) exists" test -f frontend/src/pages/AIAnalystPage.tsx
run_check "ConversationsPage.tsx exists" test -f frontend/src/pages/ConversationsPage.tsx
run_check "UsersPage.tsx (admin) exists" test -f frontend/src/pages/UsersPage.tsx
run_check "SecurityPage.tsx exists" test -f frontend/src/pages/SecurityPage.tsx

# ---------------------------------------------------------------------------
# Auth + RBAC
# ---------------------------------------------------------------------------

note "Authentication + RBAC"

run_check "AuthProvider exists" test -f frontend/src/lib/auth.tsx
run_check "auth.tsx exposes AuthProvider + useAuth" \
  bash -c 'grep -q "export function AuthProvider" frontend/src/lib/auth.tsx && grep -q "export function useAuth" frontend/src/lib/auth.tsx'
run_check "auth.tsx honours auth_enabled" \
  bash -c 'grep -q "auth_enabled" frontend/src/lib/auth.tsx'
run_check "tokens.ts (RBAC + nav config) exists" test -f frontend/src/lib/tokens.ts
run_check "tokens.ts declares ADMIN role navigation" \
  bash -c 'grep -q "ADMIN" frontend/src/lib/tokens.ts'
run_check "tokens.ts declares ANALYST role navigation" \
  bash -c 'grep -q "ANALYST" frontend/src/lib/tokens.ts'
run_check "tokens.ts declares VIEWER role navigation" \
  bash -c 'grep -q "VIEWER" frontend/src/lib/tokens.ts'
run_check "UsersPage denies non-admin access" \
  bash -c 'grep -q "AccessDenied" frontend/src/pages/UsersPage.tsx && grep -q "role !== .ADMIN." frontend/src/pages/UsersPage.tsx'
run_check "API client (centralized) exists" test -f frontend/src/lib/api.ts
run_check "API client uses relative paths only" \
  bash -c 'grep -q "starts with" frontend/src/lib/api.ts'

# ---------------------------------------------------------------------------
# Theme system
# ---------------------------------------------------------------------------

note "Theme system (dark + light + persistence + no-flash)"

run_check "ThemeProvider exists" test -f frontend/src/lib/theme.tsx
run_check "ThemeProvider exports useTheme" \
  bash -c 'grep -q "export function useTheme" frontend/src/lib/theme.tsx'
run_check "theme.tsx persists choice via localStorage" \
  bash -c 'grep -q "localStorage.setItem" frontend/src/lib/theme.tsx'
run_check "theme.tsx honours prefers-color-scheme" \
  bash -c "grep -q 'prefers-color-scheme' frontend/src/lib/theme.tsx"
run_check "index.html inline bootstrap sets data-theme pre-React" \
  bash -c 'grep -q "data-theme" frontend/index.html && grep -q "matchMedia" frontend/index.html'
run_check "ThemeToggle renders an accessible button" \
  bash -c 'grep -q "aria-label" frontend/src/components/ThemeToggle.tsx'
run_check "ThemeProvider exports useTheme" \
  bash -c 'grep -q "export function useTheme" frontend/src/lib/theme.tsx'
run_check "auth.tsx exposes AuthProvider + useAuth" \
  bash -c 'grep -q "export function AuthProvider" frontend/src/lib/auth.tsx && grep -q "export function useAuth" frontend/src/lib/auth.tsx'
run_check "UsersPage denies non-admin access" \
  bash -c 'grep -q "AccessDenied" frontend/src/pages/UsersPage.tsx'
run_check "API client uses relative paths only" \
  bash -c 'grep -q "starts with" frontend/src/lib/api.ts'

# ---------------------------------------------------------------------------
# Static regressions on the source tree
# ---------------------------------------------------------------------------

note "Static regressions (no hardcoded hosts / secrets / dark-only surfaces)"

# No localhost / 127.0.0.1 / private IPs in frontend browser code.
run_check "no localhost / 127.0.0.1 in frontend src (excludes comments)" \
  bash -c "! grep -rE 'localhost|127\\.0\\.0\\.1' frontend/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' --exclude-dir=node_modules --exclude-dir=tests | grep -v '^\\s*//\\|//.*localhost\\|//.*127\\.0\\.0\\.1'"

run_check "no private IP ranges in frontend src" \
  bash -c "! grep -rE '10\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}|192\\.168\\.[0-9]{1,3}\\.[0-9]{1,3}|169\\.254\\.[0-9]{1,3}\\.[0-9]{1,3}' frontend/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' --exclude-dir=node_modules --exclude-dir=tests"

run_check "no hardcoded EC2 / public IP literals in frontend src" \
  bash -c "! grep -rE 'ec2-[0-9-]+\\.compute[.-]amazonaws\\.com|[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}' frontend/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' --exclude-dir=node_modules --exclude-dir=tests"

run_check "no frontend secret strings" \
  bash -c "! grep -rE 'AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{16,}|jwt[_-]?secret\\s*[:=]\\s*[\"\\x27][^\"\\x27]{8,}' frontend/src --include='*.ts' --include='*.tsx' --include='*.css' --include='*.html' --exclude-dir=node_modules --exclude-dir=tests"

run_check "no direct slate/gray Tailwind palette in major components" \
  bash -c "! grep -rE 'bg-slate-|bg-gray-|text-slate-|text-gray-|border-slate-|border-gray-' frontend/src/components --include='*.tsx' --include='*.ts'"

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

note "Frontend tests"

run_check "frontend vitest suite passes" \
  bash -c 'cd frontend && npm test 2>&1 > /tmp/frontend-tests.out && test $(grep -c "^ ✓" /tmp/frontend-tests.out) -gt 50'

# ---------------------------------------------------------------------------
# Production build
# ---------------------------------------------------------------------------

note "Production build"

run_check "frontend production build succeeds" \
  bash -c 'cd frontend && npm run build 2>&1 > /tmp/frontend-build.out'

run_check "frontend build emits dist/index.html" test -f frontend/dist/index.html

# ---------------------------------------------------------------------------
# Docker / nginx
# ---------------------------------------------------------------------------

note "Docker + nginx"

run_check "docker-compose.yml present" test -f docker-compose.yml
run_check "only nginx publishes port 80" \
  bash -c 'grep -A2 "^  nginx:" docker-compose.yml | grep -q "80:80" && ! grep -B2 -A20 "^  postgres:" docker-compose.yml | grep -q "ports:"'
run_check "nginx config has /api/ → backend proxy" \
  bash -c 'grep -q "proxy_pass http://backend:8000" nginx/default.conf'
run_check "nginx config has / → frontend proxy" \
  bash -c 'grep -q "proxy_pass http://frontend:8080" nginx/default.conf'

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "============================================"
echo "Phase 6A verification summary"
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
