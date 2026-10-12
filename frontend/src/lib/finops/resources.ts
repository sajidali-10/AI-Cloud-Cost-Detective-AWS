// Phase 6B — Resource inventory service.
//
// Phase 7B: optional `accountId` selects a specific registry row.

import { apiFetch } from '../api'
import type { ResourcesResponse } from '../../types/finops'

export function buildResourcesCacheKey(
  region: string | null,
  accountId: string | null,
): string {
  return `resources:${region ?? 'default'}:${accountId ?? 'all'}`
}

export async function fetchResources(
  region: string | null,
  options: { accountId?: string | null } = {},
): Promise<ResourcesResponse> {
  const params = new URLSearchParams()
  if (region) params.set('region', region)
  if (options.accountId) params.set('account_id', options.accountId)
  const path = params.toString() ? `/api/aws/resources?${params.toString()}` : '/api/aws/resources'
  return apiFetch<ResourcesResponse>(path, { timeoutMs: 20_000 })
}
