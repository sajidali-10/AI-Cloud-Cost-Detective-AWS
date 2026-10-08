// Phase 6B — Resource inventory service.

import { apiFetch } from '../api'
import type { ResourcesResponse } from '../../types/finops'

export function buildResourcesCacheKey(region: string | null): string {
  return `resources:${region ?? 'default'}`
}

export async function fetchResources(region: string | null): Promise<ResourcesResponse> {
  const params = new URLSearchParams()
  if (region) params.set('region', region)
  const path = params.toString() ? `/api/aws/resources?${params.toString()}` : '/api/aws/resources'
  return apiFetch<ResourcesResponse>(path, { timeoutMs: 20_000 })
}
