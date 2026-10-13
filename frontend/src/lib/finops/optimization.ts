// Phase 7A — Optimization service.
//
// The backend `/api/aws/optimization/*` endpoints still take
// `?days=7|30|60|90` (Phase 3 invariant).  The frontend takes a
// calendar-month primary ``BillingPeriod`` and derives the
// lookback window:
//   * month mode → ``30`` (closest to MTD)
//   * days mode → pass-through
//
// Phase 7B: optional `accountId` selects a specific registry row.
//
// Phase 7C.1: provider scoping is appended as a leading segment
// so a provider switch cannot accidentally satisfy a stale cache
// entry for a different provider.

import { apiFetch } from '../api'
import type {
  BillingPeriod,
  CapabilitiesResponse,
  OptimizationSummaryResponse,
  RecommendationsResponse,
} from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../../types/finops'

export type CacheProvider = 'aws' | 'azure' | 'rackspace'

export function buildCapabilitiesCacheKey(
  region: string | null,
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  return `${provider}:optimization:caps:${region ?? 'default'}:${accountId ?? 'all'}`
}

export function buildRecommendationsCacheKey(
  period: BillingPeriod,
  region: string | null,
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  const acct = accountId ?? 'all'
  if (period.kind === 'month') {
    return `${provider}:optimization:recs:month:${period.month}:${region ?? 'all'}:${acct}`
  }
  return `${provider}:optimization:recs:${period.days}:${region ?? 'all'}:${acct}`
}

export function buildSummaryCacheKey(
  period: BillingPeriod,
  region: string | null,
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  const acct = accountId ?? 'all'
  if (period.kind === 'month') {
    return `${provider}:optimization:summary:month:${period.month}:${region ?? 'all'}:${acct}`
  }
  return `${provider}:optimization:summary:${period.days}:${region ?? 'all'}:${acct}`
}

function toBackendDays(period: BillingPeriod): LookbackDays {
  if (period.kind === 'days') return period.days
  return 30
}

function _appendAccount(
  params: URLSearchParams,
  accountId: string | null,
): URLSearchParams {
  if (accountId) params.set('account_id', accountId)
  return params
}

export async function fetchCapabilities(
  region: string | null,
  options: { accountId?: string | null } = {},
): Promise<CapabilitiesResponse> {
  const params = _appendAccount(new URLSearchParams(), options.accountId ?? null)
  if (region) params.set('region', region)
  const path = params.toString()
    ? `/api/aws/optimization/capabilities?${params.toString()}`
    : '/api/aws/optimization/capabilities'
  return apiFetch<CapabilitiesResponse>(path, { timeoutMs: 15_000 })
}

export async function fetchRecommendations(
  period: BillingPeriod,
  region: string | null,
  options: { accountId?: string | null } = {},
): Promise<RecommendationsResponse> {
  const days = toBackendDays(period)
  if (!ALLOWED_LOOKBACK_DAYS.includes(days)) {
    throw new Error(`fetchRecommendations: days=${days} is not allowed`)
  }
  const params = _appendAccount(new URLSearchParams(), options.accountId ?? null)
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
  options: { accountId?: string | null } = {},
): Promise<OptimizationSummaryResponse> {
  const days = toBackendDays(period)
  if (!ALLOWED_LOOKBACK_DAYS.includes(days)) {
    throw new Error(`fetchOptimizationSummary: days=${days} is not allowed`)
  }
  const params = _appendAccount(new URLSearchParams(), options.accountId ?? null)
  params.set('days', String(days))
  if (region) params.set('region', region)
  return apiFetch<OptimizationSummaryResponse>(
    `/api/aws/optimization/summary?${params.toString()}`,
    { timeoutMs: 25_000 },
  )
}
