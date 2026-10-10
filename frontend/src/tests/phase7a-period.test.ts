// Phase 7A — Calendar-month billing unit tests.
//
// Covers the BillingPeriod type, the month selector helper, and the
// period context.  No network calls; all assertions are pure.

import { describe, it, expect } from 'vitest'
import {
  buildMonthList,
  monthLabel,
  defaultBillingPeriod,
  ALLOWED_LOOKBACK_DAYS,
  type BillingPeriod,
} from '../types/finops'

describe('calendar-month billing — helpers', () => {
  it('buildMonthList returns 12 entries ending at the current month', () => {
    const ref = new Date(Date.UTC(2026, 9, 9)) // 2026-10-09
    const list = buildMonthList(ref, 12)
    expect(list).toHaveLength(12)
    expect(list[list.length - 1]).toBe('2026-10')
    expect(list[0]).toBe('2025-11')
  })

  it('buildMonthList wraps across years', () => {
    const ref = new Date(Date.UTC(2026, 1, 5)) // 2026-02-05
    const list = buildMonthList(ref, 3)
    expect(list).toEqual(['2025-12', '2026-01', '2026-02'])
  })

  it('monthLabel distinguishes Month-to-Date from Full month', () => {
    const ref = new Date(Date.UTC(2026, 9, 9)) // Oct 9
    expect(monthLabel('2026-10', ref)).toBe('October 2026 · Month-to-Date')
    expect(monthLabel('2026-09', ref)).toBe('September 2026 · Full month')
    expect(monthLabel('2025-12', ref)).toBe('December 2025 · Full month')
  })

  it('monthLabel returns raw string when malformed', () => {
    const ref = new Date(Date.UTC(2026, 9, 9))
    expect(monthLabel('2026-13', ref)).toBe('2026-13')
    expect(monthLabel('nope', ref)).toBe('nope')
  })

  it('defaultBillingPeriod returns the current UTC month in YYYY-MM', () => {
    const ref = new Date(Date.UTC(2026, 9, 9))
    expect(defaultBillingPeriod(ref)).toEqual({ kind: 'month', month: '2026-10' })
  })
})

describe('BillingPeriod shape', () => {
  it('supports both kind=month and kind=days', () => {
    const m: BillingPeriod = { kind: 'month', month: '2026-09' }
    const d: BillingPeriod = { kind: 'days', days: 30 }
    expect(m.kind).toBe('month')
    expect(d.kind).toBe('days')
    expect((d as { days: number }).days).toBe(30)
  })

  it('preserves the legacy days allow-list', () => {
    expect(ALLOWED_LOOKBACK_DAYS).toEqual([7, 30, 60, 90])
  })
})
