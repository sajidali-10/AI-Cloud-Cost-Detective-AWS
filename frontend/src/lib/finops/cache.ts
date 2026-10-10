// Phase 7A — Cache primitives for the FinOps store.
//
// Lives in its own module so the React-based `useFinopsQuery`
// (in `./store`) can import `useAuth` without creating an import
// cycle with `./auth`.  `AuthProvider` calls `invalidateFinopsCache`
// on login / logout / session expiry; query subscribers call
// `getOrCreateEntry` to register interest in a key.

export type LoadStatus = 'idle' | 'loading' | 'success' | 'error' | 'refreshing'

export interface CachedEntry<T> {
  status: LoadStatus
  data: T | null
  error: Error | null
  /** ISO string of when the entry was last successfully fetched. */
  refreshedAt: string | null
}

type Listener = () => void

export interface InternalEntry<T> {
  cacheKey: string
  status: LoadStatus
  data: T | null
  error: Error | null
  refreshedAt: string | null
  inflight: Promise<void> | null
  listeners: Set<Listener>
  ttlMs: number
  /** Stable snapshot returned to useSyncExternalStore.  Replaced atomically on every transition. */
  snapshot: CachedEntry<T>
}

const store = new Map<string, InternalEntry<unknown>>()

function nowIso(): string {
  return new Date().toISOString()
}

export function emit(entry: InternalEntry<unknown>): void {
  for (const l of entry.listeners) l()
}

export function getOrCreateEntry<T>(
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
    snapshot: { status: 'idle', data: null, error: null, refreshedAt: null },
  }
  store.set(cacheKey, entry as InternalEntry<unknown>)
  return entry
}

export function isFresh(entry: InternalEntry<unknown>): boolean {
  if (!entry.refreshedAt) return false
  const age = Date.now() - new Date(entry.refreshedAt).getTime()
  return age < entry.ttlMs && entry.status === 'success'
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
    entry.snapshot = { status: 'idle', data: null, error: null, refreshedAt: null }
    emit(entry)
  }
}

/**
 * Phase 7A — Invalidate every FinOps cache entry.
 *
 * Called from the AuthProvider on login / logout / session-expired
 * so a stale authenticated response from user A never surfaces
 * under user B's session, and so a 401 from a transiently-missing
 * token never sticks around as a fake success.
 */
export function invalidateFinopsCache(): void {
  invalidateCache('')
}

/** Number of cached entries (test-only). */
export function _storeSize(): number {
  return store.size
}
