// Phase 7A — Optimization service.
//
// The backend `/api/aws/optimization/*` endpoints still take
// `?days=7|30|60|90` (Phase 3 invariant).  The frontend takes a
// calendar-month primary ``BillingPeriod`` and derives the
// lookback window:
//   * month mode → ``30`` (closest to MTD)
//   * days mode → pass-through
//
// Deterministic recommendations and the cache invalidation
// contract are unaffected by this mapping.  The capability row
// surfaces operator-facing guidance regardless of the period.

import { apiFetch } from '../api'
import type {
  BillingPeriod,
  CapabilitiesResponse,
  OptimizationSummaryResponse,
  RecommendationsResponse,
} from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../../types/finops'

export function buildCapabilitiesCacheKey(region: string | null): string {
  return `optimization:caps:${region ?? 'default'}`
}

export function buildRecommendationsCacheKey(period: BillingPeriod, region: string | null): string {
  if (period.kind === 'month') {
    return `optimization:recs:month:${period.month}:${region ?? 'all'}`
  }
  return `optimization:recs:${period.days}:${region ?? 'all'}`
}

export function buildSummaryCacheKey(period: BillingPeriod, region: string | null): string {
  if (period.kind === 'month') {
    return `optimization:summary:month:${period.month}:${region ?? 'all'}`
  }
  return `optimization:summary:${period.days}:${region ?? 'all'}`
}

function toBackendDays(period: BillingPeriod): LookbackDays {
  if (period.kind === 'days') return period.days
  // Calendar-month requests don't change the optimization lookback
  // window; map to 30 (closest to MTD for any month).
  return 30
}

export async function fetchCapabilities(region: string | null): Promise<CapabilitiesResponse> {
  const params = new URLSearchParams()
  if (region) params.set('region', region)
  const path = params.toString()
    ? `/api/aws/optimization/capabilities?${params.toString()}`
    : '/api/aws/optimization/capabilities'
  return apiFetch<CapabilitiesResponse>(path, { timeoutMs: 15_000 })
}

export async function fetchRecommendations(
  period: BillingPeriod,
  region: string | null,
): Promise<RecommendationsResponse> {
  const days = toBackendDays(period)
  if (!ALLOWED_LOOKBACK_DAYS.includes(days)) {
    throw new Error(`fetchRecommendations: days=${days} is not allowed`)
  }
  const params = new URLSearchParams()
  params.set('days', String(days))
  if (region) params.set('region', region)
  return apiFetch<RecommendationsResponse>(
    `/api/aws/optimization/recommendations?${params.toString()}`,
    { timeoutMs: 25_000 },
  )
}

export async function fetchOptimizationSummary(
  period: BillingPeriod,
  region: string | null,
): Promise<OptimizationSummaryResponse> {
  const days = toBackendDays(period)
  if (!ALLOWED_LOOKBACK_DAYS.includes(days)) {
    throw new Error(`fetchOptimizationSummary: days=${days} is not allowed`)
  }
  const params = new URLSearchParams()
  params.set('days', String(days))
  if (region) params.set('region', region)
  return apiFetch<OptimizationSummaryResponse>(
    `/api/aws/optimization/summary?${params.toString()}`,
    { timeoutMs: 25_000 },
  )
}
