// Phase 7A — Cache primitives for the FinOps store.
//
// Lives in its own module so the React-based `useFinopsQuery`
// (in `./store`) can import `useAuth` without creating an import
// cycle with `./auth`.  `AuthProvider` calls `invalidateFinopsCache`
// on login / logout / session expiry; query subscribers call
// `getOrCreateEntry` to register interest in a key.
//
// Phase 7D.3.2 — preserves last-known-good data across failed
// refreshes so a throttled / partial response cannot replace a
// previously-successful snapshot with a fake $0.00.

export type LoadStatus = 'idle' | 'loading' | 'success' | 'error' | 'refreshing'

export interface CachedEntry<T> {
  status: LoadStatus
  data: T | null
  error: Error | null
  /** ISO string of when the entry was last successfully fetched. */
  refreshedAt: string | null
  /**
   * Last KNOWN-GOOD data.  Survives a failed refresh so the UI can
   * render the previous authoritative value alongside a "Refresh
   * temporarily unavailable" caption.  Distinct from ``data`` —
   * ``data`` reflects the most-recent fetch attempt (may be null
   * or partial), ``lastGood`` reflects the most-recent SUCCESSFUL
   * fetch.
   */
  lastGood: T | null
  /** ISO string of when ``lastGood`` was last refreshed. */
  lastGoodRefreshedAt: string | null
}

type Listener = () => void

export interface InternalEntry<T> {
  cacheKey: string
  status: LoadStatus
  data: T | null
  error: Error | null
  refreshedAt: string | null
  lastGood: T | null
  lastGoodRefreshedAt: string | null
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
    lastGood: null,
    lastGoodRefreshedAt: null,
    inflight: null,
    listeners: new Set(),
    ttlMs,
    snapshot: {
      status: 'idle',
      data: null,
      error: null,
      refreshedAt: null,
      lastGood: null,
      lastGoodRefreshedAt: null,
    },
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
 *
 * Phase 7D.3.2 — invalidation now preserves ``lastGood`` /
 * ``lastGoodRefreshedAt`` so the UI can fall back to a known-good
 * snapshot during the subsequent refresh.  ``data`` / ``error`` /
 * ``refreshedAt`` are reset so the next fetch fires immediately,
 * but the previous authoritative value is still surfaced through
 * ``lastGood``.
 *
 * Pass ``{ preserveLastGood: false }`` to drop the last-good
 * snapshot too (used by logout flows that must wipe every trace
 * of the prior session).
 */
export function invalidateCache(
  prefix: string = '',
  options: { preserveLastGood?: boolean } = {},
): void {
  const preserveLastGood = options.preserveLastGood !== false
  for (const [key, entry] of store.entries()) {
    if (prefix && !key.startsWith(prefix)) continue
    // Phase 7D.3.2 closure — the field-level wipe MUST clear
    // ``entry.data`` (the live, in-memory response) AND
    // ``entry.snapshot.data`` together.  A previous version only
    // cleared ``snapshot.data`` while leaving ``entry.data``
    // intact, so the next runFetch transitioned the entry to
    // ``refreshing`` (instead of ``loading``) and continued to
    // serve the prior response through the live ``entry.data``
    // path until the new fetch settled.  That stale-data leak
    // surfaced as order-dependent failures in three pre-existing
    // dashboard / optimization tests whose next-mount transport
    // returned a different shape (502 cost / 500 legacy /
    // optimization list).
    entry.status = 'idle'
    entry.data = null
    entry.error = null
    entry.refreshedAt = null
    entry.inflight = null
    if (!preserveLastGood) {
      entry.lastGood = null
      entry.lastGoodRefreshedAt = null
    }
    entry.snapshot = {
      status: entry.status,
      data: null,
      error: null,
      refreshedAt: null,
      lastGood: preserveLastGood ? entry.lastGood : null,
      lastGoodRefreshedAt: preserveLastGood ? entry.lastGoodRefreshedAt : null,
    }
    emit(entry)
  }
}

/**
 * Phase 7A — Invalidate every FinOps cache entry (full wipe).
 *
 * Called from the AuthProvider on login / logout / session-expired
 * so a stale authenticated response from user A never surfaces
 * under user B's session, and so a 401 from a transiently-missing
 * token never sticks around as a fake success.
 *
 * Phase 7D.3.2 — login and session-expired flows STILL wipe
 * ``lastGood`` (they MUST, because the data belongs to a
 * different user).  Provider / scope / period switches in the
 * page-level effects call ``invalidateFinopsCacheKeepLastGood``
 * instead, so the last-good snapshot survives a refresh.
 */
export function invalidateFinopsCache(): void {
  invalidateCache('', { preserveLastGood: false })
}

/**
 * Phase 7D.3.2 — Invalidate every FinOps cache entry but PRESERVE
 * the last-known-good snapshot.  Used by page-level effects that
 * switch provider / scope / period: the next fetch fires
 * immediately, but if it fails the previous authoritative value
 * is still surfaceable as a "Stale" fallback.
 */
export function invalidateFinopsCacheKeepLastGood(): void {
  invalidateCache('', { preserveLastGood: true })
}

/** Number of cached entries (test-only). */
export function _storeSize(): number {
  return store.size
}
