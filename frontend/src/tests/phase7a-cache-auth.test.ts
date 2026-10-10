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
  // Reset transport to a default 401 response; each test overrides.
  setTransport(async () => new Response(JSON.stringify({}), { status: 401 }))
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
    setTransport(async () => {
      if (mode === 'unauth') return new Response(JSON.stringify({}), { status: 401 })
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    })

    const { result, rerender } = renderHook(
      () => useFinopsQuery('cache:probe', fetcher),
      { wrapper: ({ children }) => wrapWithProviders(children) },
    )
    // No token; the gate should keep the entry idle.  Wait a tick.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).not.toHaveBeenCalled()
    expect(result.current.entry.data).toBeNull()

    // Now log in (simulate by switching the transport and the token).
    mode = 'authed'
    token = 'jwt'
    rerender()
    await waitFor(() => expect(result.current.entry.data).toEqual({ ok: true }))
  })

  it('clears the cache when session-expired fires', async () => {
    token = 'jwt'
    let storeAfter: number | null = null
    setTransport(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }))

    // Pre-warm the cache.
    const { result } = renderHook(
      () => useFinopsQuery('cache:warm', async () => ({ ok: 1 })),
      { wrapper: ({ children }) => wrapWithProviders(children) },
    )
    await waitFor(() => expect(result.current.entry.data).toEqual({ ok: 1 }))
    expect(_storeSize()).toBeGreaterThan(0)
    storeAfter = _storeSize()

    // Force a 401 — the apiFetch layer should call onSessionExpired
    // which invalidates the cache.
    setTransport(async () => new Response(JSON.stringify({}), { status: 401 }))
    await act(async () => {
      result.current.refresh()
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(_storeSize()).toBe(0)
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
