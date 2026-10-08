// Phase 6B — Cost Explorer service.
//
// Wraps `/api/aws/costs?days=` so components never compose URL
// strings inline.  Only the four backend-allowed lookback windows
// are accepted (7, 30, 60, 90) — the backend enforces this with a
// sanitized 422, but we also enforce it client-side to avoid
// generating an obvious 422 round-trip on every invalid period.

import { apiFetch } from '../api'
import type { CostReportResponse, LookbackDays } from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS } from '../../types/finops'

export function buildCostsCacheKey(days: LookbackDays, region: string | null): string {
  return `costs:${days}:${region ?? 'all'}`
}

export async function fetchCostReport(
  days: LookbackDays,
  options: { region?: string | null } = {},
): Promise<CostReportResponse> {
  if (!ALLOWED_LOOKBACK_DAYS.includes(days)) {
    throw new Error(`fetchCostReport: days=${days} is not allowed`)
  }
  const params = new URLSearchParams()
  params.set('days', String(days))
  if (options.region) params.set('region', options.region)
  return apiFetch<CostReportResponse>(`/api/aws/costs?${params.toString()}`, {
    timeoutMs: 20_000,
  })
}
