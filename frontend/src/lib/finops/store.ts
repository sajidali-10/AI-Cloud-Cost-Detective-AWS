// Phase 6B — Tiny in-memory query store.
//
// Centralises data access for every FinOps widget so the dashboard,
// costs, resources, and optimization pages share one HTTP round-trip
// per (params, ttl) tuple.  Spec forbids arbitrary fetch() calls in
// components; this store is the only place HTTP happens for FinOps
// data.
//
// The store is intentionally NOT React Query — adding a dependency
// for ~100 lines of caching logic is unnecessary.  Subscribers are
// plain Set<Listener>; fetches are de-duplicated per cache key, and
// `invalidateCache()` invalidates matching entries.
//
// React 18 note: the consumer hook uses `useSyncExternalStore`
// (the documented React 18 hook for subscribing to external stores)
// instead of `useState` + `setTick`.  useSyncExternalStore
// integrates natively with React's act() boundary in tests — so a
// parallel fetch whose `finally` block notifies subscribers from a
// microtask lands inside the test's act boundary without producing
// "An update to X inside a test was not wrapped in act(...)"
// warnings.  Behaviour at runtime is identical: every cache miss
// still triggers exactly one fetch, every successful response is
// cached, every invalidate still emits to subscribers, every entry
// still exposes `{ status, data, error, refreshedAt }`.
//
// Phase 7A: cache primitives live in `./cache` so this module can
// safely import `useAuth` for the auth-bootstrap gate without
// creating an import cycle with `../auth`.

import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { useAuth } from '../auth'
import { ApiError } from '../api'
import {
  emit,
  getOrCreateEntry,
  isFresh,
  type CachedEntry,
  type InternalEntry,
} from './cache'

// Re-exports for backward-compatibility with the original Phase 6B
// public API: tests and tools import these names from `./store`.
export { invalidateCache, invalidateFinopsCache, _storeSize } from './cache'
export type { CachedEntry, LoadStatus } from './cache'
export type FinopsSubscription<T> = {
  entry: CachedEntry<T>
  refresh: () => void
}

/**
 * Phase 7A — Determine whether an error came from an authentication
 * failure (401/403).  Such errors MUST NOT be cached as a "success"
 * or kept as the entry's persistent data because the next render
 * will receive fresh credentials via `handleSessionExpired`.
 *
 * Detected by checking the underlying ApiError's `code` and `status`
 * fields.  Any other error (timeout, backend down) is left in the
 * cache so the UI can display a real error until the next refresh.
 */
function isAuthFailure(err: unknown): boolean {
  if (err instanceof ApiError) {
    return err.status === 401 || err.status === 403
  }
  return false
}

async function runFetch<T>(
  entry: InternalEntry<T>,
  fetcher: () => Promise<T>,
): Promise<void> {
  entry.status = entry.data ? 'refreshing' : 'loading'
  entry.error = null
  entry.snapshot = { status: entry.status, data: entry.data, error: null, refreshedAt: entry.refreshedAt }
  try {
    const data = await fetcher()
    entry.data = data
    entry.status = 'success'
    entry.refreshedAt = nowIso()
    entry.error = null
    entry.snapshot = {
      status: 'success',
      data,
      error: null,
      refreshedAt: entry.refreshedAt,
    }
  } catch (err) {
    // Phase 7A: 401/403 errors must not stick around as "stale
    // successful" entries — wipe both data and refreshedAt so the
    // next mount re-fetches once a valid token is available.
    if (isAuthFailure(err)) {
      entry.data = null
      entry.status = 'error'
      entry.refreshedAt = null
      entry.error = err instanceof Error ? err : new Error(String(err))
      entry.snapshot = {
        status: 'error',
        data: null,
        error: entry.error,
        refreshedAt: null,
      }
    } else {
      entry.status = 'error'
      entry.error = err instanceof Error ? err : new Error(String(err))
      entry.snapshot = {
        status: 'error',
        data: entry.data,
        error: entry.error,
        refreshedAt: entry.refreshedAt,
      }
    }
  } finally {
    entry.inflight = null
    emit(entry)
  }
}

function nowIso(): string {
  return new Date().toISOString()
}

/**
 * Subscribe to a cached query.  Returns the current entry and a
 * stable refresh function.  `cacheKey` should encode every
 * parameter that affects the response (e.g. `"costs:30"`).
 *
 * When `cacheKey` changes (e.g. user picks a different period),
 * the hook swaps to the matching entry automatically; if that
 * entry has no cached data yet, a fetch is kicked off.
 *
 * `ttlMs` controls how long a successful response stays fresh
 * before re-fetching on the next subscriber mount.
 *
 * Phase 7A: the fetch is gated on auth-bootstrap readiness.  While
 * the AuthProvider is in 'booting' or 'unauthenticated' (and not
 * the 'disabled' synthetic-anon mode), the centralized API client
 * has no token wired in — firing requests now would generate
 * 401s.  When auth becomes ready (login completes, the
 * `/api/auth/me` probe succeeds), the gate opens and any idle
 * entry fires its fetch.
 */
export function useFinopsQuery<T>(
  cacheKey: string,
  fetcher: () => Promise<T>,
  options: { ttlMs?: number } = {},
): FinopsSubscription<T> {
  const ttlMs = options.ttlMs ?? 60_000
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  // useSyncExternalStore requires getSnapshot to be referentially
  // stable when nothing has changed, so the entry's snapshot is
  // held on the entry itself and only replaced when emit() runs.
  const entry = getOrCreateEntry<T>(cacheKey, ttlMs)
  const subscribe = useCallback(
    (listener: () => void) => {
      entry.listeners.add(listener)
      return () => {
        entry.listeners.delete(listener)
      }
    },
    [entry],
  )
  const getSnapshot = useCallback(() => entry.snapshot, [entry])
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot)

  // Phase 7A: gate the fetch on auth bootstrap.
  const auth = useAuth()
  const authReady = auth.ready && (auth.user !== null || !auth.authEnabled)

  // Kick off the fetch when the cacheKey changes (or on first
  // mount).  The hook re-runs whenever the caller passes a
  // different cacheKey or ttlMs; the fetcher itself is captured
  // via ref so a new function reference on every render does NOT
  // trigger an automatic refetch.  Now also re-runs on auth
  // readiness — when login completes, any 'idle' or post-401
  // entries fire again with a valid token.
  useEffect(() => {
    const e = getOrCreateEntry<T>(cacheKey, ttlMs)
    const needsFetch =
      e.status === 'idle' ||
      (!isFresh(e) && e.status !== 'loading' && e.status !== 'refreshing')
    if (needsFetch && authReady) {
      e.inflight = runFetch(e, () => fetcherRef.current())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey, ttlMs, authReady])

  const refresh = useCallback(() => {
    const e = getOrCreateEntry<T>(cacheKey, ttlMs)
    if (authReady) {
      e.inflight = runFetch(e, () => fetcherRef.current())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey, ttlMs, authReady])

  return {
    entry: snapshot,
    refresh,
  }
}
