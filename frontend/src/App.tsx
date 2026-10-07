// Phase 6A — Application root.
//
// Composes providers in the correct order:
//   ThemeProvider   → sets data-theme + manages persistence
//     AuthProvider  → reads theme via document attribute (no dep on ThemeProvider)
//       Router      → routes + role-aware guards
//         Routes    → role-checked route tree
//           Pages
//
// Theme is intentionally outside Auth so 401-driven redirects keep
// the user's chosen palette.

import { ThemeProvider } from './lib/theme'
import { AuthProvider, useAuth } from './lib/auth'
import { Router, Routes, Navigate, type RouteSpec } from './lib/router'
import { AppShell } from './components/AppShell'
import { LoginPage } from './pages/LoginPage'
import { DashboardPage } from './pages/DashboardPage'
import { CostsPage } from './pages/CostsPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { OptimizationPage } from './pages/OptimizationPage'
import { AIAnalystPage } from './pages/AIAnalystPage'
import { ConversationsPage } from './pages/ConversationsPage'
import { UsersPage } from './pages/UsersPage'
import { SecurityPage } from './pages/SecurityPage'
import { LoadingSkeleton } from './components/LoadingSkeleton'

export default function App() {
  return (
    <ThemeProvider>
      <Router>
        <AuthProvider>
          <AppRoot />
        </AuthProvider>
      </Router>
    </ThemeProvider>
  )
}

function AppRoot() {
  const { status, ready, role, user, authEnabled } = useAuth()

  // Bootstrap gate — while we don't yet know whether auth is on,
  // render a small neutral placeholder inside the theme so the
  // first paint uses the correct palette.
  if (!ready) {
    return <BootScreen />
  }

  // Auth required but missing — render the login route only.
  // The login page itself handles "auth disabled" redirects.
  if (authEnabled && status.phase !== 'authenticated') {
    return (
      <Routes
        routes={[{ path: '/login', element: <LoginPage /> }, { path: '*', element: <LoginPage /> }]}
        currentRole={role}
      />
    )
  }

  // Authenticated (or auth disabled) — render the full app.
  return (
    <AppShell>
      <Routes
        currentRole={role}
        routes={buildRoutes({ role, user, authEnabled })}
      />
    </AppShell>
  )
}

interface BuildRoutesArgs {
  role: ReturnType<typeof useAuth>['role']
  user: ReturnType<typeof useAuth>['user']
  authEnabled: boolean
}

function buildRoutes({ role, user, authEnabled }: BuildRoutesArgs): RouteSpec[] {
  const r: RouteSpec[] = [
    { path: '/', element: <DashboardPage /> },
    { path: '/costs', element: <CostsPage /> },
    { path: '/resources', element: <ResourcesPage /> },
    { path: '/optimization', element: <OptimizationPage /> },
  ]
  if (role === 'ADMIN' || role === 'ANALYST') {
    r.push({ path: '/analyst', element: <AIAnalystPage /> })
    r.push({ path: '/conversations', element: <ConversationsPage /> })
  }
  if (role === 'ADMIN') {
    r.push({ path: '/users', element: <UsersPage /> })
    r.push({ path: '/security', element: <SecurityPage /> })
  }
  // Login route — accessible only when auth is enabled and the user
  // is NOT authenticated.  Once signed in the user is redirected
  // to the dashboard by the LoginPage effect, but we still resolve
  // /login → / to avoid the login card flashing when the user is
  // already authenticated.
  if (authEnabled && !user) {
    r.push({ path: '/login', element: <LoginPage /> })
  } else if (user) {
    r.push({ path: '/login', element: <Navigate to="/" replace /> })
  }
  return r
}

function BootScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-6">
      <div className="flex flex-col items-center gap-3">
        <div className="h-8 w-32 animate-pulse rounded bg-surface-2" aria-hidden />
        <p className="text-xs text-fg-muted">Initializing…</p>
        <span className="sr-only" role="status">
          <LoadingSkeleton className="sr-only" />
          Loading application
        </span>
      </div>
    </div>
  )
}
