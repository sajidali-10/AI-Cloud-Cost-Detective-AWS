// Phase 7C.2D — provider-neutral Cloud recommendations service.
//
// Calls the canonical `/api/cloud/recommendations` route added in
// Phase 7C.2D.  Azure recommendation data flows through the same
// shared envelope the AWS legacy routes use so the UI consumes one
// shape.

import { apiFetch } from '../api'
import { canonicalCacheSegment, canonicalAccountParam } from './subscriptionSelection'

export type CacheProvider = 'aws' | 'azure' | 'rackspace'

export interface CloudRecommendationItem {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  account_id: string | null
  region: string | null
  recommendation_id: string
  resource_id: string
  resource_type: string
  service: string
  action: string
  title: string
  estimated_savings: string | null
  currency: string | null
  source: string
  extensions?: Record<string, unknown>
}

export interface CloudRecommendationGroupCount {
  key: string
  count: number
  // Optional subscription_id for the by_subscription breakdown so
  // the UI can mask the GUID before rendering.
  subscription_id?: string
}

export interface CloudRecommendationCurrencyTotal {
  currency: string
  amount: string
}

export interface CloudRecommendationSubscriptionError {
  subscription_id: string
  display_name: string
  code: string
  message: string
}

export interface CloudRecommendationSummaryEnvelope {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  scope: string
  scope_kind: 'all' | 'single'
  status: 'SUCCESS' | 'PARTIAL_SUCCESS' | 'NO_DATA' | 'ERROR' | string
  savings_status: 'OK' | 'MIXED_CURRENCY' | 'EMPTY' | string
  currency: string | null
  total_recommendations: number
  recommendations_with_savings: number
  recommendations_without_savings: number
  potential_annual_savings: string | null
  savings_by_currency: CloudRecommendationCurrencyTotal[]
  by_impact: CloudRecommendationGroupCount[]
  by_resource_type: CloudRecommendationGroupCount[]
  by_subscription: CloudRecommendationGroupCount[]
  by_resource_group: CloudRecommendationGroupCount[]
  by_type: CloudRecommendationGroupCount[]
  by_subcategory: CloudRecommendationGroupCount[]
  subscription_errors: CloudRecommendationSubscriptionError[]
  source: string
}

export interface CloudRecommendationListResponse {
  summary: CloudRecommendationSummaryEnvelope
  recommendations: CloudRecommendationItem[]
}

export function buildCloudRecommendationsCacheKey(
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  const acct = canonicalCacheSegment(accountId, provider)
  const providerSeg = provider
  return `${providerSeg}:cloud:recommendations:${acct}`
}

export async function fetchCloudRecommendations(
  options: {
    provider: 'AWS' | 'AZURE' | 'RACKSPACE'
    accountId?: string | null
  },
): Promise<CloudRecommendationListResponse> {
  const params = new URLSearchParams()
  params.set('provider', options.provider)
  const canonicalAccount = canonicalAccountParam(options.accountId, options.provider)
  if (canonicalAccount) params.set('account_id', canonicalAccount)
  return apiFetch<CloudRecommendationListResponse>(
    `/api/cloud/recommendations?${params.toString()}`,
    { timeoutMs: 60_000 },
  )
}
