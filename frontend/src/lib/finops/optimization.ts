// Phase 6B — Optimization service.

import { apiFetch } from '../api'
import type {
  CapabilitiesResponse,
  LookbackDays,
  OptimizationSummaryResponse,
  RecommendationsResponse,
} from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS } from '../../types/finops'

export function buildCapabilitiesCacheKey(region: string | null): string {
  return `optimization:caps:${region ?? 'default'}`
}

export function buildRecommendationsCacheKey(days: LookbackDays, region: string | null): string {
  return `optimization:recs:${days}:${region ?? 'all'}`
}

export function buildSummaryCacheKey(days: LookbackDays, region: string | null): string {
  return `optimization:summary:${days}:${region ?? 'all'}`
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
  days: LookbackDays,
  region: string | null,
): Promise<RecommendationsResponse> {
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
  days: LookbackDays,
  region: string | null,
): Promise<OptimizationSummaryResponse> {
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
