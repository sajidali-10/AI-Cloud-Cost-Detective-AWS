// Phase 7B — Shared test wrapper for auth bootstrap.
//
// Phase 7A added a `useAuth()` call inside `useFinopsQuery` and
// `FinopsPeriodProvider`, which means every test that mounts a
// page component (or calls `useFinopsQuery` directly) must provide
// an `AuthProvider` in its render tree.  Before Phase 7A, page-level
// tests could wrap with `ThemeProvider + FinopsPeriodProvider` and
// the store would just call `apiFetch`, which itself did not
// consult auth state.
//
// This helper provides two reusable wrappers:
//
//   * `withAuthDisabled(node)` — pre-binds a transport that
//     returns `{auth_enabled: false}`.  AuthProvider lands on the
//     `disabled` synthetic-anonymous phase, `useFinopsQuery`'s gate
//     opens (because `auth.authEnabled === false`), and fetches
//     proceed normally.
//
//   * `withAuthEnabled(node, opts)` — pre-binds a transport that
//     returns `{auth_enabled: true}` and either (a) a stored
//     session in sessionStorage, or (b) an empty session so the
//     provider lands on `unauthenticated`.  Tests that exercise
//     login/logout use this variant; tests that just need the
//     gate open usually want the disabled variant.
//
// Both wrappers include `ThemeProvider` + `AuthProvider` +
// `FinopsPeriodProvider` + `MemoryRouter` — i.e. the same provider
// stack `App.tsx` uses in production.  The router wrapper is
// `MemoryRouter` (so `useLocation` works without a real
// `window.history`).
//
// No secret values, tokens, or credentials are stored in source —
// the only "token" used in tests is a literal placeholder string
// like `'test-jwt'` for assertions about Authorization-header
// propagation.  Phase 7B regression tests assert that no real
// secrets are echoed into logs.

import type { ReactNode, ReactElement } from 'react'
import { render, type RenderOptions, type RenderResult } from '@testing-library/react'
import { ThemeProvider } from '../../lib/theme'
import { AuthProvider, type AuthUser } from '../../lib/auth'
import { FinopsPeriodProvider } from '../../lib/finops/period'
import { ProviderProvider } from '../../lib/providerContext'
import { AccountProvider } from '../../lib/accountContext'
import { MemoryRouter } from '../../lib/router'

const AUTH_INFO_ENABLED = {
  auth_enabled: true,
  issuer: 'test-issuer',
  audience: 'test-audience',
  algorithm: 'HS256',
  access_token_minutes: 60,
}

const AUTH_INFO_DISABLED = {
  auth_enabled: false,
  issuer: '',
  audience: '',
  algorithm: 'HS256',
  access_token_minutes: 60,
}

const ANON_USER: AuthUser = {
  id: 0,
  email: 'anon@example.test',
  display_name: 'Anonymous',
  role: 'ADMIN',
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

const SESSION_STORAGE_KEY = 'accd.session'

function sessionJson(token: string, user: AuthUser): string {
  return JSON.stringify({
    access_token: token,
    expires_at: Date.now() + 60 * 60 * 1000,
    user,
  })
}

/**
 * Configure the apiFetch transport so `/api/auth/info` returns the
 * supplied payload (and everything else falls through to the
 * caller-supplied transport).  The transport is reset before each
 * invocation so tests don't leak state.
 */
async function configureAuthInfoOnly(
  authInfo: typeof AUTH_INFO_ENABLED | typeof AUTH_INFO_DISABLED,
  fallback?: typeof fetch,
): Promise<void> {
  const { __resetApiForTests, configureApi } = await import('../../lib/api')
  __resetApiForTests()
  configureApi({
    getToken: () => null,
    onSessionExpired: () => {},
    onForbidden: () => {},
    transport: (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/auth/info') {
        return new Response(JSON.stringify(authInfo), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (fallback) return fallback(input as RequestInfo, init)
      return new Response(JSON.stringify({}), { status: 404 })
    }) as unknown as typeof fetch,
  })
}

interface DisabledOpts {
  initialPeriod?: { kind: 'month'; month: string } | { kind: 'days'; days: 7 | 30 | 60 | 90 }
  initialRegion?: string
}

export async function renderWithAuthDisabled(
  node: ReactNode,
  opts: DisabledOpts = {},
): Promise<RenderResult> {
  await configureAuthInfoOnly(AUTH_INFO_DISABLED)
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <FinopsPeriodProvider
                {...(opts.initialPeriod ? { initialPeriod: opts.initialPeriod } : {})}
                {...(opts.initialRegion ? { initialRegion: opts.initialRegion } : {})}
              >
                {node}
              </FinopsPeriodProvider>
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

interface EnabledOpts extends DisabledOpts {
  /** Bearer token to inject via sessionStorage so /api/auth/me succeeds. */
  token?: string
  user?: AuthUser
}

export async function renderWithAuthEnabled(
  node: ReactNode,
  opts: EnabledOpts = {},
): Promise<RenderResult> {
  const token = opts.token ?? 'test-jwt'
  const user = opts.user ?? ANON_USER
  // Seed sessionStorage so AuthProvider sees a stored session on mount.
  window.sessionStorage.setItem(SESSION_STORAGE_KEY, sessionJson(token, user))
  await configureAuthInfoOnly(AUTH_INFO_ENABLED, async (input) => {
    const path = typeof input === 'string' ? input : input.toString()
    if (path === '/api/auth/me') {
      return new Response(JSON.stringify(user), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({}), { status: 404 })
  })
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <FinopsPeriodProvider
                {...(opts.initialPeriod ? { initialPeriod: opts.initialPeriod } : {})}
                {...(opts.initialRegion ? { initialRegion: opts.initialRegion } : {})}
              >
                {node}
              </FinopsPeriodProvider>
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

/**
 * Lightweight element-builder variant — useful when a test needs
 * the tree but wants to call its own `render()` (e.g. with custom
 * `RenderOptions`).
 */
export function authDisabledElement(
  node: ReactNode,
  opts: DisabledOpts = {},
): ReactElement {
  return (
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <FinopsPeriodProvider
                {...(opts.initialPeriod ? { initialPeriod: opts.initialPeriod } : {})}
                {...(opts.initialRegion ? { initialRegion: opts.initialRegion } : {})}
              >
                {node}
              </FinopsPeriodProvider>
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>
  )
}
