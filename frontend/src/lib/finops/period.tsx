// Phase 7A — Shared FinOps Period context.
//
// Replaces the legacy days-only selector with a calendar-month primary
// selector and a rolling-lookback back-compat path.  The Dashboard,
// Costs and Optimization pages share this provider so a selection on
// any of them flows to the others without manual prop-drilling.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
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

export function FinopsPeriodProvider({
  children,
  initialPeriod,
  initialRegion = 'all',
}: ProviderProps) {
  const [period, setPeriod] = useState<BillingPeriod>(
    initialPeriod ?? defaultBillingPeriod(),
  )
  const [region, setRegionState] = useState<RegionFilter>(initialRegion)

  const setMonth = useCallback((month: string) => {
    if (typeof month !== 'string' || month.length !== 7 || month[4] !== '-') return
    setPeriod({ kind: 'month', month })
  }, [])

  const setDays = useCallback((next: LookbackDays) => {
    if (!ALLOWED_LOOKBACK_DAYS.includes(next)) return
    setPeriod({ kind: 'days', days: next })
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
