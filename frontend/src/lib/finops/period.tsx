// Phase 7A closure — Shared FinOps Period context.
//
// Calendar-month primary selector with rolling-lookback back-compat.
// Selected billing month and region are persisted to sessionStorage
// so an F5 / remount restores the user's last selection.  Storage
// is cleared on logout to avoid leaking the prior user's view
// selection to a re-login on the same tab.
//
// Persistence key rationale:
//   * Billing month + region are NOT secrets — they are user
//     preferences that do not grant access to anything on their
//     own.
//   * sessionStorage scopes the value to the browser tab and is
//     cleared when the tab is closed or by an explicit logout.
//   * Using a different key from the JWT keeps credential
//     handling isolated from UI state.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  ALLOWED_LOOKBACK_DAYS,
  buildMonthList,
  defaultBillingPeriod,
  type BillingPeriod,
  type LookbackDays,
} from '../../types/finops'
import { useAuth } from '../auth'

export type RegionFilter = 'all' | string

interface FinopsPeriodValue {
  /** Active billing period (calendar month preferred). */
  period: BillingPeriod
  /** Convenience: set by calendar-month string (YYYY-MM). */
  setMonth: (month: string) => void
  /** Back-compat: set by rolling-lookback days (deprecated). */
  setDays: (next: LookbackDays) => void
  /** Derived lookback days — back-compat for AI WS protocol frame. */
  days: LookbackDays
  /** 'all' = account-level cost; otherwise a specific AWS region. */
  region: RegionFilter
  setRegion: (next: RegionFilter) => void
  /** Selectable calendar months (newest last). */
  monthList: string[]
  /** Current month in `YYYY-MM` form. */
  currentMonth: string
}

const FinopsPeriodContext = createContext<FinopsPeriodValue | null>(null)

interface ProviderProps {
  children: ReactNode
  initialPeriod?: BillingPeriod
  initialRegion?: RegionFilter
}

const MONTH_STORAGE_KEY = 'accd.finops.period'
const REGION_STORAGE_KEY = 'accd.finops.region'

function safeSessionStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null
    return window.sessionStorage
  } catch {
    return null
  }
}

function readStoredPeriod(defaultPeriod: BillingPeriod): BillingPeriod {
  const storage = safeSessionStorage()
  if (!storage) return defaultPeriod
  try {
    const raw = storage.getItem(MONTH_STORAGE_KEY)
    if (!raw) return defaultPeriod
    const parsed = JSON.parse(raw) as Partial<BillingPeriod>
    if (parsed.kind === 'month' && typeof parsed.month === 'string') {
      if (parsed.month.length !== 7 || parsed.month[4] !== '-') return defaultPeriod
      return { kind: 'month', month: parsed.month }
    }
    if (parsed.kind === 'days' && typeof parsed.days === 'number') {
      if (!ALLOWED_LOOKBACK_DAYS.includes(parsed.days as LookbackDays)) return defaultPeriod
      return { kind: 'days', days: parsed.days as LookbackDays }
    }
  } catch {
    /* fallthrough */
  }
  return defaultPeriod
}

function writeStoredPeriod(period: BillingPeriod): void {
  const storage = safeSessionStorage()
  if (!storage) return
  try {
    storage.setItem(MONTH_STORAGE_KEY, JSON.stringify(period))
  } catch {
    /* non-fatal */
  }
}

function readStoredRegion(defaultRegion: RegionFilter): RegionFilter {
  const storage = safeSessionStorage()
  if (!storage) return defaultRegion
  try {
    const raw = storage.getItem(REGION_STORAGE_KEY)
    if (!raw) return defaultRegion
    if (raw === 'all') return 'all'
    return raw
  } catch {
    return defaultRegion
  }
}

function writeStoredRegion(region: RegionFilter): void {
  const storage = safeSessionStorage()
  if (!storage) return
  try {
    storage.setItem(REGION_STORAGE_KEY, region)
  } catch {
    /* non-fatal */
  }
}

function clearStoredPeriod(): void {
  const storage = safeSessionStorage()
  if (!storage) return
  try {
    storage.removeItem(MONTH_STORAGE_KEY)
    storage.removeItem(REGION_STORAGE_KEY)
  } catch {
    /* non-fatal */
  }
}

export function FinopsPeriodProvider({
  children,
  initialPeriod,
  initialRegion = 'all',
}: ProviderProps) {
  const { authReady } = useAuth()
  const [period, setPeriodState] = useState<BillingPeriod>(
    () => initialPeriod ?? readStoredPeriod(defaultBillingPeriod()),
  )
  const [region, setRegionState] = useState<RegionFilter>(
    () => initialRegion ?? readStoredRegion('all'),
  )

  // Track the previous auth state so we only re-hydrate on the
  // checking → authenticated transition.  A simple ref guards
  // against re-running storage writes on every render.
  const wasAuthedRef = useRef<boolean>(false)

  // Persist on every change.  We avoid persisting during the
  // checking state (no authenticated user yet) so the period
  // never crosses an auth boundary.
  useEffect(() => {
    if (authReady) writeStoredPeriod(period)
  }, [period, authReady])

  useEffect(() => {
    if (authReady) writeStoredRegion(region)
  }, [region, authReady])

  // When the user transitions from checking → authenticated we
  // re-hydrate from storage so a freshly-mounted session lands on
  // the previously-selected month + region.  On logout (authReady
  // goes false → authenticated) we clear storage so a re-login on
  // the same tab does not flash the previous user's selection.
  useEffect(() => {
    if (authReady && !wasAuthedRef.current) {
      const restored = readStoredPeriod(defaultBillingPeriod())
      if (restored.kind !== period.kind || (restored.kind === 'month' && period.kind === 'month' && restored.month !== period.month)) {
        setPeriodState(restored)
      }
      setRegionState(readStoredRegion('all'))
    } else if (!authReady && wasAuthedRef.current) {
      // Auth just transitioned away from authenticated (logout or
      // session expired).  Clear the selection so the next login
      // starts on the default month.
      clearStoredPeriod()
      setPeriodState(defaultBillingPeriod())
      setRegionState('all')
    }
    wasAuthedRef.current = authReady
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authReady])

  const setMonth = useCallback((month: string) => {
    if (typeof month !== 'string' || month.length !== 7 || month[4] !== '-') return
    setPeriodState({ kind: 'month', month })
  }, [])

  const setDays = useCallback((next: LookbackDays) => {
    if (!ALLOWED_LOOKBACK_DAYS.includes(next)) return
    setPeriodState({ kind: 'days', days: next })
  }, [])

  const setRegion = useCallback((next: RegionFilter) => {
    setRegionState(next)
  }, [])

  const monthList = useMemo(() => buildMonthList(), [])
  const currentMonth = monthList[monthList.length - 1] ?? ''
  const days: LookbackDays = period.kind === 'days' ? period.days : 30

  const value = useMemo<FinopsPeriodValue>(
    () => ({ period, setMonth, setDays, days, region, setRegion, monthList, currentMonth }),
    [period, setMonth, setDays, days, region, setRegion, monthList, currentMonth],
  )

  return <FinopsPeriodContext.Provider value={value}>{children}</FinopsPeriodContext.Provider>
}

export function useFinopsPeriod(): FinopsPeriodValue {
  const ctx = useContext(FinopsPeriodContext)
  if (!ctx) {
    throw new Error('useFinopsPeriod must be used within a FinopsPeriodProvider')
  }
  return ctx
}

// Test-only internals.
export const __finopsPeriodTestInternals = {
  MONTH_STORAGE_KEY,
  REGION_STORAGE_KEY,
  clearStoredPeriod,
}
