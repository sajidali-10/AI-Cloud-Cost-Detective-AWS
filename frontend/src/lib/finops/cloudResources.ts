// Phase 7C.2C — provider-neutral Cloud resource service.
//
// Calls the canonical `/api/cloud/resources` route added in Phase
// 7C.2C.  The same envelope is returned for AWS and Azure so the
// shared UI can render either provider without provider-specific
// branching.

import { apiFetch } from '../api'
import { canonicalCacheSegment, canonicalAccountParam } from './subscriptionSelection'

export type CacheProvider = 'aws' | 'azure' | 'rackspace'

export interface CloudResourceItem {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  account_id: string
  region: string | null
  resource_type: string
  resource_id: string
  service: string
  name: string | null
  state: string | null
  composite_id: string
  metadata?: Record<string, unknown>
}

export interface CloudResourceGroupCount {
  key: string
  count: number
}

export interface CloudResourceSubscriptionShare {
  subscription_id: string
  display_name: string
  count: number
}

export interface CloudResourceSubscriptionError {
  subscription_id: string
  display_name: string
  code: string
  message: string
}

export interface CloudResourceSummaryEnvelope {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  scope: string
  scope_kind: 'all' | 'single'
  status: 'SUCCESS' | 'PARTIAL_SUCCESS' | 'NO_DATA' | 'ERROR' | string
  total_resources: number
  by_type: CloudResourceGroupCount[]
  by_location: CloudResourceGroupCount[]
  by_resource_group: CloudResourceGroupCount[]
  by_subscription: CloudResourceSubscriptionShare[]
  subscription_errors: CloudResourceSubscriptionError[]
  source: string
}

export interface CloudResourceListResponse {
  summary: CloudResourceSummaryEnvelope
  resources: CloudResourceItem[]
}

export function buildCloudResourcesCacheKey(
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  const acct = canonicalCacheSegment(accountId, provider)
  const providerSeg = provider
  return `${providerSeg}:cloud:resources:${acct}`
}

export async function fetchCloudResources(
  options: {
    provider: 'AWS' | 'AZURE' | 'RACKSPACE'
    accountId?: string | null
  },
): Promise<CloudResourceListResponse> {
  const params = new URLSearchParams()
  params.set('provider', options.provider)
  const canonicalAccount = canonicalAccountParam(options.accountId, options.provider)
  if (canonicalAccount) params.set('account_id', canonicalAccount)
  return apiFetch<CloudResourceListResponse>(
    `/api/cloud/resources?${params.toString()}`,
    { timeoutMs: 60_000 },
  )
}
