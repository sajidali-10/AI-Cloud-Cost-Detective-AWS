// Phase 6B — CloudWatch utilization service.
//
// Phase 7B: optional `accountId` selects a specific registry row.
//
// Phase 7C.1: provider scoping is prepended as a leading segment.

import { apiFetch } from '../api'
import type { UtilizationRequestBody, UtilizationResponse } from '../../types/finops'

export type CacheProvider = 'aws' | 'azure' | 'rackspace'

export function buildUtilizationCacheKey(
  body: UtilizationRequestBody,
  provider: CacheProvider = 'aws',
): string {
  return `${provider}:utilization:${body.region}:${body.lookback_days}:${(body.resource_types ?? []).slice().sort().join(',')}:${body.account_id ?? 'all'}`
}

export async function fetchUtilization(body: UtilizationRequestBody): Promise<UtilizationResponse> {
  return apiFetch<UtilizationResponse>('/api/aws/utilization', {
    method: 'POST',
    json: body,
    timeoutMs: 25_000,
  })
}
