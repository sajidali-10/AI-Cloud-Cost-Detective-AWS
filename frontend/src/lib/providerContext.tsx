// Phase 7C.1 — provider context.
//
// Adds a layer above the existing AccountContext so the UI can
// switch between cloud providers (AWS today; Azure / Rackspace in
// future phases).  Today only AWS has a live adapter, but the
// shape of the context is provider-agnostic so adding a new
// provider is a matter of plugging its adapter into the registry.
//
// Persistence:
//   * sessionStorage key `cd.provider.selection.v1`
//   * cleared on logout via the same storage-event listener the
//     AccountContext uses.
//
// Cache invalidation:
//   * The context exposes a `providerVersion` counter that bumps
//     every time the provider changes.  FinOps cache keys include
//     this counter so a provider switch automatically invalidates
//     stale data without callers having to wire an explicit
//     `invalidateFinopsCache()` call.

import {
  ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { apiFetch } from './api'

export type ProviderKind = 'AWS' | 'AZURE' | 'RACKSPACE'

export interface CloudProviderDescriptor {
  provider: ProviderKind
  is_registered: boolean
  is_enabled: boolean
  status: 'OK' | 'NO_CAPABILITIES' | 'NOT_REGISTERED' | 'ERROR' | string
  credential_strategies: string[]
  capabilities: Array<{
    capability: string
    state: string
    detail?: Record<string, unknown>
  }>
  supported_regions: Array<{ region_id: string; display_name: string }>
  supported_lookback_days: number[]
  details?: Record<string, unknown>
}

interface ProviderContextValue {
  provider: ProviderKind
  setProvider: (value: ProviderKind) => void
  providers: CloudProviderDescriptor[]
  refreshProviders: () => Promise<void>
  loading: boolean
  error: string | null
  // A monotonically increasing version that bumps on every
  // provider change.  Cache keys include it so old responses
  // never leak across a provider switch.
  providerVersion: number
  // A selector's "is this provider ready for use" helper.
  isProviderReady: (kind: ProviderKind) => boolean
  // Lower-case wire form (matches the cache key segment).
  providerSlug: string
}

const STORAGE_KEY = 'cd.provider.selection.v1'
const VALID_PROVIDERS: ProviderKind[] = ['AWS', 'AZURE', 'RACKSPACE']
const DEFAULT_PROVIDER: ProviderKind = 'AWS'

const ProviderContext = createContext<ProviderContextValue | undefined>(undefined)

function readStoredProvider(): ProviderKind {
  if (typeof window === 'undefined') return DEFAULT_PROVIDER
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_PROVIDER
    if ((VALID_PROVIDERS as string[]).includes(raw)) return raw as ProviderKind
    return DEFAULT_PROVIDER
  } catch {
    return DEFAULT_PROVIDER
  }
}

function writeStoredProvider(value: ProviderKind): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(STORAGE_KEY, value)
  } catch {
    // ignore
  }
}

function clearStoredProvider(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function ProviderProvider({ children }: { children: ReactNode }) {
  const [provider, _setProvider] = useState<ProviderKind>(() => readStoredProvider())
  const [providers, setProviders] = useState<CloudProviderDescriptor[]>([])
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [providerVersion, setProviderVersion] = useState<number>(0)
  const lastFetchRef = useRef<number>(0)

  const setProvider = useCallback((value: ProviderKind) => {
    _setProvider((current) => {
      if (current === value) return current
      setProviderVersion((v) => v + 1)
      writeStoredProvider(value)
      return value
    })
  }, [])

  const refreshProviders = useCallback(async () => {
    const now = Date.now()
    if (now - lastFetchRef.current < 250) return
    lastFetchRef.current = now
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<{
        providers: CloudProviderDescriptor[]
        count: number
      }>('/api/cloud/providers')
      setProviders(data.providers ?? [])
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setError(message)
      setProviders([])
    } finally {
      setLoading(false)
    }
  }, [])

  // Clear on logout.
  useEffect(() => {
    function onStorage(ev: StorageEvent) {
      if (ev.key === 'cd.auth.token.v1' && !ev.newValue) {
        _setProvider(DEFAULT_PROVIDER)
        clearStoredProvider()
        setProviderVersion((v) => v + 1)
      }
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('storage', onStorage)
      return () => window.removeEventListener('storage', onStorage)
    }
    return undefined
  }, [])

  const isProviderReady = useCallback(
    (kind: ProviderKind): boolean => {
      const entry = providers.find((p) => p.provider === kind)
      if (!entry) return false
      return entry.is_registered && entry.is_enabled && entry.status === 'OK'
    },
    [providers],
  )

  const providerSlug = useMemo(() => provider.toLowerCase(), [provider])

  const value = useMemo<ProviderContextValue>(() => {
    return {
      provider,
      setProvider,
      providers,
      refreshProviders,
      loading,
      error,
      providerVersion,
      isProviderReady,
      providerSlug,
    }
  }, [
    provider,
    setProvider,
    providers,
    refreshProviders,
    loading,
    error,
    providerVersion,
    isProviderReady,
    providerSlug,
  ])

  return <ProviderContext.Provider value={value}>{children}</ProviderContext.Provider>
}

export function useProviderContext(): ProviderContextValue {
  const ctx = useContext(ProviderContext)
  if (!ctx) {
    throw new Error('useProviderContext must be used within <ProviderProvider>')
  }
  return ctx
}

export function clearStoredProviderForTests(): void {
  clearStoredProvider()
}
