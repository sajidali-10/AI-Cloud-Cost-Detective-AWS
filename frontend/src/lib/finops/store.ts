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

import { useCallback, useEffect, useRef, useState } from 'react'

export type LoadStatus = 'idle' | 'loading' | 'success' | 'error' | 'refreshing'

export interface CachedEntry<T> {
  status: LoadStatus
  data: T | null
  error: Error | null
  /** ISO string of when the entry was last successfully fetched. */
  refreshedAt: string | null
}

export interface FinopsSubscription<T> {
  entry: CachedEntry<T>
  refresh: () => void
}

type Listener = () => void

interface InternalEntry<T> {
  cacheKey: string
  status: LoadStatus
  data: T | null
  error: Error | null
  refreshedAt: string | null
  inflight: Promise<void> | null
  listeners: Set<Listener>
  ttlMs: number
}

const store = new Map<string, InternalEntry<unknown>>()

function nowIso(): string {
  return new Date().toISOString()
}

function emit(entry: InternalEntry<unknown>): void {
  for (const l of entry.listeners) l()
}

function getOrCreateEntry<T>(
  cacheKey: string,
  ttlMs: number,
): InternalEntry<T> {
  const existing = store.get(cacheKey)
  if (existing) {
    existing.ttlMs = ttlMs
    return existing as InternalEntry<T>
  }
  const entry: InternalEntry<T> = {
    cacheKey,
    status: 'idle',
    data: null,
    error: null,
    refreshedAt: null,
    inflight: null,
    listeners: new Set(),
    ttlMs,
  }
  store.set(cacheKey, entry as InternalEntry<unknown>)
  return entry
}

async function runFetch<T>(
  entry: InternalEntry<T>,
  fetcher: () => Promise<T>,
): Promise<void> {
  entry.status = entry.data ? 'refreshing' : 'loading'
  entry.error = null
  emit(entry)
  try {
    const data = await fetcher()
    entry.data = data
    entry.status = 'success'
    entry.refreshedAt = nowIso()
    entry.error = null
  } catch (err) {
    entry.status = 'error'
    entry.error = err instanceof Error ? err : new Error(String(err))
  } finally {
    entry.inflight = null
    emit(entry)
  }
}

function isFresh(entry: InternalEntry<unknown>): boolean {
  if (!entry.refreshedAt) return false
  const age = Date.now() - new Date(entry.refreshedAt).getTime()
  return age < entry.ttlMs && entry.status === 'success'
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
 */
export function useFinopsQuery<T>(
  cacheKey: string,
  fetcher: () => Promise<T>,
  options: { ttlMs?: number } = {},
): FinopsSubscription<T> {
  const ttlMs = options.ttlMs ?? 60_000
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const [, setTick] = useState(0)

  // Use a ref to track the currently-subscribed cacheKey so we can
  // detect a change between renders.  The entry itself lives in
  // the module-level store, not in React state, so multiple
  // subscribers can share it.
  const lastKeyRef = useRef<string | null>(null)

  useEffect(() => {
    const entry = getOrCreateEntry<T>(cacheKey, ttlMs)
    const listener: Listener = () => setTick((n) => n + 1)
    entry.listeners.add(listener)
    lastKeyRef.current = cacheKey
    const needsFetch =
      entry.status === 'idle' ||
      (!isFresh(entry) && entry.status !== 'loading' && entry.status !== 'refreshing')
    if (needsFetch) {
      entry.inflight = runFetch(entry, () => fetcherRef.current())
    }
    return () => {
      entry.listeners.delete(listener)
    }
    // The fetcher is intentionally captured via ref so a new
    // function reference on every render does NOT trigger an
    // automatic refetch — only `cacheKey` changes do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey, ttlMs])

  // Re-render whenever the current cacheKey entry changes.
  const entry = getOrCreateEntry<T>(cacheKey, ttlMs)
  const refresh = useCallback(() => {
    const e = getOrCreateEntry<T>(cacheKey, ttlMs)
    e.inflight = runFetch(e, () => fetcherRef.current())
    setTick((n) => n + 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey, ttlMs])

  return {
    entry: {
      status: entry.status,
      data: entry.data,
      error: entry.error,
      refreshedAt: entry.refreshedAt,
    },
    refresh,
  }
}

/**
 * Invalidate every cached entry whose key contains `prefix`.
 * Used by the global Refresh button and from tests.
 */
export function invalidateCache(prefix: string = ''): void {
  for (const [key, entry] of store.entries()) {
    if (prefix && !key.startsWith(prefix)) continue
    entry.status = 'idle'
    entry.error = null
    entry.refreshedAt = null
    emit(entry)
  }
}

/** Number of cached entries (test-only). */
export function _storeSize(): number {
  return store.size
}
