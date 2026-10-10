// Phase 7A — Cost Explorer service.
//
// Wraps `/api/aws/costs` for two period kinds:
//
//   * `BillingPeriod = { kind: 'month', month }` → `?month=YYYY-MM`
//   * `BillingPeriod = { kind: 'days', days }`  → `?days=7|30|60|90`
//
// The calendar-month path is the Phase 7A primary; the rolling-
// lookback path is preserved for backward compatibility with Phase 6
// regression tests and any external callers still using `?days=`.

import { apiFetch } from '../api'
import type {
  BillingPeriod,
  CostReportResponse,
} from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS } from '../../types/finops'

export function buildCostsCacheKey(period: BillingPeriod, region: string | null): string {
  if (period.kind === 'month') {
    return `costs:month:${period.month}:${region ?? 'all'}`
  }
  return `costs:days:${period.days}:${region ?? 'all'}`
}

export async function fetchCostReport(
  period: BillingPeriod,
  options: { region?: string | null } = {},
): Promise<CostReportResponse> {
  const params = new URLSearchParams()
  if (period.kind === 'month') {
    params.set('month', period.month)
  } else {
    if (!ALLOWED_LOOKBACK_DAYS.includes(period.days)) {
      throw new Error(`fetchCostReport: days=${period.days} is not allowed`)
    }
    params.set('days', String(period.days))
  }
  if (options.region) params.set('region', options.region)
  return apiFetch<CostReportResponse>(`/api/aws/costs?${params.toString()}`, {
    timeoutMs: 20_000,
  })
}
