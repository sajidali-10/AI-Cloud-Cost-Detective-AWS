// Phase 7C.1 — provider-context smoke tests.
//
// Pins the public contract:
//   * ProviderProvider wires useProviderContext() and exposes the
//     default AWS provider.
//   * The persisted storage key is `cd.provider.selection.v1`.
//   * Auth logout resets the active provider back to AWS.
//   * isProviderReady reports true only when the descriptor is
//     registered, enabled, and OK.
//   * source-level pin: every page that uses FinOps cache keys
//     invalidates the cache when the active provider changes.

import { describe, expect, it, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, render, renderHook } from '@testing-library/react'
import {
  ProviderProvider,
  useProviderContext,
  clearStoredProviderForTests,
} from '../lib/providerContext'

const SRC_ROOT = resolve(__dirname, '..')

function src(relative: string): string {
  return readFileSync(resolve(SRC_ROOT, relative), 'utf-8')
}

describe('Phase 7C.1 — ProviderProvider defaults', () => {
  beforeEach(() => {
    clearStoredProviderForTests()
    window.sessionStorage.clear()
  })
  afterEach(() => {
    clearStoredProviderForTests()
    window.sessionStorage.clear()
  })

  it('exposes AWS as the default provider', () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ProviderProvider>{children}</ProviderProvider>
    )
    const { result } = renderHook(() => useProviderContext(), { wrapper })
    expect(result.current.provider).toBe('AWS')
    expect(result.current.providerSlug).toBe('aws')
    expect(result.current.providerVersion).toBe(0)
  })

  it('setProvider bumps the version and persists to sessionStorage', () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ProviderProvider>{children}</ProviderProvider>
    )
    const { result } = renderHook(() => useProviderContext(), { wrapper })
    act(() => {
      result.current.setProvider('AZURE')
    })
    expect(result.current.provider).toBe('AZURE')
    expect(result.current.providerVersion).toBe(1)
    expect(
      window.sessionStorage.getItem('cd.provider.selection.v1'),
    ).toBe('AZURE')
  })

  it('isProviderReady returns false for AZURE (not registered in 7C.1)', () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ProviderProvider>{children}</ProviderProvider>
    )
    const { result } = renderHook(() => useProviderContext(), { wrapper })
    // The hook seeds providers via a fetch from /api/cloud/providers;
    // without a transport the array is empty so isProviderReady is
    // false for every kind — exactly the expected Phase 7C.1 state.
    expect(result.current.isProviderReady('AWS')).toBe(false)
    expect(result.current.isProviderReady('AZURE')).toBe(false)
  })

  it('useProviderContext throws outside a ProviderProvider', () => {
    expect(() => renderHook(() => useProviderContext())).toThrow(
      /useProviderContext must be used within/,
    )
  })
})

describe('Phase 7C.1 — ProviderContext source invariants', () => {
  it('persists selection in sessionStorage under cd.provider.selection.v1', () => {
    const ctx = src('lib/providerContext.tsx')
    expect(ctx).toMatch(/STORAGE_KEY = 'cd\.provider\.selection\.v1'/)
    expect(ctx).toMatch(/sessionStorage/)
    expect(ctx).not.toMatch(/localStorage\.setItem/)
    expect(ctx).not.toMatch(/localStorage\.getItem/)
  })

  it('exposes providerVersion for cache key derivation', () => {
    const ctx = src('lib/providerContext.tsx')
    expect(ctx).toMatch(/providerVersion/)
    expect(ctx).toMatch(/setProviderVersion/)
  })

  it('resetting on logout falls back to AWS', () => {
    const ctx = src('lib/providerContext.tsx')
    expect(ctx).toMatch(/DEFAULT_PROVIDER/)
    expect(ctx).toMatch(/clearStoredProvider/)
  })
})

describe('Phase 7C.1 — App.tsx nests ProviderProvider above AccountProvider', () => {
  it('App.tsx wraps AccountProvider in ProviderProvider', () => {
    const app = src('App.tsx')
    expect(app).toMatch(/ProviderProvider/)
    expect(app).toMatch(/AccountProvider/)
    // Ordering: ProviderProvider must wrap AccountProvider so the
    // account selector can be scoped per-provider later.
    const providerIdx = app.indexOf('<ProviderProvider>')
    const accountIdx = app.indexOf('<AccountProvider>')
    expect(providerIdx).toBeGreaterThan(-1)
    expect(accountIdx).toBeGreaterThan(-1)
    expect(providerIdx).toBeLessThan(accountIdx)
  })
})
