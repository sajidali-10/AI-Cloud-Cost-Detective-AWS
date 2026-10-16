// Phase 7C.2D — provider-neutral Cloud recommendations service.
//
// Calls the canonical `/api/cloud/recommendations` route added in
// Phase 7C.2D.  Azure recommendation data flows through the same
// shared envelope the AWS legacy routes use so the UI consumes one
// shape.

import { apiFetch } from '../api'

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
  // Phase 7D.1 — generic columns promoted from extensions so the
  // same fields render for AWS and Azure without provider checks.
  category?: string | null
  impact?: string | null
  description?: string | null
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

// Phase 7D.1 — provider-neutral failure shape (AWS account or
// Azure subscription).
export interface CloudRecommendationAccountError {
  account_id: string
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
  // Phase 7D.1 — provider-neutral savings pair.  AWS populates a
  // MONTHLY figure here (its annual field stays null); Azure keeps
  // both populated.
  potential_savings?: string | null
  savings_period?: 'MONTHLY' | 'ANNUAL' | null
  savings_by_currency: CloudRecommendationCurrencyTotal[]
  by_impact: CloudRecommendationGroupCount[]
  by_resource_type: CloudRecommendationGroupCount[]
  by_subscription: CloudRecommendationGroupCount[]
  by_resource_group: CloudRecommendationGroupCount[]
  by_type: CloudRecommendationGroupCount[]
  by_subcategory: CloudRecommendationGroupCount[]
  subscription_errors: CloudRecommendationSubscriptionError[]
  // Phase 7D.1 — provider-neutral aliases (AWS accounts / Azure
  // subscriptions) plus generic source / action breakdowns.
  by_account?: CloudRecommendationGroupCount[]
  by_source?: CloudRecommendationGroupCount[]
  by_action?: CloudRecommendationGroupCount[]
  account_errors?: CloudRecommendationAccountError[]
  source: string
}

export interface CloudRecommendationListResponse {
  summary: CloudRecommendationSummaryEnvelope
  recommendations: CloudRecommendationItem[]
}

export function buildCloudRecommendationsCacheKey(
  accountId: string | null,
  provider: CacheProvider = 'aws',
  region: string | null = null,
  days: number | null = null,
): string {
  const acct = accountId ?? 'all'
  const providerSeg = provider
  // Phase 7D.1 — region + lookback-window are part of the
  // recommendation identity for AWS.  When both are omitted the key
  // is byte-for-byte the pre-7D.1 string so existing Azure callers
  // and tests are unaffected.
  const base = `${providerSeg}:cloud:recommendations:${acct}`
  if (region === null && days === null) return base
  return `${base}:${region ?? 'all'}:${days === null ? 'default' : String(days)}`
}

export interface FetchCloudRecommendationsOptions {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  accountId?: string | null
  /** Optional region scope (AWS). Azure ignores it. */
  region?: string | null
  /** Optional lookback window in days (AWS). Azure ignores it. */
  days?: number | null
}

export async function fetchCloudRecommendations(
  options: FetchCloudRecommendationsOptions,
): Promise<CloudRecommendationListResponse> {
  const params = new URLSearchParams()
  params.set('provider', options.provider)
  if (options.accountId) params.set('account_id', options.accountId)
  if (options.region) params.set('region', options.region)
  if (options.days != null) params.set('days', String(options.days))
  return apiFetch<CloudRecommendationListResponse>(
    `/api/cloud/recommendations?${params.toString()}`,
    { timeoutMs: 60_000 },
  )
}
