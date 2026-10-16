// Phase 7C.2D — Azure Advisor recommendations view.
//
// Renders Azure Advisor Cost recommendations from the canonical
// ``/api/cloud/recommendations`` endpoint.  The component reuses
// the same shared UI primitives as the AWS optimization view
// (``PageHeader``, ``SectionCard``, ``MetricCard``, ``DataTable``,
// ``StatusBadge``, ``PartialWarning``, ``ErrorState``,
// ``RefreshButton``).
//
// Differences from the AWS optimization view:
//
// * Data comes from ``fetchCloudRecommendations`` rather than
//   ``fetchRecommendations`` so the response shape is the canonical
//   provider-neutral envelope (Phase 7C.2D).
// * The summary breakdown is provider-agnostic:
//   by_impact / by_resource_type / by_resource_group /
//   by_subscription / by_type / by_subcategory.
// * Savings display handles three distinct states:
//   - ``OK`` — single currency, ``potential_annual_savings`` is a
//     single Decimal.
//   - ``MIXED_CURRENCY`` — multiple currencies; the total is NOT
//     shown, only per-currency totals.
//   - ``EMPTY`` — no recommendations carried a savings value.

import { useEffect, useMemo, useRef } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { RefreshButton } from '../components/RefreshButton'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { PartialWarning } from '../components/PartialWarning'
import { useFinopsQuery } from '../lib/finops/store'
import { invalidateFinopsCache } from '../lib/finops/store'
import {
  fetchCloudRecommendations,
  buildCloudRecommendationsCacheKey,
  type CloudRecommendationItem,
  type CloudRecommendationListResponse,
} from '../lib/finops/cloudRecommendations'
import { useAccountContext } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import {
  formatLocalTime,
  maskAccountId,
  parseDecimal,
} from '../lib/format'

interface AzureAdvisorViewProps {
  selection: string | null
  cacheProvider: 'aws' | 'azure' | 'rackspace'
  providerVersion: number
}

function recommendationTitle(rec: CloudRecommendationItem): string {
  const ext = (rec.extensions ?? {}) as Record<string, unknown>
  const sub = typeof ext['subcategory'] === 'string' ? ext['subcategory'] : null
  if (sub) return sub
  return rec.title || rec.action
}

function resourceNameFromMetadata(rec: CloudRecommendationItem): string {
  const ext = (rec.extensions ?? {}) as Record<string, unknown>
  const name = ext['resource_name']
  return typeof name === 'string' && name ? name : '—'
}

function recommendationImpact(rec: CloudRecommendationItem): string | null {
  const ext = (rec.extensions ?? {}) as Record<string, unknown>
  const impact = ext['impact']
  return typeof impact === 'string' ? impact : null
}

function recommendationLastUpdated(rec: CloudRecommendationItem): string | null {
  const ext = (rec.extensions ?? {}) as Record<string, unknown>
  const lu = ext['last_updated']
  return typeof lu === 'string' ? lu : null
}

function recommendationSubcategory(rec: CloudRecommendationItem): string | null {
  const ext = (rec.extensions ?? {}) as Record<string, unknown>
  const sub = ext['subcategory']
  return typeof sub === 'string' ? sub : null
}

function formatAnnualSavings(value: string | null, currency: string | null): string {
  if (value === null) return '—'
  const n = parseDecimal(value)
  if (n === null) return '—'
  if (!currency) return n.toString()
  return `${currency} ${n.toString()}`
}

function statusTone(status: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  if (status === 'SUCCESS') return 'success'
  if (status === 'PARTIAL_SUCCESS') return 'warning'
  if (status === 'NO_DATA') return 'neutral'
  if (status === 'ERROR') return 'danger'
  return 'info'
}

function impactTone(impact: string | null): 'success' | 'warning' | 'danger' | 'neutral' {
  if (impact === 'High') return 'danger'
  if (impact === 'Medium') return 'warning'
  if (impact === 'Low') return 'success'
  return 'neutral'
}

export function AzureAdvisorView({
  selection,
  cacheProvider,
  providerVersion,
}: AzureAdvisorViewProps) {
  // Phase 7B / 7C.1: invalidate stale data on provider / account
  // change.  The canonical cache key already includes the provider
  // segment, so AWS and Azure cannot collide.
  const prevSelectionRef = useRef(selection)
  const prevProviderRef = useRef(cacheProvider)
  useEffect(() => {
    if (
      prevSelectionRef.current !== selection ||
      prevProviderRef.current !== cacheProvider
    ) {
      invalidateFinopsCache()
      prevSelectionRef.current = selection
      prevProviderRef.current = cacheProvider
    }
  }, [selection, cacheProvider, providerVersion])

  const recommendations = useFinopsQuery<CloudRecommendationListResponse>(
    buildCloudRecommendationsCacheKey(selection, cacheProvider),
    () => fetchCloudRecommendations({ provider: 'AZURE', accountId: selection }),
  )

  const data = recommendations.entry.data
  const summary = data?.summary
  const items = data?.recommendations ?? []
  const loading = recommendations.entry.status === 'loading'
  const error = recommendations.entry.error

  const total = summary?.total_recommendations ?? 0
  const withSavings = summary?.recommendations_with_savings ?? 0
  const potentialAnnual = summary?.potential_annual_savings ?? null
  const currency = summary?.currency ?? null
  const savingsStatus = summary?.savings_status ?? 'EMPTY'
  const highImpactCount = (summary?.by_impact ?? [])
    .filter((entry) => entry.key === 'High')
    .reduce((acc, entry) => acc + entry.count, 0)
  const resourcesAffected = new Set(items.map((r) => r.resource_id)).size

  const byImpactRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_impact ?? []).map((entry) => ({
        key: entry.key,
        label: entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_impact],
  )
  const byResourceTypeRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_resource_type ?? []).slice(0, 10).map((entry) => ({
        key: entry.key,
        label: entry.key.split('/').pop() ?? entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_resource_type],
  )
  const bySubscriptionRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_subscription ?? []).map((entry) => ({
        key: entry.key,
        label: entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_subscription],
  )

  const tableColumns: DataTableColumn<CloudRecommendationItem>[] = [
    {
      key: 'recommendation',
      header: 'Recommendation',
      cell: (r) => (
        <span className="truncate" title={r.title}>
          {recommendationTitle(r)}
        </span>
      ),
    },
    {
      key: 'impact',
      header: 'Impact',
      cell: (r) => {
        const impact = recommendationImpact(r)
        return (
          <StatusBadge tone={impactTone(impact)}>
            {impact ?? '—'}
          </StatusBadge>
        )
      },
    },
    {
      key: 'resource',
      header: 'Resource',
      cell: (r) => (
        <span className="truncate" title={r.resource_id}>
          {resourceNameFromMetadata(r)}
        </span>
      ),
    },
    {
      key: 'resource_type',
      header: 'Resource Type',
      cell: (r) => (
        <span className="truncate" title={r.resource_type}>
          {r.resource_type.split('/').pop() ?? r.resource_type}
        </span>
      ),
    },
    {
      key: 'subscription',
      header: 'Subscription',
      cell: (r) => maskAccountId(r.account_id ?? ''),
    },
    {
      key: 'savings',
      header: 'Annual Savings',
      cell: (r) => formatAnnualSavings(r.estimated_savings, r.currency),
    },
    {
      key: 'last_updated',
      header: 'Last Updated',
      cell: (r) => {
        const lu = recommendationLastUpdated(r)
        if (!lu) return '—'
        try {
          return new Date(lu).toLocaleString()
        } catch {
          return lu
        }
      },
    },
  ]

  // Currency safety: when savingsStatus == "MIXED_CURRENCY" the
  // aggregator refused to invent an FX rate.  Show per-currency
  // totals explicitly so the operator sees the currency mismatch.
  const renderPotentialSavings = () => {
    if (savingsStatus === 'MIXED_CURRENCY') {
      const lines = (summary?.savings_by_currency ?? []).map((entry) => ({
        key: entry.currency,
        text: `${entry.currency} ${entry.amount}`,
      }))
      return (
        <div className="space-y-1" data-testid="azure-advisor-mixed-currency">
          <div className="text-xs text-fg-muted">Multiple currencies</div>
          {lines.map((line) => (
            <div key={line.key} className="text-sm font-semibold tabular-nums">
              {line.text}
            </div>
          ))}
        </div>
      )
    }
    if (savingsStatus === 'EMPTY') {
      return <span className="text-fg-muted">No savings data</span>
    }
    return (
      <span className="text-lg font-semibold tabular-nums">
        {formatAnnualSavings(potentialAnnual, currency)}
      </span>
    )
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Optimization"
        subtitle="Azure Advisor Cost recommendations"
        context={
          <>
            <span className="text-fg-muted">Subscription</span>
            <span className="text-fg-secondary">
              {selection === 'all' || selection === null
                ? 'All Azure Subscriptions'
                : maskAccountId(selection)}
            </span>
            <span className="text-fg-muted">Status</span>
            <span className="text-fg-secondary">
              <StatusBadge tone={statusTone(summary?.status ?? 'SUCCESS')}>
                {summary?.status ?? '—'}
              </StatusBadge>
            </span>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">
              {formatLocalTime(recommendations.entry.refreshedAt)}
            </span>
          </>
        }
        actions={<RefreshButton onClick={recommendations.refresh} />}
      />

      {error && !data && (
        <ErrorState
          title="Could not load Azure recommendations"
          message={error.message}
          onRetry={recommendations.refresh}
        />
      )}

      {(summary?.subscription_errors ?? []).length > 0 && (
        <PartialWarning
          warnings={(summary?.subscription_errors ?? []).map((e) => ({
            source: 'azure-advisor',
            code: e.code,
            message: `${e.display_name}: ${e.message}`,
            region: null,
          }))}
          title="Some Azure subscriptions failed to enumerate recommendations"
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="Active Recommendations"
          value={total.toLocaleString('en-US')}
          description={loading ? 'Loading…' : 'From Azure Advisor (Cost)'}
        />
        <div data-testid="azure-advisor-potential-savings">
          <MetricCard
            label="Potential Annual Savings"
            value={renderPotentialSavings()}
            description={
              savingsStatus === 'MIXED_CURRENCY'
                ? 'Per-currency totals below'
                : currency
                  ? `Currency: ${currency}`
                  : 'No currency metadata'
            }
          />
        </div>
        <MetricCard
          label="High Impact"
          value={highImpactCount.toLocaleString('en-US')}
          description="High-severity recommendations"
        />
        <MetricCard
          label="Resources Affected"
          value={resourcesAffected.toLocaleString('en-US')}
          description="Distinct target resources"
        />
      </div>

      {withSavings > 0 && total > withSavings && (
        <div
          className="rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-fg-muted"
          data-testid="azure-advisor-savings-coverage"
        >
          {withSavings.toLocaleString('en-US')} of {total.toLocaleString('en-US')} recommendations carry explicit annual savings data;
          {' '}
          the remainder are surfaced without savings figures (the
          aggregator never invents a value).
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <SectionCard title="By Impact" description="Severity distribution">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={byImpactRows} currency="" maxRows={5} showShare safeShare />
          )}
        </SectionCard>
        <SectionCard title="By Resource Type" description="Most-affected resource types">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={byResourceTypeRows} currency="" maxRows={8} showShare safeShare />
          )}
        </SectionCard>
        <SectionCard title="By Subscription" description="Per-subscription attribution">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={bySubscriptionRows} currency="" maxRows={5} showShare safeShare />
          )}
        </SectionCard>
      </div>

      <SectionCard
        title="Azure Advisor Recommendations"
        description={`${total.toLocaleString('en-US')} total · ${items.length.toLocaleString('en-US')} returned`}
      >
        {loading && !data ? (
          <LoadingSkeleton className="h-32 w-full" />
        ) : (
          <DataTable
            columns={tableColumns}
            rows={items}
            rowKey={(r) => r.recommendation_id || r.resource_id}
            caption="Azure Advisor Cost recommendations"
          />
        )}
      </SectionCard>

      {(summary?.by_subcategory ?? []).length > 0 && (
        <SectionCard
          title="By Subcategory"
          description="Advisor subcategory breakdown"
        >
          <ul className="space-y-1 text-xs" data-testid="azure-advisor-by-subcategory">
            {(summary?.by_subcategory ?? []).map((entry) => (
              <li key={entry.key} className="flex items-center justify-between gap-3">
                <span className="text-fg-primary">{entry.key}</span>
                <span className="tabular-nums text-fg-secondary">{entry.count.toLocaleString('en-US')}</span>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}
    </div>
  )
}
