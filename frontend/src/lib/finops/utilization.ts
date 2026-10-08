// Phase 6B — CloudWatch utilization service.

import { apiFetch } from '../api'
import type { UtilizationRequestBody, UtilizationResponse } from '../../types/finops'

export function buildUtilizationCacheKey(body: UtilizationRequestBody): string {
  return `utilization:${body.region}:${body.lookback_days}:${(body.resource_types ?? []).slice().sort().join(',')}`
}

export async function fetchUtilization(body: UtilizationRequestBody): Promise<UtilizationResponse> {
  return apiFetch<UtilizationResponse>('/api/aws/utilization', {
    method: 'POST',
    json: body,
    timeoutMs: 25_000,
  })
}
