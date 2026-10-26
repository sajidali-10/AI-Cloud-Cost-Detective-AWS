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
import {
  useFinopsQuery,
  invalidateFinopsCache,
  invalidateFinopsCacheKeepLastGood,
  StaleGenerationError,
} from '../lib/finops/store'
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
  costDomainSnapshot,
  dataReadinessLabel,
  deriveCostCompleteness,
  deriveDashboardReadiness,
  deriveDataFreshness,
  type DataReadinessKind,
  type DomainSnapshot,
} from '../lib/finops/costCompleteness'
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

  const cacheProvider: CacheProvider =
    provider === 'AZURE' ? 'azure' : provider === 'RACKSPACE' ? 'rackspace' : 'aws'
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
  //
  // Phase 7D.3.2 — switch from ``invalidateFinopsCache()`` (full
  // wipe, used by auth flows) to ``invalidateFinopsCacheKeepLastGood()``
  // so a provider / scope switch preserves the previous
  // authoritative snapshot.  If the subsequent fetch for the new
  // provider/scope fails, the UI can fall back to the previous
  // value with a "Stale" caption instead of rendering a blank /
  // $0.00 page during the transient failure window.
  const prevSelectionRef = useRef(selection)
  const prevProviderRef = useRef(cacheProvider)
  useEffect(() => {
    if (
      prevSelectionRef.current !== selection ||
      prevProviderRef.current !== cacheProvider
    ) {
      invalidateFinopsCacheKeepLastGood()
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

  // Phase 9D.1 — Rackspace Billing answers only for the CURRENT
  // billing cycle; the canonical envelope carries the ACTUAL source
  // period.  Derive the display label from the report (falling back
  // to the last-known-good snapshot) so the dashboard never calls a
  // billing cycle "This Month" / a calendar month name.
  const rackspaceReportSource =
    cost.entry.data?.report ?? cost.entry.lastGood?.report ?? null
  const rackspaceCycleLabel = rackspaceReportSource
    ? billingCycleLabel(rackspaceReportSource.period)
    : FALLBACK_CYCLE_LABEL
  // The header context item already carries the "Billing cycle"
  // label — pass the bare date range so the value is not
  // "Billing cycle: Billing cycle Oct 3 – …".
  const rackspaceCycleRange = rackspaceReportSource
    ? billingCycleRange(rackspaceReportSource.period)
    : FALLBACK_CYCLE_LABEL
  const headerPeriodLabel =
    provider === 'RACKSPACE' ? rackspaceCycleRange : activeMonthLabel

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

  // Phase 7D.3.2 — single readiness derivation shared by the page
  // header status badge AND the DashboardHero dataStatus.  Replaces
  // the per-domain OR-chain that produced contradictory labels
  // ("Unavailable" in the header, "Live" in the hero) when cost
  // failed but resources succeeded.  All three call-sites now go
  // through ``deriveDashboardReadiness`` so they cannot disagree.
  const costSnapshot = costDomainSnapshot(
    cost.entry.data,
    cost.entry.status,
    cost.entry.error,
  )
  const domains: ReadonlyArray<DomainSnapshot> = [
    costSnapshot,
    { status: resources.entry.status, error: resources.entry.error, hasData: !!resources.entry.data },
    { status: recommendations.entry.status, error: recommendations.entry.error, hasData: !!recommendations.entry.data },
  ]
  const readiness = deriveDashboardReadiness(domains)
  const readinessLabel = dataReadinessLabel(readiness)
  const readinessTone = readinessToneFor(readiness)

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
            <ContextItem
              label={provider === 'RACKSPACE' ? 'Billing cycle' : 'Month'}
              value={headerPeriodLabel}
            />
            <ContextItem
              label="Status"
              value={
                <span data-testid="dashboard-status-badge">
                  <StatusBadge tone={readinessTone}>
                    {readinessLabel}
                  </StatusBadge>
                </span>
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
            <RefreshButton
              onClick={refreshAll}
              disabled={readiness === 'loading' || readiness === 'refreshing'}
            />
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
        dataStatus={readiness}
      />

      {/* KPI row — 5 provider-neutral cards.  Wraps cleanly down
          to 1024 px (spec step 22). */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <SpendKpi
          report={cost.entry.data?.report ?? null}
          loading={cost.entry.status === 'loading'}
          period={period}
          providerLabel={provider === 'AZURE' ? 'Total Spend' : 'Total Spend'}
          lastGood={cost.entry.lastGood?.report ?? null}
          stale={!!cost.entry.error && !!cost.entry.lastGood}
          freshness={deriveDataFreshness(cost.entry.data)}
          labelOverride={provider === 'RACKSPACE' ? rackspaceCycleLabel : null}
        />
        <ChangeKpi
          report={cost.entry.data?.report ?? null}
          loading={cost.entry.status === 'loading'}
          period={period}
        />
        <ResourcesKpi
          resources={resources.entry.data ?? null}
          loading={resources.entry.status === 'loading'}
          provider={provider}
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
        description={`Daily spend · ${provider === 'RACKSPACE' ? rackspaceCycleRange : activeMonthLabel}`}
        actions={
          <StatusBadge tone={cost.entry.data?.cache_status === 'HIT' ? 'neutral' : 'info'}>
            Cache: {cost.entry.data?.cache_status ?? '—'}
          </StatusBadge>
        }
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeleton className="h-[200px] w-full" />
        ) : cost.entry.error && !cost.entry.data ? (
          // Phase 7D.3.2 — when the most-recent fetch failed AND
          // we have a safe last-known-good snapshot, render the
          // chart from lastGood with a "Stale" caption.  Otherwise
          // surface a generic "Unavailable" so the message text
          // never leaks internal control-flow signals.
          <ErrorState
            title={cost.entry.lastGood ? 'Refresh temporarily unavailable' : 'Cost data unavailable'}
            message={
              cost.entry.lastGood
                ? 'Showing last known good data while the upstream recovers.'
                : `${providerCostSourceLabel(provider)} request failed.`
            }
          />
        ) : provider === 'RACKSPACE' && cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind !== 'unavailable' ? (
          // Phase 9D.1 — daily granularity is an unsupported
          // dimension for Rackspace Billing; the envelope carries
          // an empty ``daily_trend`` by contract.  Never render a
          // fabricated trend — show the professional unavailable
          // state instead (distinct from zero / empty / error).
          <UnavailableDimensionNotice
            testId="rackspace-daily-trend-unavailable"
            title="Daily spend trend not available"
            message="Not available for the current Rackspace Billing integration. Rackspace Billing reports estimated charges for the current billing cycle without a daily breakdown."
          />
        ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind !== 'unavailable' ? (
          <CostTrendChart
            points={cost.entry.data.report.daily_trend}
            currency={cost.entry.data.report.currency}
            ariaLabel={`Daily cost trend for ${activeMonthLabel}`}
          />
        ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind === 'unavailable' ? (
          // Phase 7D.3.2 — the response was technically successful
          // but every subscription / account failed (THROTTLED,
          // AUTH, etc.).  Distinguish this from an authoritative
          // empty dataset.
          <ErrorState
            title="Cost data unavailable"
            message={`${providerCostSourceLabel(provider)} returned no successful data.`}
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
            <ErrorState
              title="Service breakdown unavailable"
              message={cost.entry.lastGood ? 'Showing last known good data.' : 'Service breakdown request failed.'}
            />
          ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind !== 'unavailable' ? (
            <HorizontalBarChart
              rows={serviceRows(cost.entry.data.report.by_service)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind === 'unavailable' ? (
            <ErrorState
              title="Service breakdown unavailable"
              message="All upstream requests failed."
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
            <ErrorState
              title="Region breakdown unavailable"
              message={cost.entry.lastGood ? 'Showing last known good data.' : 'Region breakdown request failed.'}
            />
          ) : provider === 'RACKSPACE' && cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind !== 'unavailable' ? (
            // Phase 9D.1 — regional granularity is an unsupported
            // dimension for Rackspace Billing (charges are scoped to
            // the billing account, not a datacenter).
            <UnavailableDimensionNotice
              testId="rackspace-region-unavailable"
              title="Regional cost breakdown not available"
              message="Not available for the current Rackspace Billing integration. Rackspace Billing scopes charges to the billing account, not to individual datacenters."
            />
          ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind !== 'unavailable' ? (
            <HorizontalBarChart
              rows={regionRows(cost.entry.data.report.by_region)}
              currency={cost.entry.data.report.currency}
              maxRows={10}
              showShare
              safeShare
            />
          ) : cost.entry.data && deriveCostCompleteness(cost.entry.data.report).kind === 'unavailable' ? (
            <ErrorState
              title="Region breakdown unavailable"
              message="All upstream requests failed."
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
 * `StaleGenerationError` is imported from `../lib/finops/store` so
 * the store can detect it and discard the result silently.  It is
 * NOT a user-facing error and must never reach the ErrorState UI.
 */

// ===========================================================================
//  KPI tiles
// ===========================================================================

export function SpendKpi({
  report,
  loading,
  period,
  providerLabel,
  lastGood,
  stale,
  freshness,
  labelOverride,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  period: import('../types/finops').BillingPeriod
  providerLabel: string
  /** Optional last-known-good snapshot, used as a safe fallback
   *  when the most recent fetch was throttled / failed and the
   *  current ``report`` is unavailable. */
  lastGood?: CostReportResponse['report'] | null
  /** Whether ``lastGood`` is being rendered because the current
   *  fetch failed.  Drives a "Stale" caption. */
  stale?: boolean
  /** Phase 7D.3.3 — server-emitted cache-freshness signal. */
  freshness?: ReturnType<typeof deriveDataFreshness>
  /** Phase 9D.1 — provider-inherent period label.  Rackspace
   *  billing cycles are NOT calendar months; the caller passes the
   *  actual source-cycle label so the KPI never claims "This
   *  Month" for a billing cycle. */
  labelOverride?: string | null
}) {
  const label = labelOverride ?? (period.kind === 'month' ? monthLabel(period.month) : `Last ${period.days} days`)
  const completeness = deriveCostCompleteness(report)
  const fresh = freshness ?? deriveDataFreshness(null)
  if (loading && !report) {
    return <MetricCard label={providerLabel} description="Loading…" value={undefined} />
  }
  // Phase 7D.3.2 — single source of truth for completeness.  Use
  // the canonical view-model (Status / successful / failed counts)
  // instead of the inline duplicate the previous version carried.
  // When the current report is unavailable AND we have a safe
  // last-known-good snapshot, render that with a "Stale" caption
  // rather than blanking the KPI.
  if (completeness.kind === 'unavailable') {
    if (lastGood && deriveCostCompleteness(lastGood).kind !== 'unavailable') {
      const lg = lastGood
      // Phase 7D.3.3 — when the server reports ``data_freshness='STALE'``
      // we have a precise last-successful timestamp to surface.
      const staleDesc = fresh.kind === 'stale'
        ? `${label} · ${lg.currency} · ${fresh.description} · refresh unavailable`
        : `${label} · ${lg.currency} · Stale · refresh unavailable`
      return (
        <MetricCard
          label={providerLabel}
          icon={<SpendIcon />}
          value={formatCurrencyCompact(lg.total_cost, lg.currency)}
          description={staleDesc}
          tone="warning"

        />
      )
    }
    return (
      <MetricCard
        label={providerLabel}
        icon={<SpendIcon />}
        value="Unavailable"
        description={stale ? `${label} · no subscription data · Stale` : `${label} · no subscription data`}
        tone="warning"

      />
    )
  }
  // ``report`` MUST be non-null when completeness is not 'unavailable'
  // because deriveCostCompleteness returns 'unavailable' for a null
  // input.  Narrow with a local variable so TS doesn't complain
  // about the report. fields below.
  const live = report as NonNullable<CostReportResponse['report']>
  if (completeness.kind === 'partial') {
    return (
      <MetricCard
        label={providerLabel}
        icon={<SpendIcon />}
        value={formatCurrencyCompact(live.total_cost, live.currency)}
        description={`${label} · ${live.currency} · Partial (${completeness.failedAccounts} unavailable)`}
        tone="warning"

      />
    )
  }
  // Phase 7D.3.3 — surface STALE / CACHED freshness so the user
  // can tell the data isn't brand-new.
  let description = `${label} · ${live.currency}`
  if (fresh.kind === 'stale') {
    description = `${label} · ${live.currency} · ${fresh.description}`
  } else if (fresh.kind === 'cached') {
    description = `${label} · ${live.currency} · Cached`
  }
  return (
    <MetricCard
      label={providerLabel}
      icon={<SpendIcon />}
      value={formatCurrencyCompact(live.total_cost, live.currency)}
      description={description}

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
  // Phase 7D.3.3 (correctness) — when the previous-period fetch
  // failed, ``previous_period_cost`` / ``change_amount`` /
  // ``change_percent`` are all ``null``.  We MUST NOT fabricate a
  // percentage (even ``0%`` would imply "no change" against an
  // invented baseline).  Render "—" / "Previous period unavailable".
  const previousUnavailable =
    report.previous_period_unavailable === true ||
    report.previous_period_cost == null
  if (previousUnavailable) {
    return (
      <MetricCard
        label="Change vs Previous Period"
        icon={<DeltaIcon />}
        value="—"
        tone="neutral"
        description="Previous period unavailable"
      />
    )
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
  const description = `${directionWord} ${compareLabel} · ${abs}`
  return (
    <MetricCard
      label="Change vs Previous Period"
      icon={<DeltaIcon />}
      value={pct}
      tone={tone}
      description={description}
    />
  )
}

function ResourcesKpi({
  resources,
  loading,
  provider,
}: {
  resources: CloudResourceListResponse | null
  loading: boolean
  provider: DashboardProvider
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
        : `${subs.length} ${provider === 'AZURE' ? 'subscriptions' : 'accounts'}`
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
      description={
        provider === 'AZURE'
          ? 'Azure inventory snapshot'
          : provider === 'RACKSPACE'
            ? 'Rackspace inventory snapshot'
            : 'AWS inventory snapshot'
      }
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
                  : `${subs.length} ${provider === 'AZURE' ? 'subscriptions' : 'accounts'}`}
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

/**
 * Phase 7D.3.2 — single readiness-to-tone mapping used by BOTH
 * the page-header status badge and the DashboardHero.  Replaces
 * the cost-only ``statusTone`` that was used before so the two
 * surface areas cannot disagree.
 */
function readinessToneFor(
  readiness: DataReadinessKind,
): 'success' | 'info' | 'warning' | 'danger' | 'neutral' {
  switch (readiness) {
    case 'live':
      return 'success'
    case 'loading':
    case 'refreshing':
      return 'info'
    case 'partial':
      return 'warning'
    case 'unavailable':
      return 'danger'
    case 'idle':
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

// ===========================================================================
//  Phase 9D.1 — Rackspace billing-cycle helpers
// ===========================================================================

/** Human label for the upstream cost system that produced a report. */
export function providerCostSourceLabel(provider: DashboardProvider): string {
  if (provider === 'AZURE') return 'Azure Cost Management'
  if (provider === 'RACKSPACE') return 'Rackspace Billing'
  return 'AWS Cost Explorer'
}

const MONTHS_SHORT = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]

function parseIsoDateUtc(value: string): Date | null {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatShortDate(d: Date): string {
  return `${MONTHS_SHORT[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`
}

/** Shared fallback when the envelope has not carried a source
 *  period yet (loading / error).  Title-case per the Phase 9D.1
 *  wording contract — never label a billing cycle as a calendar
 *  month. */
const FALLBACK_CYCLE_LABEL = 'Current Billing Cycle'

/** The bare date range of the source billing cycle, e.g.
 *  ``Oct 3 – Oct 26, 2026``.  Used under a "Billing cycle"
 *  context label so the value is not redundant. */
export function billingCycleRange(period: { start: string; end: string }): string {
  const start = parseIsoDateUtc(period.start)
  const endExclusive = parseIsoDateUtc(period.end)
  if (!start || !endExclusive) return FALLBACK_CYCLE_LABEL
  // The canonical end is EXCLUSIVE — display the last covered day.
  const end = new Date(endExclusive.getTime() - 86_400_000)
  return `${formatShortDate(start)} – ${formatShortDate(end)}`
}

/**
 * Phase 9D.1 — Rackspace Billing answers only for the CURRENT
 * billing cycle.  The canonical envelope exposes the ACTUAL source
 * period (``start`` inclusive, ``end`` exclusive); render that
 * window instead of the requested calendar month so the UI never
 * mislabels a billing cycle as a calendar month.
 */
export function billingCycleLabel(period: { start: string; end: string }): string {
  const range = billingCycleRange(period)
  if (range === FALLBACK_CYCLE_LABEL) return range
  return `Billing cycle ${range}`
}

/**
 * Phase 9D.1 — professional unavailable state for dimensions a
 * provider genuinely cannot serve (Rackspace daily trend / regional
 * breakdown).  Never fabricates zeros or an empty chart.
 */
export function UnavailableDimensionNotice({
  title,
  message,
  testId,
}: {
  title: string
  message: string
  testId?: string
}) {
  return (
    <div
      role="status"
      data-testid={testId}
      className="flex h-[180px] flex-col items-center justify-center gap-1 rounded-md border border-border bg-surface-2 px-6 text-center"
    >
      <span className="text-xs font-medium text-fg-secondary">{title}</span>
      <span className="max-w-md text-[11px] text-fg-muted">{message}</span>
    </div>
  )
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
