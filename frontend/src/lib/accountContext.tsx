// Phase 7B — account selector context.
//
// Provides a React context that:
//   * holds the currently selected AWS account selector value
//   * persists the selection across page refreshes (sessionStorage)
//   * does NOT persist any sensitive information (no tokens, no role ARNs)
//   * exposes a list of selectable accounts fetched from the backend
//
// Selector semantics:
//   * "all"         — query every enabled account; backend returns the
//                     multi-account aggregate.
//   * "acct:<id>"   — query a specific registered target account.
//   * undefined/""  — treated as "all" for backwards-compatibility.
//
// Sensitive material (role ARN, external ID, etc.) is NEVER
// represented on the client beyond the opaque registry id and the
// already-public display name.

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
import { useProviderContext } from './providerContext'

export type AccountSelection = 'all' | `acct:${number}`

export interface SelectableAccount {
  id: number
  account_id: string
  display_name: string
  provider: string
  enabled: boolean
  is_management: boolean
}

interface AccountContextValue {
  selection: AccountSelection
  setSelection: (value: AccountSelection) => void
  accounts: SelectableAccount[]
  refreshAccounts: () => Promise<void>
  loading: boolean
  error: string | null
  toQueryParam: () => string
}

const STORAGE_KEY = 'cd.account.selection.v1'

const AccountContext = createContext<AccountContextValue | undefined>(undefined)

function readStoredSelection(): AccountSelection {
  if (typeof window === 'undefined') return 'all'
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return 'all'
    if (raw === 'all') return 'all'
    if (/^acct:\d+$/.test(raw)) return raw as AccountSelection
    return 'all'
  } catch {
    return 'all'
  }
}

function writeStoredSelection(value: AccountSelection): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(STORAGE_KEY, value)
  } catch {
    // sessionStorage may be unavailable (private mode); ignore.
  }
}

function clearStoredSelection(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
}

export function AccountProvider({ children }: { children: ReactNode }) {
  const [selection, _setSelection] = useState<AccountSelection>(() => readStoredSelection())
  const [accounts, setAccounts] = useState<SelectableAccount[]>([])
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const lastFetchRef = useRef<number>(0)

  // Phase 7C.2A — the account context is now provider-aware.
  // The active provider drives both the selectable endpoint and the
  // selection-reset logic so a stale AWS row can never satisfy an
  // Azure request (and vice versa).
  const { provider, providerVersion } = useProviderContext()

  const setSelection = useCallback((value: AccountSelection) => {
    _setSelection(value)
    if (value === 'all') {
      // Still record the choice so a refresh remembers it.
      writeStoredSelection('all')
    } else {
      writeStoredSelection(value)
    }
  }, [])

  const refreshAccounts = useCallback(
    async (opts: { force?: boolean } = {}) => {
      // Coalesce parallel refreshes — protects against multiple useEffect
      // triggers firing in the same tick on first mount.  Pass
      // ``force: true`` to bypass the lock for explicit provider
      // switches and other imperative refreshes.
      const now = Date.now()
      if (!opts.force && now - lastFetchRef.current < 250) return
      lastFetchRef.current = now
      setLoading(true)
      setError(null)
      try {
        // Phase 7C.2A — route to the provider-aware selectable
        // endpoint.  AWS keeps the legacy /api/aws/cloud-accounts/
        // selectable route (Phase 7B surface, no behaviour change);
        // AZURE (and any future registered provider) uses the
        // Phase 7C.1 /api/cloud/accounts/selectable/{provider} route.
        // RACKSPACE is not selectable in the UI today so we never
        // hit this branch for it, but the routing is generic.
        const path =
          provider === 'AWS'
            ? '/api/aws/cloud-accounts/selectable'
            : `/api/cloud/accounts/selectable/${encodeURIComponent(provider)}`
        const data = await apiFetch<{
          items: SelectableAccount[]
          count: number
        }>(path)
        // Defensive: drop anything that does not belong to the
        // active provider, so a backend bug or stale cache can
        // never leak a row across provider boundaries.
        const items = (data.items ?? []).filter((row) => row.provider === provider)
        setAccounts(items)
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        setError(message)
        setAccounts([])
      } finally {
        setLoading(false)
      }
    },
    [provider],
  )

  // Provider switch → reset the selection, drop stale rows, and
  // refetch the rows that belong to the new provider.  The effect
  // MUST NOT fire on the very first mount — that would clobber
  // the sessionStorage-rehydrated selection that ``useState``
  // initialiser just loaded.  We track first mount with a ref so
  // the reset only runs on a real provider change.
  const isFirstProviderMountRef = useRef<boolean>(true)
  useEffect(() => {
    if (isFirstProviderMountRef.current) {
      isFirstProviderMountRef.current = false
      // First mount — fetch the rows for the active provider but
      // keep whatever selection ``useState`` rehydrated from
      // sessionStorage.
      void refreshAccounts({ force: true })
      return
    }
    _setSelection('all')
    writeStoredSelection('all')
    setAccounts([])
    setError(null)
    void refreshAccounts({ force: true })
  }, [provider, providerVersion, refreshAccounts])

  // Clear selection on logout: listen for storage events.  When the
  // auth token disappears from sessionStorage, fall back to "all".
  useEffect(() => {
    function onStorage(ev: StorageEvent) {
      if (ev.key === 'cd.auth.token.v1' && !ev.newValue) {
        _setSelection('all')
        clearStoredSelection()
      }
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('storage', onStorage)
      return () => window.removeEventListener('storage', onStorage)
    }
    return undefined
  }, [])

  const value = useMemo<AccountContextValue>(() => {
    return {
      selection,
      setSelection,
      accounts,
      refreshAccounts,
      loading,
      error,
      toQueryParam: () => (selection === 'all' ? '' : selection),
    }
  }, [selection, setSelection, accounts, refreshAccounts, loading, error])

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>
}

export function useAccountContext(): AccountContextValue {
  const ctx = useContext(AccountContext)
  if (!ctx) {
    throw new Error('useAccountContext must be used within <AccountProvider>')
  }
  return ctx
}

export function clearStoredAccountSelectionForTests(): void {
  clearStoredSelection()
}
