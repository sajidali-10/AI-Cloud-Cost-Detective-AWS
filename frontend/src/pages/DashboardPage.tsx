// Phase 7A — Dashboard page wired to real Phase 1–3 data with
// calendar-month billing.
//
// Phase 7A invariants:
//   * The single source of truth for the active period is the
//     FinopsPeriod context — header and hero always show the same
//     month.
//   * Identity / Resources / Capabilities drive the AWS
//     environment panel; costs come from the selected calendar
//     month (MTD-clipped when current).
//   * Auth lifecycle: every FinOps hook now waits for auth
//     bootstrap via the store's gate; the dashboard cannot fire a
//     request before the JWT is wired in.

import * as React from 'react'
import { useEffect, useMemo, useRef } from 'react'
import { PageHeader, ContextItem } from '../components/PageHeader'
import { MetricCard } from '../components/MetricCard'
import { SectionCard } from '../components/SectionCard'
import { StatusBadge } from '../components/StatusBadge'
import { DashboardHero } from '../components/DashboardHero'
import { PeriodSelector } from '../components/PeriodSelector'
import { RefreshButton } from '../components/RefreshButton'
import { CostTrendChart } from '../components/CostTrendChart'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { CapabilityStatusRow } from '../components/CapabilityStatusRow'
import { PartialWarning } from '../components/PartialWarning'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { useFinopsQuery } from '../lib/finops/store'
import { fetchIdentity } from '../lib/finops/identity'
import { fetchCostReport, buildCostsCacheKey } from '../lib/finops/costs'
import {
  fetchCloudCostReport,
  adaptCloudCostReport,
  buildCloudCostsCacheKey,
} from '../lib/finops/cloudCosts'
import { fetchResources, buildResourcesCacheKey } from '../lib/finops/resources'
import {
  fetchCapabilities,
  fetchOptimizationSummary,
  buildCapabilitiesCacheKey,
  buildSummaryCacheKey,
} from '../lib/finops/optimization'
import { useFinopsPeriod } from '../lib/finops/period'
import { useAccountContext } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import { Link } from '../lib/router'
import { invalidateFinopsCache } from '../lib/finops/store'
import type {
  AwsIdentity,
  CostReportResponse,
  OptimizationSummaryResponse,
  CapabilitiesResponse,
  ResourcesResponse,
  SummaryByCategory,
  RegionCost,
  ServiceCost,
} from '../types/finops'
import { monthLabel } from '../types/finops'
import {
  changeTone,
  formatChangePercent,
  formatCurrencyCompact,
  formatCurrencyDecimal,
  formatLocalTime,
  formatSignedChange,
  maskAccountId,
  parseDecimal,
  capabilityTone,
  capabilityLabel,
} from '../lib/format'

export function DashboardPage() {
  const { period, region, currentMonth } = useFinopsPeriod()
  const { selection } = useAccountContext()
  const { provider, providerSlug, providerVersion } = useProviderContext()
  const effectiveRegion = region === 'all' ? null : region
  const cacheProvider = providerSlug === 'azure'
    ? ('azure' as const)
    : providerSlug === 'rackspace'
    ? ('rackspace' as const)
    : ('aws' as const)

  // Phase 7B: invalidate stale data when the user switches accounts.
  // The store's cache is keyed on (period, region, account) so this
  // is belt-and-braces — it ensures no stale entry from a previous
  // account survives while the new fetch is in flight.
  // Phase 7C.1: also invalidate when the provider changes so an
  // AWS response cannot satisfy an Azure request that flips in
  // before the new fetch completes.
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

  // Hotfix: dispatch to a provider-specific child component so the
  // hook count and order stay stable across provider switches.
  // Returning an Azure child BEFORE the AWS hooks fire causes
  // React's "Rendered fewer hooks than expected" error and the
  // entire tree unmounts — that was the source of the blank
  // dashboard bug.  Each child calls its own hooks.
  if (provider === 'AZURE') {
    return (
      <DashboardErrorBoundary provider={provider}>
        <AzureDashboard
          period={period}
          selection={selection}
          cacheProvider={cacheProvider}
          providerVersion={providerVersion}
          currentMonth={currentMonth}
        />
      </DashboardErrorBoundary>
    )
  }

  return (
    <DashboardErrorBoundary provider={provider}>
      <AwsDashboard
        period={period}
        region={region}
        currentMonth={currentMonth}
        effectiveRegion={effectiveRegion}
        selection={selection}
        cacheProvider={cacheProvider}
        providerVersion={providerVersion}
      />
    </DashboardErrorBoundary>
  )
}

/**
 * Hotfix: error boundary scoped to the dashboard so an exception
 * in any provider-specific child cannot permanently blank the
 * page.  Falls back to a professional error state with a retry
 * button instead of crashing the whole tree.
 */
class DashboardErrorBoundary extends React.Component<
  { provider: string; children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  handleReset = (): void => {
    this.setState({ error: null })
  }

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div className="space-y-3" data-testid="dashboard-error-boundary">
          <ErrorState
            title="Dashboard could not render"
            message={this.state.error.message || 'An unexpected error occurred.'}
            onRetry={this.handleReset}
          />
        </div>
      )
    }
    return this.props.children
  }
}

function AwsDashboard({
  period,
  region,
  currentMonth,
  effectiveRegion,
  selection,
  cacheProvider,
  providerVersion: _providerVersion,
}: {
  period: import('../types/finops').BillingPeriod
  region: string
  currentMonth: string
  effectiveRegion: string | null
  selection: string | null
  cacheProvider: 'aws' | 'azure' | 'rackspace'
  providerVersion: number
}) {
  // Identity — single fetch, drives the page header.
  const identity = useFinopsQuery<AwsIdentity>(
    `${cacheProvider}:identity:${selection}`,
    () => fetchIdentity({ accountId: selection }),
    { ttlMs: 5 * 60_000 },
  )

  // Cost report — keyed on period + region + account + provider.
  const cost = useFinopsQuery<CostReportResponse>(
    buildCostsCacheKey(period, effectiveRegion, selection, cacheProvider),
    () => fetchCostReport(period, { region: effectiveRegion, accountId: selection }),
  )

  // Capabilities — drives the Optimization Summary card.
  const caps = useFinopsQuery<CapabilitiesResponse>(
    buildCapabilitiesCacheKey(effectiveRegion, selection, cacheProvider),
    () => fetchCapabilities(effectiveRegion, { accountId: selection }),
    { ttlMs: 60_000 },
  )

  // Optimization summary — keyed on period + region + account + provider.
  const summary = useFinopsQuery<OptimizationSummaryResponse>(
    buildSummaryCacheKey(period, effectiveRegion, selection, cacheProvider),
    () => fetchOptimizationSummary(period, effectiveRegion, { accountId: selection }),
    { ttlMs: 60_000 },
  )

  // Resources — drives the resource KPI + environment panel.
  const resources = useFinopsQuery<ResourcesResponse>(
    buildResourcesCacheKey(effectiveRegion, selection, cacheProvider),
    () => fetchResources(effectiveRegion, { accountId: selection }),
    { ttlMs: 60_000 },
  )

  const refreshAll = () => {
    identity.refresh()
    cost.refresh()
    caps.refresh()
    summary.refresh()
    resources.refresh()
  }

  const report = cost.entry.data?.report ?? null

  // Phase 7A — single source of truth for the active month label.
  const activeMonthLabel = period.kind === 'month' ? monthLabel(period.month) : monthLabel(currentMonth)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Dashboard"
        subtitle="AWS cost visibility, optimization and AI-powered FinOps analysis"
        context={
          <>
            <ContextItem
              label="Account"
              value={
                identity.entry.data
                  ? maskAccountId(identity.entry.data.account)
                  : '—'
              }
            />
            <ContextItem
              label="Region"
              value={
                identity.entry.data?.region
                  ? identity.entry.data.region
                  : resources.entry.data?.region
                    ? resources.entry.data.region
                    : effectiveRegion ?? 'Account-level'
              }
            />
            <ContextItem
              label="Month"
              value={activeMonthLabel}
            />
            <ContextItem
              label="Status"
              value={
                <StatusBadge
                  tone={
                    cost.entry.status === 'success'
                      ? 'success'
                      : cost.entry.status === 'error'
                        ? 'danger'
                        : 'info'
                  }
                >
                  {cost.entry.status === 'success'
                    ? 'Live'
                    : cost.entry.status === 'loading'
                      ? 'Loading'
                      : cost.entry.status === 'refreshing'
                        ? 'Refreshing'
                        : cost.entry.status === 'error'
                          ? 'Unavailable'
                          : 'Idle'}
                </StatusBadge>
              }
            />
            <ContextItem
              label="Last updated"
              value={formatLocalTime(cost.entry.refreshedAt ?? identity.entry.refreshedAt)}
            />
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            <PeriodSelector />
            <RefreshButton onClick={refreshAll} />
          </div>
        }
      />

      {(cost.entry.error || summary.entry.error || resources.entry.error) && (
        <PartialWarning
          warnings={[
            ...(cost.entry.error
              ? [{ source: 'costs', code: 'error', message: cost.entry.error.message, region: null }]
              : []),
            ...(summary.entry.error
              ? [{ source: 'optimization', code: 'error', message: summary.entry.error.message, region: null }]
              : []),
            ...(resources.entry.error
              ? [{ source: 'resources', code: 'error', message: resources.entry.error.message, region: null }]
              : []),
          ]}
          title="Some data could not be loaded"
        />
      )}

      {/* Centred HipLink AI Assistant hero — uses real data only. */}
      <DashboardHero
        provider="AWS"
        accountLabel={
          identity.entry.data ? maskAccountId(identity.entry.data.account) : null
        }
        regionLabel={identity.entry.data?.region ?? null}
        refreshedLabel={formatLocalTime(
          cost.entry.refreshedAt ?? identity.entry.refreshedAt,
        )}
        dataStatus={
          cost.entry.status === 'success'
            ? 'live'
            : cost.entry.status === 'loading'
              ? 'loading'
              : cost.entry.status === 'refreshing'
                ? 'refreshing'
                : cost.entry.status === 'error'
                  ? 'unavailable'
                  : 'idle'
        }
      />

      {/* KPI tiles */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SpendKpi report={report} loading={cost.entry.status === 'loading'} period={period} providerLabel="AWS Spend" />
        <ChangeKpi report={report} loading={cost.entry.status === 'loading'} period={period} />
        <ResourcesKpi
          resources={resources.entry.data}
          loading={resources.entry.status === 'loading'}
        />
        <OptimizeKpi
          summary={summary.entry.data}
          loading={summary.entry.status === 'loading'}
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Cost Trend"
          description={`Daily spend · ${activeMonthLabel}`}
          actions={<StatusBadge tone={cost.entry.data?.cache_status === 'HIT' ? 'neutral' : 'info'}>
            Cache: {cost.entry.data?.cache_status ?? '—'}
          </StatusBadge>}
        >
          {cost.entry.status === 'loading' && <LoadingSkeleton className="h-[200px] w-full" />}
          {cost.entry.error && <ErrorState message={cost.entry.error.message} />}
          {cost.entry.data && (
            <CostTrendChart
              points={cost.entry.data.report.daily_trend}
              currency={cost.entry.data.report.currency}
              ariaLabel={`Daily cost trend for ${activeMonthLabel}`}
            />
          )}
        </SectionCard>
        <SectionCard
          title="Cost by Service"
          description="Top services by spend"
          actions={
            <StatusBadge tone={report?.estimated ? 'warning' : 'success'}>
              {report?.estimated ? 'Estimated' : 'Final'}
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && <LoadingSkeletonRows rows={6} />}
          {cost.entry.error && <ErrorState message={cost.entry.error.message} />}
          {cost.entry.data && (
            <HorizontalBarChart
              rows={serviceRows(cost.entry.data.report.by_service)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          )}
        </SectionCard>
      </div>

      {/* Summary + environment */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Optimization Summary"
          description={`Open opportunities · ${activeMonthLabel}`}
        >
          <OptimizationSummary
            summary={summary.entry.data}
            loading={summary.entry.status === 'loading'}
            error={summary.entry.error}
          />
          <CapabilityPanel caps={caps.entry.data} loading={caps.entry.status === 'loading'} />
        </SectionCard>
        <SectionCard title="AWS Environment" description="Account, regions and active services">
          <EnvironmentPanel
            identity={identity.entry.data}
            resources={resources.entry.data}
            caps={caps.entry.data}
            cost={cost.entry.data}
          />
        </SectionCard>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// KPI tiles
// ---------------------------------------------------------------------------

export function SpendKpi({
  report,
  loading,
  period,
  providerLabel,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  period: import('../types/finops').BillingPeriod
  providerLabel: string
}) {
  if (loading && !report) {
    return <MetricCard label={providerLabel} description="Loading…" value={undefined} />
  }
  if (!report) {
    return <MetricCard label={providerLabel} description="—" value={undefined} />
  }
  const label = period.kind === 'month' ? monthLabel(period.month) : `Last ${period.days} days`

  // Hotfix #2 — cost completeness: a genuine successful $0.00 renders
  // as $0.00, but UNKNOWN must never be shown as $0.00.  When every
  // subscription failed/throttled the canonical envelope reports
  // ``status`` in {ERROR, PARTIAL_SUCCESS} with ``successful_accounts``
  // == 0 — render "Unavailable" instead.  When some succeeded and some
  // failed, show the subtotal with an explicit "Partial" marker.
  const successful = report.successful_accounts ?? 0
  const failed = report.failed_accounts ?? 0
  const allFailed = failed > 0 && successful === 0
  const partial = successful > 0 && failed > 0

  if (allFailed) {
    return (
      <MetricCard
        label={providerLabel}
        icon={<SpendIcon />}
        value="Unavailable"
        description={`${label} · no subscription data`}
      />
    )
  }
  if (partial) {
    return (
      <MetricCard
        label={providerLabel}
        icon={<SpendIcon />}
        value={formatCurrencyCompact(report.total_cost, report.currency)}
        description={`${label} · ${report.currency} · Partial (${failed} unavailable)`}
      />
    )
  }
  return (
    <MetricCard
      label={providerLabel}
      icon={<SpendIcon />}
      value={formatCurrencyCompact(report.total_cost, report.currency)}
      description={`${label} · ${report.currency}`}
    />
  )
}

function ChangeKpi({
  report,
  loading,
  period,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  period: import('../types/finops').BillingPeriod
}) {
  if (loading && !report) {
    return <MetricCard label="Cost Change" description="Loading…" value={undefined} />
  }
  if (!report) {
    return <MetricCard label="Cost Change" description="—" value={undefined} />
  }
  const pct = formatChangePercent(report.total_cost, report.previous_period_cost)
  const abs = formatSignedChange(report.change_amount, report.currency)
  const tone = changeTone(report.total_cost, report.previous_period_cost)
  const directionWord =
    parseDecimal(report.change_amount) === null
      ? 'No comparison'
      : parseDecimal(report.change_amount) === 0
        ? 'No change'
        : parseDecimal(report.change_amount)! > 0
          ? 'Increase'
          : 'Decrease'
  const compareLabel =
    period.kind === 'month'
      ? 'vs previous month'
      : `vs previous ${period.days} days`
  return (
    <MetricCard
      label="Cost Change"
      icon={<DeltaIcon />}
      value={pct}
      tone={tone}
      description={`${directionWord} ${compareLabel} · ${abs}`}
    />
  )
}

function ResourcesKpi({
  resources,
  loading,
}: {
  resources: ResourcesResponse | null
  loading: boolean
}) {
  const counts = useMemo(() => computeResourceCounts(resources), [resources])
  if (loading && !resources) {
    return <MetricCard label="Resources" description="Loading…" value={undefined} />
  }
  if (!resources) {
    return <MetricCard label="Resources" description="—" value={undefined} />
  }
  return (
    <div data-testid="resources-total-kpi">
      <MetricCard
        label="Resources"
        icon={<ResourcesIcon />}
        value={counts.total.toLocaleString('en-US')}
        description={`Across ${counts.types} resource types`}
      />
    </div>
  )
}

function OptimizeKpi({
  summary,
  loading,
}: {
  summary: OptimizationSummaryResponse | null
  loading: boolean
}) {
  if (loading && !summary) {
    return <MetricCard label="Optimize" description="Loading…" value={undefined} />
  }
  if (!summary) {
    return <MetricCard label="Optimize" description="—" value={undefined} />
  }
  const savings = parseDecimal(summary.total_estimated_monthly_savings)
  const description =
    savings !== null
      ? `${formatCurrencyCompact(savings, summary.currency)} known savings / mo`
      : 'Savings not available'
  return (
    <MetricCard
      label="Optimize"
      icon={<OptimizeIcon />}
      value={summary.total_recommendations.toLocaleString('en-US')}
      description={description}
      tone={summary.total_recommendations > 0 ? 'info' : 'neutral'}
    />
  )
}

// ---------------------------------------------------------------------------
// Optimization summary
// ---------------------------------------------------------------------------

function OptimizationSummary({
  summary,
  loading,
  error,
}: {
  summary: OptimizationSummaryResponse | null
  loading: boolean
  error: Error | null
}) {
  if (loading && !summary) {
    return <LoadingSkeletonRows rows={3} />
  }
  if (error && !summary) {
    return <ErrorState message={error.message} />
  }
  if (!summary) {
    return <div className="text-xs text-fg-muted">No data</div>
  }

  const rows = summary.by_resource_type.filter((r) => r.count > 0)
  const savings = parseDecimal(summary.total_estimated_monthly_savings)
  return (
    <div className="space-y-3" data-testid="optimization-summary">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-2xl font-semibold tabular-nums text-fg-primary" data-testid="optimization-count">
          {summary.total_recommendations.toLocaleString('en-US')}
        </span>
        <span className="text-xs text-fg-muted">open opportunities</span>
        {savings !== null ? (
          <span className="text-xs text-success">
            {formatCurrencyCompact(savings, summary.currency)} known savings / mo
          </span>
        ) : (
          <span className="text-xs text-fg-muted">savings not available</span>
        )}
      </div>
      {rows.length === 0 ? (
        <p className="text-xs text-fg-muted">No resource categories reported any opportunities.</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {rows.map((r) => (
            <li key={r.key} className="flex items-center justify-between gap-3" data-testid="optimization-by-type">
              <span className="text-fg-primary">{r.key.split('_').join(' ')}</span>
              <span className="tabular-nums text-fg-secondary">{r.count}</span>
            </li>
          ))}
        </ul>
      )}
      {summary.warnings.length > 0 && (
        <PartialWarning warnings={summary.warnings} title="Optimization warnings" />
      )}
    </div>
  )
}

function CapabilityPanel({
  caps,
  loading,
}: {
  caps: CapabilitiesResponse | null
  loading: boolean
}) {
  if (loading && !caps) {
    return <LoadingSkeletonRows rows={3} />
  }
  if (!caps) {
    return null
  }
  return (
    <div className="mt-3 space-y-1 border-t border-border pt-3" data-testid="dashboard-capabilities">
      <CapabilityStatusRow label="Compute Optimizer" capability={caps.compute_optimizer} kind="compute_optimizer" />
      <CapabilityStatusRow
        label="Cost Optimization Hub"
        capability={caps.cost_optimization_hub}
        kind="cost_optimization_hub"
      />
      <CapabilityStatusRow
        label="Deterministic Engine"
        capability={caps.deterministic_engine}
        kind="deterministic_engine"
      />
      {caps.warnings.length > 0 && (
        <PartialWarning warnings={caps.warnings} title="Capability warnings" />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AWS environment panel
// ---------------------------------------------------------------------------

function EnvironmentPanel({
  identity,
  resources,
  caps,
  cost,
}: {
  identity: AwsIdentity | null
  resources: ResourcesResponse | null
  caps: CapabilitiesResponse | null
  cost: CostReportResponse | null
}) {
  const counts = useMemo(() => computeResourceCounts(resources), [resources])
  const costState = cost
    ? {
        cache: cost.cache_status,
        cachedAt: cost.cached_at,
        days: cost.report.period.days,
      }
    : null

  return (
    <dl className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2" data-testid="environment-panel">
      <Field label="Account" value={identity ? maskAccountId(identity.account) : '—'} />
      <Field label="Region" value={resources?.region ?? identity?.region ?? '—'} />
      <Field label="Resource types discovered" value={String(counts.types)} />
      <Field label="Total resources" value={counts.total.toLocaleString('en-US')} />
      <Field
        label="Compute Optimizer"
        value={caps ? capabilityLabel(caps.compute_optimizer.status) : '—'}
      />
      <Field
        label="Cost Optimization Hub"
        value={caps ? capabilityLabel(caps.cost_optimization_hub.status) : '—'}
      />
      <Field label="Cost data state" value={costState ? `Cache ${costState.cache}` : '—'} />
      <Field
        label="Cost cache updated"
        value={costState?.cachedAt ? formatLocalTime(costState.cachedAt) : '—'}
      />
    </dl>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col rounded-md border border-border bg-surface-2 px-3 py-2">
      <dt className="text-[10px] font-medium uppercase tracking-wider text-fg-muted">{label}</dt>
      <dd className="mt-0.5 text-fg-primary">{value}</dd>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function computeResourceCounts(resources: ResourcesResponse | null): {
  total: number
  types: number
} {
  if (!resources) return { total: 0, types: 0 }
  let total = 0
  let types = 0
  for (const svc of Object.values(resources.services)) {
    if (!svc) continue
    if (svc.items.length > 0 || svc.status !== 'ok') {
      types += 1
    }
    total += svc.items.length
  }
  return { total, types }
}

function serviceRows(services: ServiceCost[]): HorizontalBarRow[] {
  return services.map((s) => ({
    key: s.service,
    label: s.service,
    title: s.service,
    value: s.amount,
  }))
}

export function regionRows(regions: RegionCost[]): HorizontalBarRow[] {
  return regions.map((r) => ({
    key: r.region,
    label: r.region === 'global' || r.region === 'no_region' ? 'Global / No Region' : r.region,
    title: r.region,
    value: r.amount,
  }))
}

// ---------------------------------------------------------------------------
// Icons (same vocabulary as Phase 6A; reused here so the dashboard
// keeps the same visual rhythm)
// ---------------------------------------------------------------------------

function SpendIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2v20" />
      <path d="M17 5H9.5a3.3 3.5 0 0 0 0 7h5a3.3 3.5 0 0 1 0 7H6" />
    </svg>
  )
}
function DeltaIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m22 7-8.5 8.5-5-5L2 17" />
      <path d="M16 7h6v6" />
    </svg>
  )
}
function ResourcesIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8" />
      <path d="M12 17v4" />
    </svg>
  )
}
function OptimizeIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3" />
      <path d="M12 19v3" />
      <path d="m4.93 4.93 2.12 2.12" />
      <path d="m16.95 16.95 2.12 2.12" />
      <path d="M2 12h3" />
      <path d="M19 12h3" />
      <path d="m4.93 19.07 2.12-2.12" />
      <path d="m16.95 7.05 2.12-2.12" />
    </svg>
  )
}

// Re-export for downstream pages.
export { fieldGrid, summaryByCategory }
function fieldGrid() {
  return null
}
function summaryByCategory(rows: SummaryByCategory[]) {
  return rows
}

// ---------------------------------------------------------------------------
// Phase 7C.2B — Azure Dashboard
// ---------------------------------------------------------------------------

function AzureDashboard({
  period,
  selection,
  cacheProvider,
  providerVersion: _providerVersion,
  currentMonth,
}: {
  period: import('../types/finops').BillingPeriod
  selection: string | null
  cacheProvider: 'aws' | 'azure' | 'rackspace'
  providerVersion: number
  currentMonth: string
}) {
  // Cost report — provider-neutral route.
  const cost = useFinopsQuery<CostReportResponse>(
    buildCloudCostsCacheKey(period, null, selection, cacheProvider),
    () =>
      fetchCloudCostReport(period, {
        provider: 'AZURE',
        accountId: selection,
      }).then(adaptCloudCostReport),
  )
  const report = cost.entry.data?.report ?? null
  const activeMonthLabel = period.kind === 'month' ? monthLabel(period.month) : monthLabel(currentMonth)

  const refreshAll = () => {
    cost.refresh()
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Azure Dashboard"
        subtitle="Azure cost visibility, resource inventory, optimization and AI-powered FinOps analysis"
        context={
          <>
            <ContextItem
              label="Subscription"
              value={
                report?.account_id
                  ? maskAccountId(report.account_id)
                  : selection === 'all' || selection === null
                    ? 'All Azure Subscriptions'
                    : maskAccountId(selection)
              }
            />
            <ContextItem label="Month" value={activeMonthLabel} />
            <ContextItem
              label="Status"
              value={
                <StatusBadge
                  tone={
                    cost.entry.status === 'success'
                      ? 'success'
                      : cost.entry.status === 'error'
                        ? 'danger'
                        : 'info'
                  }
                >
                  {cost.entry.status === 'success'
                    ? 'Live'
                    : cost.entry.status === 'loading'
                      ? 'Loading'
                      : cost.entry.status === 'refreshing'
                        ? 'Refreshing'
                        : cost.entry.status === 'error'
                          ? 'Unavailable'
                          : 'Idle'}
                </StatusBadge>
              }
            />
            <ContextItem label="Last updated" value={formatLocalTime(cost.entry.refreshedAt)} />
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            <PeriodSelector />
            <RefreshButton onClick={refreshAll} />
          </div>
        }
      />

      {cost.entry.error && !cost.entry.data && (
        <PartialWarning
          warnings={[
            {
              source: 'costs',
              code: 'error',
              message: cost.entry.error.message,
              region: null,
            },
          ]}
          title="Cost data could not be loaded"
        />
      )}

      <DashboardHero
        provider="AZURE"
        accountLabel={
          report?.account_id
            ? maskAccountId(report.account_id)
            : selection === 'all' || selection === null
              ? 'All Azure Subscriptions'
              : maskAccountId(selection)
        }
        regionLabel={null}
        refreshedLabel={formatLocalTime(cost.entry.refreshedAt)}
        dataStatus={
          cost.entry.status === 'success'
            ? 'live'
            : cost.entry.status === 'loading'
              ? 'loading'
              : cost.entry.status === 'refreshing'
                ? 'refreshing'
                : cost.entry.status === 'error'
                  ? 'unavailable'
                  : 'idle'
        }
      />

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <SpendKpi report={report} loading={cost.entry.status === 'loading'} period={period} providerLabel="Total Spend" />
        <ChangeKpi report={report} loading={cost.entry.status === 'loading'} period={period} />
        <ResourcesKpiAzure />
        <OptimizeKpiAzure />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Cost Trend"
          description={`Daily spend · ${activeMonthLabel}`}
          actions={
            <StatusBadge tone={cost.entry.data?.cache_status === 'HIT' ? 'neutral' : 'info'}>
              Cache: {cost.entry.data?.cache_status ?? '—'}
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && <LoadingSkeleton className="h-[200px] w-full" />}
          {cost.entry.error && <ErrorState message={cost.entry.error.message} />}
          {cost.entry.data && (
            <CostTrendChart
              points={cost.entry.data.report.daily_trend}
              currency={cost.entry.data.report.currency}
              ariaLabel={`Daily cost trend for ${activeMonthLabel}`}
            />
          )}
        </SectionCard>
        <SectionCard
          title="Cost by Service"
          description="Top services by spend"
          actions={
            <StatusBadge tone={report?.estimated ? 'warning' : 'success'}>
              {report?.estimated ? 'Estimated' : 'Final'}
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && <LoadingSkeletonRows rows={6} />}
          {cost.entry.error && <ErrorState message={cost.entry.error.message} />}
          {cost.entry.data && (
            <HorizontalBarChart
              rows={serviceRows(cost.entry.data.report.by_service)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          )}
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Azure Optimization"
          description="View active Azure Advisor Cost recommendations."
          actions={
            <Link
              to="/optimization"
              className="
                inline-flex items-center rounded-md border border-border
                bg-surface px-3 py-1.5 text-sm font-medium text-fg-primary
                hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
              "
              data-testid="azure-dashboard-optimization-link"
            >
              Open Optimization
            </Link>
          }
        >
          <p className="text-xs text-fg-muted">
            Active Azure Advisor Cost recommendations for the current subscription are
            available in the Optimization page.
          </p>
        </SectionCard>
        <SectionCard
          title="Azure Resources"
          description="View Azure Resource Graph inventory for the selected subscription."
          actions={
            <Link
              to="/resources"
              className="
                inline-flex items-center rounded-md border border-border
                bg-surface px-3 py-1.5 text-sm font-medium text-fg-primary
                hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
              "
              data-testid="azure-dashboard-resources-link"
            >
              Open Resources
            </Link>
          }
        >
          <p className="text-xs text-fg-muted">
            Full Azure Resource Graph inventory for the current subscription is
            available in the Resources page.
          </p>
        </SectionCard>
      </div>
    </div>
  )
}

function ResourcesKpiAzure() {
  return (
    <MetricCard
      label="Resources"
      value="—"
      description="Visit Resources"
    />
  )
}

function OptimizeKpiAzure() {
  return (
    <MetricCard
      label="Optimize"
      value="—"
      description="Visit Optimization"
    />
  )
}
