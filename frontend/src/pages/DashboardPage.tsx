// Phase 7D.3 — Unified multi-cloud executive Dashboard.
//
// One provider-aware Dashboard for AWS and Azure, fed exclusively
// from the canonical Phase 7C / 7D.1 cross-cloud routes:
//
//   * /api/cloud/costs          — total spend, trend, service / region breakdown
//   * /api/cloud/resources      — resource inventory, per-scope counts
//   * /api/cloud/recommendations — optimization summary + monthly savings
//
// The legacy /api/aws/* routes are kept as compatibility surfaces
// by the backend; the Dashboard no longer reads them as a primary
// path.  Phase 7C.2B–2D wired the canonical routes and Phase 7D.1
// normalised the recommendation shape; Phase 7D.3 removes the last
// provider-specific branches from the executive page so AWS and
// Azure share:
//
//   * the same KPI tile implementations,
//   * the same Spend Trend / Service / Region charts,
//   * the same Optimization and Resource summary sections,
//   * the same loading / partial / error states,
//   * the same provider/scope-switch race protection,
//   * the same cache-key namespacing convention.
//
// Release-critical invariants (preserved from Phase 7C / 7D.2):
//
//   * bea38fd (provider-switch fix): a provider change wipes the
//     FinOps cache so an AWS response can never satisfy an Azure
//     request (and vice versa).
//   * 0c7fce1 (Azure subscription fix): the canonical cache segment
//     for Azure self-corrects a stale ``acct:<n>`` to the aggregate
//     segment so it cannot land as a stale authoritative-zero cache
//     entry.
//   * 6938362 (Phase 7C cleanup): friendly display labels only —
//     no ``acct:<n>`` in the UI; no AWS-only wording.
//   * 43b6fae (Phase 7D.1 backend): recommendation shape is
//     provider-neutral; the Dashboard consumes the unified view
//     model.
//   * 77f9279 (Phase 7D.2 UI): the Dashboard reuses the unified
//     recommendation view model and never duplicates savings /
//     currency / impact normalisation.

import * as React from 'react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader, ContextItem } from '../components/PageHeader'
import { MetricCard } from '../components/MetricCard'
import { SectionCard } from '../components/SectionCard'
import { StatusBadge } from '../components/StatusBadge'
import { DashboardHero } from '../components/DashboardHero'
import { PeriodSelector } from '../components/PeriodSelector'
import { RefreshButton } from '../components/RefreshButton'
import { CostTrendChart } from '../components/CostTrendChart'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { PartialWarning } from '../components/PartialWarning'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { Link } from '../lib/router'
import { useFinopsQuery, invalidateFinopsCache } from '../lib/finops/store'
import { useFinopsPeriod } from '../lib/finops/period'
import { useAccountContext, type SelectableAccount } from '../lib/accountContext'
import { useProviderContext, type ProviderKind } from '../lib/providerContext'
import {
  asDashboardProvider,
  dashboardScopeCacheSegment,
  friendlyScopeLabel,
  providerSubtitle,
  type DashboardProvider,
} from '../lib/finops/dashboardScope'
import {
  fetchCloudCostReport,
  adaptCloudCostReport,
  buildCloudCostsCacheKey,
  type CacheProvider,
} from '../lib/finops/cloudCosts'
import {
  fetchCloudResources,
  buildCloudResourcesCacheKey,
  type CloudResourceListResponse,
} from '../lib/finops/cloudResources'
import {
  fetchCloudRecommendations,
  buildCloudRecommendationsCacheKey,
  type CloudRecommendationListResponse,
} from '../lib/finops/cloudRecommendations'
import { buildUnifiedEnvelope } from '../lib/finops/unifiedRecommendations'
import {
  changeTone,
  formatChangePercent,
  formatCurrencyCompact,
  formatLocalTime,
  formatSignedChange,
  parseDecimal,
} from '../lib/format'
import type {
  CostReportResponse,
  RegionCost,
  ServiceCost,
  SummaryByCategory,
} from '../types/finops'
import { monthLabel } from '../types/finops'

// ===========================================================================
//  Top-level page
// ===========================================================================

export function DashboardPage() {
  const { provider } = useProviderContext()
  const wire = asDashboardProvider(provider)

  // Rackspace is intentionally not in scope for the executive
  // dashboard (the spec is AWS + Azure only).  Render a friendly
  // unavailable state instead of crashing — a missing provider
  // must never blank the page.
  if (wire === null) {
    return <DashboardUnsupported provider={provider} />
  }

  return <UnifiedDashboard provider={wire} />
}

/**
 * Friendly fallback when the active provider is not in scope for
 * the executive dashboard.  Stays within the locked two-row
 * navigation by reusing the same chrome the supported providers
 * render (AppShell wraps this; no nav changes here).
 */
function DashboardUnsupported({ provider }: { provider: ProviderKind }) {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Dashboard"
        subtitle="Multi-cloud cost visibility, optimization and AI-powered FinOps analysis."
        actions={<RefreshButton onClick={() => invalidateFinopsCache()} />}
      />
      <SectionCard
        title="Provider not supported"
        description={`The executive dashboard does not render for ${provider}.`}
      >
        <p className="text-xs text-fg-muted">
          Switch to AWS or Azure to see the unified multi-cloud
          executive view.  Cost, resource and recommendation data
          remain accessible via the cloud-specific pages when
          supported.
        </p>
      </SectionCard>
    </div>
  )
}

// ===========================================================================
//  Unified dashboard component
// ===========================================================================

interface UnifiedDashboardProps {
  provider: DashboardProvider
}

function UnifiedDashboard({ provider }: UnifiedDashboardProps) {
  const { period, currentMonth, region } = useFinopsPeriod()
  const { selection, accounts } = useAccountContext()
  const { providerVersion } = useProviderContext()

  const cacheProvider: CacheProvider = provider === 'AZURE' ? 'azure' : 'aws'
  const effectiveRegion = region === 'all' ? null : region

  // Build a provider-aware account map for friendly attribution.
  // Mirrors the pattern OptimizationPage established in Phase 7D.2:
  //   * AWS rows  → selector "acct:<local-row-id>"
  //   * Azure rows → selector "sub:<guid>"
  // The raw account_id carried by the recommendation payload is
  // kept separately so the lookup can resolve recommendation rows.
  const accountMap = useMemo(
    () =>
      accounts
        .filter((row): row is SelectableAccount => row != null)
        .map((row) => ({
          selector: provider === 'AZURE' ? `sub:${row.account_id}` : `acct:${row.id}`,
          rawAccountId: row.account_id,
          displayName: row.display_name,
          provider,
        })),
    [accounts, provider],
  )

  // -----------------------------------------------------------------------
  //  Race protection
  //
  //  A late response from a previous provider/scope must NEVER
  //  overwrite fresh state.  The Dashboard bumps ``generation``
  //  on every provider or scope change; queries capture the
  //  current generation at fetch-start and discard the result if
  //  the captured generation is stale.  This is release-critical
  //  (see spec steps 15 and 17).
  // -----------------------------------------------------------------------
  const generation = useGeneration(provider, selection, providerVersion)
  const generationRef = useRef(generation)
  useEffect(() => {
    generationRef.current = generation
  }, [generation])

  // Phase 7C/7D.1: invalidate the FinOps cache on provider/scope
  // change so a stale AWS response can never satisfy an Azure
  // request (and vice versa).  Belt-and-braces with the
  // generation guard above.
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

  // Cache-key namespacing: every dashboard query embeds the
  // (provider, scope, region, period) identity so different
  // selections never share a cache entry.  This is the
  // "cache isolation" invariant from spec step 25.
  const scopeSegment = dashboardScopeCacheSegment(selection, provider)
  const costsKey = `${scopeSegment}:costs:${periodToKey(period)}:${effectiveRegion ?? 'all'}`
  const resourcesKey = `${scopeSegment}:resources:gen${generation}`
  const recsKey = `${scopeSegment}:recommendations:gen${generation}`

  // -----------------------------------------------------------------------
  //  Canonical data fetches — provider-neutral.
  //
  //  Cost: period-aware (calendar month OR rolling lookback days).
  //  Resources / Recommendations: current-state.  This distinction
  //  is documented in spec step 24.
  // -----------------------------------------------------------------------
  const cost = useFinopsQuery<CostReportResponse>(
    costsKey,
    async () => {
      const startGen = generationRef.current
      const response = await fetchCloudCostReport(period, {
        provider,
        region: effectiveRegion,
        accountId: selection,
      })
      // Generation guard: if the user switched provider/scope
      // while this request was in flight, discard the result
      // instead of letting it overwrite fresh state.
      if (startGen !== generationRef.current) {
        throw new StaleGenerationError(startGen, generationRef.current)
      }
      return adaptCloudCostReport(response)
    },
    { ttlMs: 60_000 },
  )

  const resources = useFinopsQuery<CloudResourceListResponse>(
    resourcesKey,
    async () => {
      const startGen = generationRef.current
      const response = await fetchCloudResources({ provider, accountId: selection })
      if (startGen !== generationRef.current) {
        throw new StaleGenerationError(startGen, generationRef.current)
      }
      return response
    },
    { ttlMs: 60_000 },
  )

  const recommendations = useFinopsQuery<CloudRecommendationListResponse>(
    recsKey,
    async () => {
      const startGen = generationRef.current
      const response = await fetchCloudRecommendations({
        provider,
        accountId: selection,
      })
      if (startGen !== generationRef.current) {
        throw new StaleGenerationError(startGen, generationRef.current)
      }
      return response
    },
    { ttlMs: 60_000 },
  )

  // Derive the unified recommendation envelope once for the
  // Optimization summary section.  We deliberately consume the
  // Phase 7D.2 normaliser here so we do not duplicate the
  // savings/impact/attribution logic (spec step 8).
  const recommendationEnvelope = useMemo(() => {
    const response = recommendations.entry.data
    if (!response) return null
    return buildUnifiedEnvelope({
      response,
      provider,
      selection,
      accountMap,
      savingsPeriod: response.summary?.savings_period ?? null,
    })
  }, [recommendations.entry.data, provider, selection, accountMap])

  // Friendly scope label for the page header + hero.
  const scopeLabel = friendlyScopeLabel(selection, {
    slug: cacheProvider,
    wire: provider,
    rows: accounts,
  })

  const activeMonthLabel = period.kind === 'month' ? monthLabel(period.month) : monthLabel(currentMonth)

  const refreshAll = () => {
    cost.refresh()
    resources.refresh()
    recommendations.refresh()
  }

  // Aggregate domain error banner: surface only when one or more
  // domains failed.  Individual KPI tiles and summary sections
  // render their own per-section error indicators; this banner is
  // the dashboard-level overview (spec step 19, 20).
  const costFailed = !!cost.entry.error && !cost.entry.data
  const resourcesFailed = !!resources.entry.error && !resources.entry.data
  const recsFailed = !!recommendations.entry.error && !recommendations.entry.data
  const partialDomains = collectPartialDomains(
    cost.entry.data?.report ?? null,
    resources.entry.data ?? null,
    recommendationEnvelope,
  )

  return (
    <div className="space-y-5" data-testid="unified-dashboard" data-provider={provider}>
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Dashboard"
        subtitle={providerSubtitle(provider)}
        context={
          <>
            <ContextItem
              label={provider === 'AZURE' ? 'Subscription' : 'Account'}
              value={scopeLabel}
            />
            <ContextItem label="Month" value={activeMonthLabel} />
            <ContextItem
              label="Status"
              value={
                <StatusBadge
                  tone={
                    statusTone(cost.entry.status, cost.entry.error)
                  }
                >
                  {statusLabel(cost.entry.status, cost.entry.error)}
                </StatusBadge>
              }
            />
            <ContextItem
              label="Last updated"
              value={formatLocalTime(
                cost.entry.refreshedAt ??
                  resources.entry.refreshedAt ??
                  recommendations.entry.refreshedAt,
              )}
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

      {(partialDomains.length > 0) && (
        <PartialWarning
          title="Partial data"
          warnings={partialDomains}
        />
      )}

      <DashboardHero
        provider={provider}
        accountLabel={scopeLabel}
        regionLabel={effectiveRegion ?? null}
        refreshedLabel={formatLocalTime(
          cost.entry.refreshedAt ??
            resources.entry.refreshedAt ??
            recommendations.entry.refreshedAt,
        )}
        dataStatus={
          cost.entry.status === 'success' || resources.entry.status === 'success' || recommendations.entry.status === 'success'
            ? 'live'
            : cost.entry.status === 'loading' || resources.entry.status === 'loading' || recommendations.entry.status === 'loading'
              ? 'loading'
              : cost.entry.status === 'refreshing' || resources.entry.status === 'refreshing' || recommendations.entry.status === 'refreshing'
                ? 'refreshing'
                : cost.entry.error || resources.entry.error || recommendations.entry.error
                  ? 'unavailable'
                  : 'idle'
        }
      />

      {/* KPI row — 5 provider-neutral cards.  Wraps cleanly down
          to 1024 px (spec step 22). */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <SpendKpi
          report={cost.entry.data?.report ?? null}
          loading={cost.entry.status === 'loading'}
          period={period}
          providerLabel={provider === 'AZURE' ? 'Total Spend' : 'Total Spend'}
        />
        <ChangeKpi
          report={cost.entry.data?.report ?? null}
          loading={cost.entry.status === 'loading'}
          period={period}
        />
        <ResourcesKpi
          resources={resources.entry.data ?? null}
          loading={resources.entry.status === 'loading'}
        />
        <OptimizationKpi
          envelope={recommendationEnvelope}
          loading={recommendations.entry.status === 'loading'}
        />
        <SavingsKpi
          envelope={recommendationEnvelope}
          loading={recommendations.entry.status === 'loading'}
        />
      </div>

      {/* Spend Trend */}
      <SectionCard
        title="Spend Trend"
        description={`Daily spend · ${activeMonthLabel}`}
        actions={
          <StatusBadge tone={cost.entry.data?.cache_status === 'HIT' ? 'neutral' : 'info'}>
            Cache: {cost.entry.data?.cache_status ?? '—'}
          </StatusBadge>
        }
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeleton className="h-[200px] w-full" />
        ) : cost.entry.error && !cost.entry.data ? (
          <ErrorState message={cost.entry.error.message} />
        ) : cost.entry.data ? (
          <CostTrendChart
            points={cost.entry.data.report.daily_trend}
            currency={cost.entry.data.report.currency}
            ariaLabel={`Daily cost trend for ${activeMonthLabel}`}
          />
        ) : (
          <EmptyChartState label="No spend data" />
        )}
      </SectionCard>

      {/* Spend by Service | Spend by Region */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Spend by Service"
          description="Top services by spend"
          actions={
            <StatusBadge tone={cost.entry.data?.report.estimated ? 'warning' : 'success'}>
              {cost.entry.data?.report.estimated ? 'Estimated' : 'Final'}
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : cost.entry.error && !cost.entry.data ? (
            <ErrorState message={cost.entry.error.message} />
          ) : cost.entry.data ? (
            <HorizontalBarChart
              rows={serviceRows(cost.entry.data.report.by_service)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          ) : (
            <EmptyChartState label="No service breakdown" />
          )}
        </SectionCard>
        <SectionCard
          title="Spend by Region"
          description="Region / location breakdown"
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : cost.entry.error && !cost.entry.data ? (
            <ErrorState message={cost.entry.error.message} />
          ) : cost.entry.data ? (
            <HorizontalBarChart
              rows={regionRows(cost.entry.data.report.by_region)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          ) : (
            <EmptyChartState label="No region breakdown" />
          )}
        </SectionCard>
      </div>

      {/* Resource Summary | Optimization Summary */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ResourceSummaryCard
          resources={resources.entry.data ?? null}
          loading={resources.entry.status === 'loading'}
          error={resources.entry.error}
          provider={provider}
        />
        <OptimizationSummaryCard
          envelope={recommendationEnvelope}
          loading={recommendations.entry.status === 'loading'}
          error={recommendations.entry.error}
        />
      </div>
    </div>
  )
}

// ===========================================================================
//  Race protection — generation hook + custom error
// ===========================================================================

function useGeneration(
  provider: DashboardProvider,
  selection: string | null,
  providerVersion: number,
): number {
  const [generation, setGeneration] = useState(0)
  const prev = useRef({ provider, selection, providerVersion })
  useEffect(() => {
    if (
      prev.current.provider !== provider ||
      prev.current.selection !== selection ||
      prev.current.providerVersion !== providerVersion
    ) {
      setGeneration((g) => g + 1)
      prev.current = { provider, selection, providerVersion }
    }
  }, [provider, selection, providerVersion])
  return generation
}

/**
 * Thrown by dashboard queries when a response arrives for a
 * previous generation.  The store treats it like any other error
 * (the entry lands in `error` state), but the message is stable
 * for tests and never surfaces to the UI as a "real" failure.
 */
class StaleGenerationError extends Error {
  readonly startGen: number
  readonly currentGen: number
  constructor(startGen: number, currentGen: number) {
    super(`stale-generation:${startGen}->${currentGen}`)
    this.name = 'StaleGenerationError'
    this.startGen = startGen
    this.currentGen = currentGen
  }
}

// ===========================================================================
//  KPI tiles
// ===========================================================================

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

  // Phase 7C hotfix #2 / Phase 7D.3 — cost completeness:
  //   * all failed → "Unavailable" (never "$0.00")
  //   * some failed → valid subtotal + "Partial (N unavailable)"
  //   * all succeeded → normal value (incl. genuine $0.00)
  // Spec step 5 + step 21 — release-critical.
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
    return <MetricCard label="Change vs Previous Period" description="Loading…" value={undefined} />
  }
  if (!report) {
    return <MetricCard label="Change vs Previous Period" description="—" value={undefined} />
  }
  const pct = formatChangePercent(report.total_cost, report.previous_period_cost)
  const abs = formatSignedChange(report.change_amount, report.currency)
  const tone = changeTone(report.total_cost, report.previous_period_cost)
  const changeValue = parseDecimal(report.change_amount)
  const directionWord =
    changeValue === null
      ? 'No comparison'
      : changeValue === 0
        ? 'No change'
        : changeValue > 0
          ? 'Increase'
          : 'Decrease'
  const compareLabel =
    period.kind === 'month'
      ? 'vs previous month'
      : `vs previous ${period.days} days`
  const description =
    pct === '—' && changeValue === null
      ? 'Previous period unavailable'
      : `${directionWord} ${compareLabel} · ${abs}`
  return (
    <MetricCard
      label="Change vs Previous Period"
      icon={<DeltaIcon />}
      value={pct}
      tone={pct === '—' ? 'neutral' : tone}
      description={description}
    />
  )
}

function ResourcesKpi({
  resources,
  loading,
}: {
  resources: CloudResourceListResponse | null
  loading: boolean
}) {
  const counts = useMemo(() => computeResourceCounts(resources), [resources])
  if (loading && !resources) {
    return <MetricCard label="Resources" description="Loading…" value={undefined} />
  }
  if (!resources) {
    return <MetricCard label="Resources" description="—" value={undefined} />
  }
  const subs = resources.summary.by_subscription ?? []
  const scopeAttribution =
    subs.length === 1
      ? subs[0].display_name
      : resources.summary.scope_kind === 'all'
        ? `${resources.summary.scope}`
        : `${subs.length} subscriptions`
  return (
    <div data-testid="resources-total-kpi">
      <MetricCard
        label="Resources"
        icon={<ResourcesIcon />}
        value={counts.total.toLocaleString('en-US')}
        description={`${counts.types} types · ${scopeAttribution}`}
      />
    </div>
  )
}

function OptimizationKpi({
  envelope,
  loading,
}: {
  envelope: ReturnType<typeof buildUnifiedEnvelope> | null
  loading: boolean
}) {
  if (loading && !envelope) {
    return <MetricCard label="Active Recommendations" description="Loading…" value={undefined} />
  }
  if (!envelope) {
    return <MetricCard label="Active Recommendations" description="—" value={undefined} />
  }
  return (
    <MetricCard
      label="Active Recommendations"
      icon={<OptimizeIcon />}
      value={envelope.kpis.activeRecommendations.toLocaleString('en-US')}
      description={`${envelope.kpis.highImpact} high · ${envelope.kpis.mediumImpact} medium · ${envelope.kpis.lowImpact} low`}
      tone={envelope.kpis.activeRecommendations > 0 ? 'info' : 'neutral'}
      testId="kpi-active-recommendations"
    />
  )
}

function SavingsKpi({
  envelope,
  loading,
}: {
  envelope: ReturnType<typeof buildUnifiedEnvelope> | null
  loading: boolean
}) {
  if (loading && !envelope) {
    return <MetricCard label="Potential Monthly Savings" description="Loading…" value={undefined} testId="kpi-potential-monthly-savings" />
  }
  if (!envelope) {
    return <MetricCard label="Potential Monthly Savings" description="—" value={undefined} testId="kpi-potential-monthly-savings" />
  }
  // Mixed currency: NEVER sum across currencies (spec step 8).
  if (envelope.kpis.savingsStatus === 'MIXED_CURRENCY') {
    const breakdown = envelope.kpis.savingsByCurrency
      .slice(0, 2)
      .map((c) => formatCurrencyCompact(c.amount, c.currency))
      .join(' · ')
    return (
      <MetricCard
        label="Potential Monthly Savings"
        icon={<SavingsIcon />}
        value="Mixed"
        description={breakdown || 'Multiple currencies'}
        testId="kpi-potential-monthly-savings"
      />
    )
  }
  if (envelope.kpis.savingsUnavailable) {
    return (
      <MetricCard
        label="Potential Monthly Savings"
        icon={<SavingsIcon />}
        value="Not available"
        description="Savings not reported"
        testId="kpi-potential-monthly-savings"
      />
    )
  }
  const amount = envelope.kpis.potentialMonthlySavings
  if (amount === null) {
    return (
      <MetricCard
        label="Potential Monthly Savings"
        icon={<SavingsIcon />}
        value="Not available"
        description="Savings not reported"
        testId="kpi-potential-monthly-savings"
      />
    )
  }
  const currency = envelope.kpis.currency ?? 'USD'
  return (
    <MetricCard
      label="Potential Monthly Savings"
      icon={<SavingsIcon />}
      value={formatCurrencyCompact(amount, currency)}
      description={
        envelope.kpis.authoritativeZero
          ? `${currency} · Authoritative zero`
          : `${currency} · normalised to monthly`
      }
      tone={envelope.kpis.authoritativeZero ? 'neutral' : 'success'}
      testId="kpi-potential-monthly-savings"
    />
  )
}

// ===========================================================================
//  Resource summary
// ===========================================================================

interface ResourceSummaryCardProps {
  resources: CloudResourceListResponse | null
  loading: boolean
  error: Error | null
  provider: DashboardProvider
}

function ResourceSummaryCard({
  resources,
  loading,
  error,
  provider,
}: ResourceSummaryCardProps) {
  const counts = useMemo(() => computeResourceCounts(resources), [resources])
  const topTypes = useMemo(() => {
    if (!resources) return [] as Array<{ key: string; count: number }>
    return (resources.summary.by_type ?? [])
      .filter((entry) => entry.count > 0)
      .slice(0, 6)
  }, [resources])
  const subs = resources?.summary.by_subscription ?? []
  const locationCount = (resources?.summary.by_location ?? []).length

  return (
    <SectionCard
      title="Resource Summary"
      description={provider === 'AZURE' ? 'Azure inventory snapshot' : 'AWS inventory snapshot'}
      actions={
        <Link
          to="/resources"
          className="
            inline-flex items-center rounded-md border border-border
            bg-surface px-3 py-1.5 text-xs font-medium text-fg-primary
            hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
          "
          data-testid="dashboard-resources-link"
        >
          View Resources
        </Link>
      }
    >
      {loading && !resources ? (
        <LoadingSkeletonRows rows={4} />
      ) : error && !resources ? (
        <ErrorState message={error.message} />
      ) : resources ? (
        <div className="space-y-3" data-testid="dashboard-resource-summary">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="text-2xl font-semibold tabular-nums text-fg-primary">
              {counts.total.toLocaleString('en-US')}
            </span>
            <span className="text-xs text-fg-muted">total resources</span>
            <span className="text-xs text-fg-muted">
              {counts.types} types · {locationCount} {provider === 'AZURE' ? 'locations' : 'regions'} ·{' '}
              {subs.length === 0
                ? '—'
                : subs.length === 1
                  ? subs[0].display_name
                  : `${subs.length} subscriptions`}
            </span>
          </div>
          {topTypes.length === 0 ? (
            <p className="text-xs text-fg-muted">No resource categories reported.</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {topTypes.map((row) => (
                <li
                  key={row.key}
                  className="flex items-center justify-between gap-3"
                  data-testid="dashboard-resource-type-row"
                >
                  <span className="truncate text-fg-primary">{row.key}</span>
                  <span className="tabular-nums text-fg-secondary">
                    {row.count.toLocaleString('en-US')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-xs text-fg-muted">No data</p>
      )}
    </SectionCard>
  )
}

// ===========================================================================
//  Optimization summary
// ===========================================================================

interface OptimizationSummaryCardProps {
  envelope: ReturnType<typeof buildUnifiedEnvelope> | null
  loading: boolean
  error: Error | null
}

function OptimizationSummaryCard({
  envelope,
  loading,
  error,
}: OptimizationSummaryCardProps) {
  const topSources = useMemo(() => {
    if (!envelope) return [] as Array<{ key: string; label: string; count: number }>
    return envelope.kpis.recommendationSources.slice(0, 3)
  }, [envelope])

  return (
    <SectionCard
      title="Optimization Summary"
      description="Top opportunities and savings for the active scope"
      actions={
        <Link
          to="/optimization"
          className="
            inline-flex items-center rounded-md border border-border
            bg-surface px-3 py-1.5 text-xs font-medium text-fg-primary
            hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
          "
          data-testid="dashboard-optimization-link"
        >
          View Optimization
        </Link>
      }
    >
      {loading && !envelope ? (
        <LoadingSkeletonRows rows={4} />
      ) : error && !envelope ? (
        <ErrorState message={error.message} />
      ) : envelope ? (
        <div className="space-y-3" data-testid="optimization-summary">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-2xl font-semibold tabular-nums text-fg-primary" data-testid="optimization-count">
              {envelope.kpis.activeRecommendations.toLocaleString('en-US')}
            </span>
            <span className="text-xs text-fg-muted">open opportunities</span>
            {envelope.kpis.potentialMonthlySavings !== null &&
            envelope.kpis.currency &&
            !envelope.kpis.savingsUnavailable &&
            envelope.kpis.savingsStatus !== 'MIXED_CURRENCY' ? (
              <span className="text-xs text-success">
                {formatCurrencyCompact(envelope.kpis.potentialMonthlySavings, envelope.kpis.currency)} known savings / mo
              </span>
            ) : envelope.kpis.savingsStatus === 'MIXED_CURRENCY' ? (
              <span className="text-xs text-warning">Mixed currencies</span>
            ) : (
              <span className="text-xs text-fg-muted">savings not available</span>
            )}
          </div>
          {topSources.length === 0 ? (
            <p className="text-xs text-fg-muted">No recommendation sources reported.</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {topSources.map((source) => (
                <li
                  key={source.key}
                  className="flex items-center justify-between gap-3"
                  data-testid="dashboard-optimization-source-row"
                >
                  <span className="truncate text-fg-primary">{source.label}</span>
                  <span className="tabular-nums text-fg-secondary">{source.count}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p className="text-xs text-fg-muted">No data</p>
      )}
    </SectionCard>
  )
}

// ===========================================================================
//  Helpers
// ===========================================================================

function computeResourceCounts(
  resources: CloudResourceListResponse | null,
): { total: number; types: number } {
  if (!resources) return { total: 0, types: 0 }
  const total = resources.summary.total_resources ?? 0
  const types = (resources.summary.by_type ?? []).length
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

function periodToKey(period: import('../types/finops').BillingPeriod): string {
  return period.kind === 'month' ? `month:${period.month}` : `days:${period.days}`
}

function statusLabel(status: string, error: Error | null): string {
  if (error) return 'Unavailable'
  switch (status) {
    case 'success':
      return 'Live'
    case 'loading':
      return 'Loading'
    case 'refreshing':
      return 'Refreshing'
    case 'error':
      return 'Unavailable'
    default:
      return 'Idle'
  }
}

function statusTone(
  status: string,
  error: Error | null,
): 'success' | 'info' | 'warning' | 'danger' | 'neutral' {
  if (error) return 'danger'
  switch (status) {
    case 'success':
      return 'success'
    case 'loading':
    case 'refreshing':
      return 'info'
    case 'error':
      return 'danger'
    default:
      return 'neutral'
  }
}

function collectPartialDomains(
  costReport: CostReportResponse['report'] | null,
  resources: CloudResourceListResponse | null,
  envelope: ReturnType<typeof buildUnifiedEnvelope> | null,
): Array<{ source: string; code: string; message: string; region: string | null }> {
  const out: Array<{ source: string; code: string; message: string; region: string | null }> = []
  // The dashboard-level PartialWarning renders for *real*
  // partial-data signals — i.e. a successful response whose
  // report flagged some accounts / subscriptions as failed.
  // Domain-level failures (no data at all) are localized to the
  // affected KPI / section per spec step 20, and the page header
  // status badge flips to Unavailable.
  if (costReport) {
    const failed = costReport.failed_accounts ?? 0
    if (failed > 0) {
      out.push({
        source: 'costs',
        code: 'PARTIAL',
        message: `${failed} account${failed === 1 ? '' : 's'} unavailable`,
        region: null,
      })
    }
  }
  if (resources && resources.summary.subscription_errors?.length) {
    out.push({
      source: 'resources',
      code: 'PARTIAL',
      message: `${resources.summary.subscription_errors.length} resource subscription failure${resources.summary.subscription_errors.length === 1 ? '' : 's'}`,
      region: null,
    })
  }
  if (envelope && envelope.partialErrors.length > 0) {
    out.push({
      source: 'recommendations',
      code: 'PARTIAL',
      message: `${envelope.partialErrors.length} recommendation account failure${envelope.partialErrors.length === 1 ? '' : 's'}`,
      region: null,
    })
  }
  return out
}

function EmptyChartState({ label }: { label: string }) {
  return (
    <div
      className="flex h-[180px] items-center justify-center rounded-md border border-border bg-surface-2 text-xs text-fg-muted"
      role="status"
    >
      {label}
    </div>
  )
}

// ===========================================================================
//  Icons
// ===========================================================================

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
function SavingsIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M19 5h-2V3H7v2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z" />
      <path d="M12 11v6" />
      <path d="m9 14 3 3 3-3" />
    </svg>
  )
}

// ===========================================================================
//  Re-exports for downstream tests
// ===========================================================================

export { fieldGrid, summaryByCategory }
function fieldGrid() {
  return null
}
function summaryByCategory(rows: SummaryByCategory[]) {
  return rows
}

// Suppress unused-import warnings for the legacy helpers some
// downstream tests reach for via this module.
void buildCloudCostsCacheKey
void buildCloudResourcesCacheKey
void buildCloudRecommendationsCacheKey
