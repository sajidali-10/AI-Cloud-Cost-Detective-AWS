// Phase 7D.3.3 — freshness derivation tests.
//
// These tests pin the data_freshness → view-model mapping for
// the Dashboard hero, KPI tiles, and Costs page header status.
// The derivation is the single source of truth for "Live" /
// "Cached" / "Stale" / "Unavailable" labels so the two pages
// can never disagree about what the user is seeing.

import { describe, expect, it } from 'vitest'

import {
  deriveCostCompleteness,
  deriveDataFreshness,
  deriveDashboardReadiness,
  costDomainSnapshot,
} from '../lib/finops/costCompleteness'

import type { CostReportResponse } from '../types/finops'

function baseReport(overrides: Partial<CostReportResponse['report']> = {}): CostReportResponse['report'] {
  return {
    account_id: 'acct:1',
    period: { start: '2026-10-01', end: '2026-11-01', days: 31 },
    previous_period: { start: '2026-09-01', end: '2026-10-01', days: 30 },
    currency: 'USD',
    total_cost: '123.45',
    previous_period_cost: '100.00',
    change_amount: '23.45',
    change_percent: '0.23',
    estimated: false,
    daily_trend: [],
    by_service: [],
    by_region: [],
    source: 'AZURE_COST_MANAGEMENT',
    status: 'SUCCESS',
    currency_status: 'OK',
    successful_accounts: 1,
    failed_accounts: 0,
    subscription_errors: [],
    ...overrides,
  }
}

function envelope(
  overrides: Partial<CostReportResponse> = {},
): CostReportResponse {
  return {
    report: baseReport(),
    cache_status: 'REFRESHED',
    cached_at: '2026-10-19T12:00:00Z',
    expires_at: '2026-10-19T12:10:00Z',
    data_freshness: 'LIVE',
    last_successful_at: '2026-10-19T12:00:00Z',
    azure_post_count: 4,
    ...overrides,
  }
}

describe('deriveDataFreshness — backend data_freshness hint', () => {
  it('returns live for LIVE hint', () => {
    const result = deriveDataFreshness(envelope({ data_freshness: 'LIVE' }))
    expect(result.kind).toBe('live')
    expect(result.description).toBe('Live')
    expect(result.lastSuccessfulAt).toBe('2026-10-19T12:00:00Z')
  })

  it('returns cached for CACHED hint', () => {
    const result = deriveDataFreshness(envelope({ data_freshness: 'CACHED' }))
    expect(result.kind).toBe('cached')
    expect(result.description).toContain('Cached')
  })

  it('returns stale with last-successful timestamp for STALE hint', () => {
    const result = deriveDataFreshness(envelope({ data_freshness: 'STALE' }))
    expect(result.kind).toBe('stale')
    expect(result.description).toContain('Stale')
    expect(result.description).toContain('last successful')
    expect(result.lastSuccessfulAt).toBe('2026-10-19T12:00:00Z')
  })

  it('returns unavailable for UNAVAILABLE hint', () => {
    const result = deriveDataFreshness(
      envelope({ data_freshness: 'UNAVAILABLE', report: null as unknown as CostReportResponse['report'] }),
    )
    expect(result.kind).toBe('unavailable')
    expect(result.description).toContain('Azure refresh temporarily unavailable')
  })

  it('defaults to live when data_freshness is missing (legacy AWS / old fixtures)', () => {
    const legacy = envelope()
    delete (legacy as Partial<CostReportResponse>).data_freshness
    delete (legacy as Partial<CostReportResponse>).last_successful_at
    const result = deriveDataFreshness(legacy)
    expect(result.kind).toBe('live')
  })

  it('returns unavailable for null envelope', () => {
    const result = deriveDataFreshness(null)
    expect(result.kind).toBe('unavailable')
    expect(result.lastSuccessfulAt).toBeNull()
  })
})

describe('Phase 7D.3.2 + 7D.3.3 — completeness preserved', () => {
  it('throttled-zero (PARTIAL_SUCCESS, successful=0) renders unavailable, NOT $0.00', () => {
    const report = baseReport({
      status: 'PARTIAL_SUCCESS',
      successful_accounts: 0,
      failed_accounts: 2,
      total_cost: '0.00',
    })
    const completeness = deriveCostCompleteness(report)
    expect(completeness.kind).toBe('unavailable')
  })

  it('true zero (SUCCESS, total_cost=0, successful>0) renders success', () => {
    const report = baseReport({
      status: 'SUCCESS',
      successful_accounts: 1,
      failed_accounts: 0,
      total_cost: '0.00',
    })
    const completeness = deriveCostCompleteness(report)
    expect(completeness.kind).toBe('success')
  })

  it('partial (PARTIAL_SUCCESS, successful>0) renders partial', () => {
    const report = baseReport({
      status: 'PARTIAL_SUCCESS',
      successful_accounts: 1,
      failed_accounts: 1,
      total_cost: '50.00',
    })
    const completeness = deriveCostCompleteness(report)
    expect(completeness.kind).toBe('partial')
  })
})

describe('costDomainSnapshot — preserves hasData contract', () => {
  it('returns hasData=true for a successful LIVE response', () => {
    const snap = costDomainSnapshot(envelope(), 'success', null)
    expect(snap.hasData).toBe(true)
  })

  it('returns hasData=true for a STALE LKG response (completeness != unavailable)', () => {
    const snap = costDomainSnapshot(
      envelope({
        data_freshness: 'STALE',
        cache_status: 'STALE',
      }),
      'success',
      new Error('throttled'),
    )
    expect(snap.hasData).toBe(true)
  })

  it('returns hasData=false for a throttled-zero response', () => {
    const report = baseReport({
      status: 'PARTIAL_SUCCESS',
      successful_accounts: 0,
      failed_accounts: 2,
    })
    const snap = costDomainSnapshot(
      envelope({ report, data_freshness: 'UNAVAILABLE', cache_status: 'MISS' }),
      'error',
      new Error('throttled'),
    )
    expect(snap.hasData).toBe(false)
  })
})

describe('deriveDashboardReadiness — freshness never produces contradictory labels', () => {
  it('stale LKG → live (hasData=true keeps the hero usable)', () => {
    const snap = costDomainSnapshot(
      envelope({ data_freshness: 'STALE', cache_status: 'STALE' }),
      'success',
      new Error('throttled'),
    )
    expect(deriveDashboardReadiness([snap])).toBe('live')
  })

  it('throttled-zero → unavailable', () => {
    const report = baseReport({
      status: 'PARTIAL_SUCCESS',
      successful_accounts: 0,
      failed_accounts: 2,
    })
    const snap = costDomainSnapshot(
      envelope({ report, data_freshness: 'UNAVAILABLE' }),
      'error',
      new Error('throttled'),
    )
    expect(deriveDashboardReadiness([snap])).toBe('unavailable')
  })
})
