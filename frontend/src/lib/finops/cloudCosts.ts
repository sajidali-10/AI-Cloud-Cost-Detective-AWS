// Phase 7C.2B — provider-neutral Cloud cost service.
//
// Calls the canonical `/api/cloud/costs` route added in Phase 7C.2B.
// The same envelope is returned for AWS and Azure so the front-end
// can render cost dashboards from a single shape.
//
// AWS-specific caching is intentionally preserved via the legacy
// `/api/aws/costs` route — this helper covers the cross-cloud
// surface only.

import { apiFetch } from '../api'
import type { BillingPeriod } from '../../types/finops'
import { ALLOWED_LOOKBACK_DAYS } from '../../types/finops'
import { canonicalCacheSegment, canonicalAccountParam } from './subscriptionSelection'

export type CacheProvider = 'aws' | 'azure' | 'rackspace'

export interface CloudAccountShare {
  account_id: string
  display_name: string
  amount: string
  currency?: string | null
}

export interface CloudAccountError {
  account_id: string
  display_name: string
  code: string
  message: string
}

export interface CloudCostReport {
  provider: 'AWS' | 'AZURE' | 'RACKSPACE'
  scope: string
  scope_kind: 'all' | 'single'
  status: string
  currency_status: 'OK' | 'MIXED_CURRENCY' | 'EMPTY' | 'NO_DATA'
  account_id?: string | null
  account_display_name?: string | null
  period: { start: string; end: string; days: number }
  previous_period: { start: string; end: string; days: number }
  currency: string
  total_cost: string
  previous_period_cost: string
  change_amount: string
  change_percent: string | null
  estimated: boolean
  daily_trend: Array<{ date: string; amount: string; unit: string }>
  by_service: Array<{ service: string; amount: string; unit: string }>
  by_region: Array<{ region: string; amount: string; unit: string }>
  by_account: CloudAccountShare[]
  account_errors: CloudAccountError[]
  /** Phase 7D.3.2 — when the backend (or a test fixture) populates
   *  these explicitly, the frontend MUST trust them.  The
   *  ``by_account``-based derivation below is only a fallback so
   *  older test fixtures that omit the counts continue to work. */
  successful_accounts?: number | null
  failed_accounts?: number | null
  source: string
}

export interface CloudCostReportResponse {
  report: CloudCostReport
  cache_status: 'HIT' | 'MISS' | 'REFRESHED' | 'STALE'
  cached_at: string | null
  expires_at: string | null
  /** Phase 7D.3.3 — coarse freshness signal the frontend renders.
   *  ``undefined`` for AWS responses (legacy) and for older
   *  Azure fixtures — treated as ``'LIVE'`` by the readiness
   *  derivation. */
  data_freshness?: 'LIVE' | 'CACHED' | 'STALE' | 'UNAVAILABLE' | null
  /** Phase 7D.3.3 — timestamp of the most recent successful
   *  upstream call.  ``null`` for AWS responses or when the
   *  cache has never seen a success. */
  last_successful_at?: string | null
  /** Phase 7D.3.3 — number of Azure Cost Management POSTs in
   *  the last successful refresh.  ``null`` for AWS responses. */
  azure_post_count?: number | null
}

export function buildCloudCostsCacheKey(
  period: BillingPeriod,
  region: string | null,
  accountId: string | null,
  provider: CacheProvider = 'aws',
): string {
  const acct = canonicalCacheSegment(accountId, provider)
  const providerSeg = provider
  if (period.kind === 'month') {
    return `${providerSeg}:cloud:costs:month:${period.month}:${region ?? 'all'}:${acct}`
  }
  return `${providerSeg}:cloud:costs:days:${period.days}:${region ?? 'all'}:${acct}`
}

export async function fetchCloudCostReport(
  period: BillingPeriod,
  options: {
    provider: 'AWS' | 'AZURE' | 'RACKSPACE'
    region?: string | null
    accountId?: string | null
  },
): Promise<CloudCostReportResponse> {
  const params = new URLSearchParams()
  params.set('provider', options.provider)
  if (period.kind === 'month') {
    params.set('month', period.month)
  } else {
    if (!ALLOWED_LOOKBACK_DAYS.includes(period.days)) {
      throw new Error(`fetchCloudCostReport: days=${period.days} is not allowed`)
    }
    params.set('days', String(period.days))
  }
  if (options.region) params.set('region', options.region)
  const canonicalAccount = canonicalAccountParam(options.accountId, options.provider)
  if (canonicalAccount) params.set('account_id', canonicalAccount)
  return apiFetch<CloudCostReportResponse>(`/api/cloud/costs?${params.toString()}`, {
    timeoutMs: 30_000,
  })
}

/**
 * Adapt a CloudCostReportResponse to the same shape the existing
 * dashboard / costs components consume (the AWS CostReport shape).
 *
 * This is a thin shim so the shared UI does not need provider-
 * specific branching: a single set of components handles AWS
 * and Azure responses after adaptation.
 */
export function adaptCloudCostReport(
  response: CloudCostReportResponse,
): import('../../types/finops').CostReportResponse {
  const r = response.report
  // Hotfix #2 — forward the cost-completeness fields so the UI can
  // distinguish a genuine successful zero from a transient throttle.
  // The canonical Azure report surfaces the per-subscription picture
  // through TOP-LEVEL fields: ``by_account`` (one entry per
  // subscription, including failed ones whose amount is zero) and
  // ``account_errors`` (the failed / throttled subscriptions).  A
  // snapshot is "failed" iff it appears in ``account_errors``.  The
  // envelope's ``status`` (``SUCCESS`` / ``PARTIAL_SUCCESS`` /
  // ``ERROR``), ``currency_status`` and the counts are re-exposed at
  // the CostReport top level so the shared components can render
  // Unavailable / Partial without reaching into provider internals.
  const accountErrors = Array.isArray(r.account_errors) ? r.account_errors : []
  const subscriptionErrors = accountErrors.map((e) => ({
    subscription_id: e.account_id,
    display_name: e.display_name,
    code: e.code,
    message: e.message,
  }))
  // Phase 7D.3.2 — prefer the backend-emitted counts (they are
  // authoritative).  Only fall back to the by_account-derived
  // counts when the backend has not populated the field (older
  // fixtures, providers that don't surface per-subscription
  // counts).  ``r`` is typed loosely here because the canonical
  // ``CloudCostReport`` schema doesn't yet declare the fields,
  // but the production envelope already carries them.
  const explicitFailed =
    typeof (r as { failed_accounts?: unknown }).failed_accounts === 'number'
      ? ((r as { failed_accounts: number }).failed_accounts)
      : null
  const explicitSuccessful =
    typeof (r as { successful_accounts?: unknown }).successful_accounts === 'number'
      ? ((r as { successful_accounts: number }).successful_accounts)
      : null
  const failedAccounts =
    explicitFailed !== null ? explicitFailed : subscriptionErrors.length
  const subscribedCount = Array.isArray(r.by_account) ? r.by_account.length : 0
  const successfulAccounts =
    explicitSuccessful !== null
      ? explicitSuccessful
      : Math.max(0, subscribedCount - failedAccounts)
  return {
    report: {
      account_id: r.account_id ?? '',
      period: r.period,
      previous_period: r.previous_period,
      currency: r.currency,
      total_cost: r.total_cost,
      // Phase 7D.3.3 (correctness) — propagate ``null`` for
      // previous_period_cost / change_amount / change_percent
      // when the previous-period fetch failed.  The UI renders
      // "—" / "Previous period unavailable" instead of fabricating
      // a percentage against an invented zero baseline.
      previous_period_cost: (r.previous_period_cost as string | null) ?? null,
      change_amount: (r.change_amount as string | null) ?? null,
      change_percent: r.change_percent ?? null,
      previous_period_unavailable:
        typeof (r as unknown as { previous_period_unavailable?: unknown }).previous_period_unavailable === 'boolean'
          ? ((r as unknown as { previous_period_unavailable: boolean }).previous_period_unavailable)
          : undefined,
      estimated: r.estimated,
      daily_trend: r.daily_trend,
      by_service: r.by_service,
      by_region: r.by_region.map((entry) => ({
        region: entry.region,
        amount: entry.amount,
        unit: entry.unit,
      })),
      source: (r.source === 'AZURE_COST_MANAGEMENT'
        ? 'AZURE_COST_MANAGEMENT'
        : r.source === 'RACKSPACE_BILLING'
          ? 'RACKSPACE_BILLING'
          : 'AWS_COST_EXPLORER') as
        'AWS_COST_EXPLORER' | 'AZURE_COST_MANAGEMENT' | 'RACKSPACE_BILLING',
      // Hotfix #2 — propagate completeness semantics.
      status: r.status,
      currency_status: r.currency_status,
      successful_accounts: successfulAccounts,
      failed_accounts: failedAccounts,
      subscription_errors: subscriptionErrors,
    },
    cache_status: response.cache_status,
    cached_at: response.cached_at,
    expires_at: response.expires_at,
    // Phase 7D.3.3 — propagate cache-freshness metadata so the
    // readiness derivation can render "Cached" / "Stale" labels
    // and the KPI tiles can show "Stale · last successful <time>".
    data_freshness: response.data_freshness ?? null,
    last_successful_at: response.last_successful_at ?? null,
    azure_post_count: response.azure_post_count ?? null,
  }
}
