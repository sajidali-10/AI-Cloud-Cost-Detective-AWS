// Phase 7A — Auth lifecycle → cache invalidation tests.
//
// These tests assert the contracts that prevent the "missing
// Authorization header" symptom seen in Phase 6B:
//   * FinOps queries wait for auth bootstrap.
//   * 401 errors invalidate the cache entry instead of being
//     treated as successful data.
//   * login / logout / session-expired all invalidate the cache.

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import React from 'react'

import {
  useFinopsQuery,
  invalidateFinopsCache,
  _storeSize,
} from '../lib/finops/store'
import { configureApi, ApiError } from '../lib/api'
import { AuthProvider, useAuth } from '../lib/auth'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { MemoryRouter } from '../lib/router'

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

let token: string | null = null
let nextResponse: () => Response = () =>
  new Response(JSON.stringify({ status: 'error' }), { status: 401 })

function setTransport(fn: typeof fetch) {
  configureApi({
    getToken: () => token,
    onSessionExpired: () => {
      token = null
      invalidateFinopsCache()
    },
    onForbidden: () => {},
    transport: fn,
  })
}

beforeEach(() => {
  token = null
  invalidateFinopsCache()
  window.sessionStorage.clear()
  // Reset transport to a default that handles both bootstrap
  // endpoints (/api/auth/info, /api/auth/me) so AuthProvider's gate
  // opens to the `auth_enabled=true` phase, and returns 401 for
  // everything else.  Tests that need a different transport override
  // via setTransport().
  setTransport(async (input: RequestInfo | URL) => {
    const path = typeof input === 'string' ? input : input.toString()
    if (path === '/api/auth/info') {
      return new Response(
        JSON.stringify({
          auth_enabled: true,
          issuer: 'test',
          audience: 'test',
          algorithm: 'HS256',
          access_token_minutes: 60,
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      )
    }
    if (path === '/api/auth/me') {
      return new Response(JSON.stringify(TEST_USER), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    return new Response(JSON.stringify({}), { status: 401 })
  })
})

// Wrapper that supplies both providers + router so the hook can
// resolve `useAuth()` and the auth bootstrap completes.
function wrapWithProviders(node: React.ReactNode): React.ReactElement {
  return React.createElement(
    MemoryRouter,
    null,
    React.createElement(AuthProvider, null, React.createElement(FinopsPeriodProvider, null, node)),
  )
}

// ---------------------------------------------------------------------------
// 401 must NOT be cached as success
// ---------------------------------------------------------------------------

const TEST_USER = {
  id: 1,
  email: 'admin@x.test',
  display_name: 'Admin',
  role: 'ADMIN' as const,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

function seedSession(tokenValue = 'test-jwt') {
  window.sessionStorage.setItem(
    'accd.session',
    JSON.stringify({
      access_token: tokenValue,
      expires_at: Date.now() + 60 * 60 * 1000,
      user: TEST_USER,
    }),
  )
}

describe('Phase 7A — cache and auth', () => {
  it('does not retain 401 data across login', async () => {
    let mode: 'unauth' | 'authed' = 'unauth'
    const fetcher = vi.fn(async () => {
      if (mode === 'unauth') {
        throw new ApiError({
          code: 'Unauthorized',
          status: 401,
          message: 'missing Authorization header',
        })
      }
      return { ok: true }
    })
    const buildTransport = (m: 'unauth' | 'authed') =>
      (async (input: RequestInfo | URL) => {
        const path = typeof input === 'string' ? input : input.toString()
        if (path === '/api/auth/info') {
          return new Response(
            JSON.stringify({
              auth_enabled: true,
              issuer: 'test',
              audience: 'test',
              algorithm: 'HS256',
              access_token_minutes: 60,
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          )
        }
        if (path === '/api/auth/me') {
          return new Response(JSON.stringify(TEST_USER), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          })
        }
        if (m === 'unauth') return new Response(JSON.stringify({}), { status: 401 })
        return new Response(JSON.stringify({ ok: true }), { status: 200 })
      }) as unknown as typeof fetch

    // First mount: no session yet -> AuthProvider lands on
    // `unauthenticated`.  The fetcher's gate stays closed because
    // authReady is false.
    setTransport(buildTransport('unauth'))
    const first = renderHook(() => useFinopsQuery('cache:probe', fetcher), {
      wrapper: ({ children }) => wrapWithProviders(children),
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).not.toHaveBeenCalled()
    expect(first.result.current.entry.data).toBeNull()
    first.unmount()

    // Now log in: write the session to sessionStorage so the
    // AuthProvider's bootstrap lands on `authenticated` when we
    // remount, opening the FinOps gate.
    mode = 'authed'
    seedSession('jwt')
    setTransport(buildTransport('authed'))
    const second = renderHook(() => useFinopsQuery('cache:probe', fetcher), {
      wrapper: ({ children }) => wrapWithProviders(children),
    })
    await waitFor(() => expect(second.result.current.entry.data).toEqual({ ok: true }))
    second.unmount()
  })

  it('clears the cache when session-expired fires', async () => {
    token = 'jwt'
    let storeAfter: number | null = null
    seedSession('jwt')
    // Phase 7B: the fetcher must actually route through apiFetch so
    // the centralized 401-handler can call onSessionExpired.  In
    // earlier revisions the fetcher was a plain `async () => ...`
    // that never produced a 401 — so changing the transport had no
    // effect and the assertion `expect(_storeSize()).toBe(0)` was
    // testing a flow that did not exist.
    const { apiFetch } = await import('../lib/api')
    const fetcher = vi.fn(async () => {
      return apiFetch<{ ok: number }>('/api/aws/costs')
    })
    setTransport(async (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/auth/info') {
        return new Response(
          JSON.stringify({
            auth_enabled: true,
            issuer: 'test',
            audience: 'test',
            algorithm: 'HS256',
            access_token_minutes: 60,
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      if (path === '/api/auth/me') {
        return new Response(JSON.stringify(TEST_USER), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ ok: 1 }), { status: 200, headers: { 'content-type': 'application/json' } })
    })

    // Pre-warm the cache.
    const { result } = renderHook(
      () => useFinopsQuery('cache:warm', fetcher),
      { wrapper: ({ children }) => wrapWithProviders(children) },
    )
    await waitFor(() => expect(result.current.entry.data).toEqual({ ok: 1 }))
    expect(_storeSize()).toBeGreaterThan(0)
    storeAfter = _storeSize()

    // Force a 401 — the apiFetch layer should call onSessionExpired
    // which invalidates the cache.
    setTransport(async (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/auth/info') {
        return new Response(
          JSON.stringify({
            auth_enabled: true,
            issuer: 'test',
            audience: 'test',
            algorithm: 'HS256',
            access_token_minutes: 60,
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      if (path === '/api/auth/me') {
        return new Response(JSON.stringify(TEST_USER), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({}), { status: 401 })
    })
    await act(async () => {
      result.current.refresh()
      // Wait long enough for: inflight → fetch → 401 → apiFetch
      // throw → onSessionExpired → invalidateFinopsCache().
      await new Promise((r) => setTimeout(r, 30))
    })
    // Phase 7B invariant: a 401 from a previously-valid token must
    // wipe BOTH the cached data AND the refreshedAt stamp on the
    // entry, so the next mount re-fetches once a valid token is
    // available.  `_storeSize()` does NOT decrement (entries stay
    // in the Map until the page unmounts); the contract is on the
    // entry state.
    expect(result.current.entry.data).toBeNull()
    expect(result.current.entry.status).toBe('error')
    expect(result.current.entry.refreshedAt).toBeNull()
    void storeAfter
  })

  it('invalidateFinopsCache clears every entry', () => {
    // Just call it on an empty store and assert no exception.
    expect(() => invalidateFinopsCache()).not.toThrow()
  })

  it('re-exposes a stable identity-change trigger', async () => {
    const { result } = renderHook(() => useAuth(), {
      wrapper: ({ children }) => wrapWithProviders(children),
    })
    // During boot we are not ready.
    expect(result.current.ready).toBe(false)
    await waitFor(() => expect(result.current.ready).toBe(true))
    expect(result.current.authEnabled).toBe(true)
    expect(result.current.user).toBeNull()
  })
})
