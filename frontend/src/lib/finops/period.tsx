// Phase 6B — Shared FinOps Period context.
//
// Provides the active `days` (lookback window) and `region`
// filter to every FinOps widget.  The Dashboard and Costs pages
// share this provider so a period change on the Costs page flows
// back to the Dashboard without manual prop-drilling.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../../types/finops'

export type RegionFilter = 'all' | string

interface FinopsPeriodValue {
  days: LookbackDays
  setDays: (next: LookbackDays) => void
  /** 'all' = account-level cost; otherwise a specific AWS region. */
  region: RegionFilter
  setRegion: (next: RegionFilter) => void
}

const FinopsPeriodContext = createContext<FinopsPeriodValue | null>(null)

interface ProviderProps {
  children: ReactNode
  initialDays?: LookbackDays
  initialRegion?: RegionFilter
}

export function FinopsPeriodProvider({
  children,
  initialDays = 30,
  initialRegion = 'all',
}: ProviderProps) {
  const [days, setDaysState] = useState<LookbackDays>(initialDays)
  const [region, setRegionState] = useState<RegionFilter>(initialRegion)

  const setDays = useCallback((next: LookbackDays) => {
    if (!ALLOWED_LOOKBACK_DAYS.includes(next)) return
    setDaysState(next)
  }, [])

  const setRegion = useCallback((next: RegionFilter) => {
    setRegionState(next)
  }, [])

  const value = useMemo<FinopsPeriodValue>(
    () => ({ days, setDays, region, setRegion }),
    [days, setDays, region, setRegion],
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
