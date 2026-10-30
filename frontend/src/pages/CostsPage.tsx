// Phase 7D.3.2 — Costs page wired to calendar-month billing with
// shared cost-completeness semantics.
//
// This page consumes the SAME canonical envelope as the unified
// Dashboard and applies the SAME completeness derivation.  Before
// Phase 7D.3.2 the page rendered raw ``report.total_cost`` even
// when the canonical response was ``status='PARTIAL_SUCCESS'`` /
// ``successful_accounts=0`` (every Azure subscription throttled),
// which produced a fake "$0.00" as authoritative spend.
//
// Phase 7D.3.2 changes:
//
//   * The cost KPI tiles go through ``deriveCostCompleteness`` —
//     a PARTIAL_SUCCESS with zero successful scopes renders
//     "Unavailable", not "$0.00".
//   * The page tracks a ``generation`` token (same pattern as the
//     Dashboard) so a stale request can't overwrite fresh state.
//     Stale responses raise ``StaleGenerationError``, which the
//     store now silently discards (no user-facing error).
//   * The last-known-good snapshot survives a failed refresh; the
//     KPI tiles and charts render the LKG value with a "Stale"
//     caption instead of blanking the page.
//   * Charts distinguish "data unavailable" (upstream failure) from
//     "no data" (authoritative empty dataset) per spec step 11.
//   * Refresh invalidates with ``invalidateFinopsCacheKeepLastGood``
//     so a manual refresh that fails cannot wipe the prior good
//     snapshot.

import { useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { PeriodSelector } from '../components/PeriodSelector'
import { RegionFilter, buildRegionOptions } from '../components/RegionFilter'
import { RefreshButton } from '../components/RefreshButton'
import { CostTrendChart } from '../components/CostTrendChart'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { useFinopsQuery, invalidateFinopsCacheKeepLastGood } from '../lib/finops/store'
// Phase 9D.2 — ALL providers (AWS included) route through the
// canonical ``/api/cloud/costs`` endpoint so individual-account
// scopes speak the Phase 9D.1 canonical selector contract
// (``acct:<AWS cloud account_id>``) and never the legacy registry
// primary-key grammar of ``/api/aws/costs``.  The legacy AWS-only
// endpoint remains a backend compatibility surface only — no
// current frontend caller remains.
import {
  fetchCloudCostReport,
  adaptCloudCostReport,
  buildCloudCostsCacheKey,
} from '../lib/finops/cloudCosts'
import {
  costDomainSnapshot,
  dataReadinessLabel,
  deriveCostCompleteness,
  deriveDashboardReadiness,
  deriveDataFreshness,
  type DataReadinessKind,
} from '../lib/finops/costCompleteness'
import { useFinopsPeriod } from '../lib/finops/period'
import { useAccountContext } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import type { CostReportResponse, ServiceCost, BillingPeriod } from '../types/finops'
import { monthLabel } from '../types/finops'
import {
  changeTone,
  formatChangePercent,
  formatCurrencyCompact,
  formatCurrencyDecimal,
  formatLocalTime,
  formatSignedChange,
  parseDecimal,
} from '../lib/format'
import { regionRows, billingCycleLabel, providerCostSourceLabel, UnavailableDimensionNotice } from './DashboardPage'
import { friendlyAccountScopeErrorMessage } from '../lib/finops/subscriptionSelection'

function periodDisplay(period: BillingPeriod): string {
  return period.kind === 'month' ? monthLabel(period.month) : `Last ${period.days} days`
}

function providerSubtitle(provider: string): string {
  if (provider === 'AZURE') {
    return 'Browse Azure cost data by subscription, service and region'
  }
  if (provider === 'RACKSPACE') {
    return 'Browse Rackspace billing data for the current billing cycle, by service'
  }
  return 'Browse AWS cost data by account, service and region'
}

export function CostsPage() {
  const { period, region, currentMonth } = useFinopsPeriod()
  const { selection } = useAccountContext()
  const { provider, providerSlug, providerVersion } = useProviderContext()
  // Phase 9D.1 closure — Rackspace Billing answers only for the
  // CURRENT billing cycle and has no authoritative regional cost
  // dimension (charges are scoped to the billing account, not a
  // datacenter).  A month / region persisted while another
  // provider was active must never reach a Rackspace cost query:
  // force the effective period to the current month and the
  // region to account-level (null).  AWS / Azure behaviour is
  // unchanged.
  const effectivePeriod: BillingPeriod =
    provider === 'RACKSPACE' ? { kind: 'month', month: currentMonth } : period
  const effectiveRegion: string | null =
    provider === 'RACKSPACE' ? null : region === 'all' ? null : region
  const cacheProvider = providerSlug === 'azure'
    ? ('azure' as const)
    : providerSlug === 'rackspace'
    ? ('rackspace' as const)
    : ('aws' as const)

  // Phase 7C.2B: Azure routed through the provider-neutral
  // ``/api/cloud/costs`` endpoint while AWS kept the legacy AWS-only
  // endpoint.  Phase 9D.2 closure — the legacy AWS route resolves
  // ``acct:<digits>`` against the registry PRIMARY KEY, so a
  // canonical cloud selector like ``acct:974053642038`` 404s there.
  // Every provider now routes through the canonical endpoint, which
  // resolves ``acct:<id>`` by provider + cloud-side account_id:
  // one selector contract, no DB row ids in the browser.
  // Phase 9D.1: RACKSPACE routes through the canonical endpoint
  // (Rackspace Billing V2 via the Phase 9B.1 adapter).

  // Phase 7B: invalidate stale data when the account switches.
  // Phase 7C.1: also invalidate on provider change.
  // Phase 7D.3.2: use the keep-last-good variant so a provider /
  // scope / period switch preserves the prior authoritative
  // snapshot across the transient refresh window.
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

  const cost = useFinopsQuery<CostReportResponse>(
    buildCloudCostsCacheKey(effectivePeriod, effectiveRegion, selection, cacheProvider),
    () =>
      fetchCloudCostReport(effectivePeriod, {
        provider,
        region: effectiveRegion,
        accountId: selection,
      }).then(adaptCloudCostReport),
  )

  const report = cost.entry.data?.report ?? null
  // Phase 9D.1 — Rackspace Billing answers only for the CURRENT
  // billing cycle; the canonical envelope carries the ACTUAL source
  // period.  Derive the display label from the report (falling back
  // to the last-known-good snapshot) so the page never labels a
  // billing cycle as a calendar month.
  const reportSource = report ?? cost.entry.lastGood?.report ?? null
  const rackspaceCycleLabel = reportSource
    ? billingCycleLabel(reportSource.period)
    : 'Current Billing Cycle'
  const periodText =
    provider === 'RACKSPACE' ? rackspaceCycleLabel : periodDisplay(period)
  const regionOptions = useMemo(
    () => buildRegionOptions(cost.entry.data?.report.by_region.map((r) => r.region) ?? []),
    [cost.entry.data],
  )

  // Phase 7D.3.2 — single readiness derivation shared with the
  // Dashboard.  The page header status badge uses the same label
  // derivation as the rest of the app, so the standalone Costs
  // page can never disagree with the Dashboard.
  const costSnapshot = costDomainSnapshot(cost.entry.data, cost.entry.status, cost.entry.error)
  const readiness: DataReadinessKind = deriveDashboardReadiness([costSnapshot])
  const readinessText = dataReadinessLabel(readiness)
  const readinessTone = readinessToneFor(readiness)
  const completeness = deriveCostCompleteness(report)
  const hasLastGood = !!cost.entry.lastGood && deriveCostCompleteness(cost.entry.lastGood.report).kind !== 'unavailable'
  const showStale = !!cost.entry.error && hasLastGood

  // The chart / KPI rendering branches on the same completeness
  // view-model the Dashboard uses, so the two pages cannot drift.
  const renderableReport = (() => {
    if (completeness.kind !== 'unavailable') return report
    if (hasLastGood) return cost.entry.lastGood?.report ?? null
    return null
  })()

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Costs"
        subtitle={providerSubtitle(provider)}
        context={
          <>
            <span className="text-fg-muted">Period</span>
            <span className="text-fg-secondary">{periodText}</span>
            <span className="text-fg-muted">Currency</span>
            <span className="text-fg-secondary">{report?.currency ?? cost.entry.lastGood?.report.currency ?? '—'}</span>
            <span className="text-fg-muted">Cache</span>
            <span className="text-fg-secondary">{cost.entry.data?.cache_status ?? '—'}</span>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">{formatLocalTime(cost.entry.refreshedAt)}</span>
            <span className="text-fg-muted">Status</span>
            <span data-testid="costs-status-badge">
              <StatusBadge tone={readinessTone}>{readinessText}</StatusBadge>
            </span>
          </>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <PeriodSelector />
            <RegionFilter options={regionOptions} />
            <RefreshButton onClick={cost.refresh} disabled={isInFlight(cost.entry.status)} />
          </div>
        }
      />

      {cost.entry.error && !cost.entry.data && !hasLastGood && (
        <ErrorState
          title="Could not load costs"
          message={
            friendlyAccountScopeErrorMessage(cost.entry.error, provider) ??
            sanitiseUserMessage(cost.entry.error.message)
          }
          onRetry={cost.refresh}
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SpendKpi
          report={report}
          lastGood={cost.entry.lastGood?.report ?? null}
          loading={cost.entry.status === 'loading'}
          period={effectivePeriod}
          stale={showStale}
          freshness={deriveDataFreshness(cost.entry.data)}
          labelOverride={provider === 'RACKSPACE' ? rackspaceCycleLabel : null}
        />
        <ChangeKpi
          report={renderableReport}
          loading={cost.entry.status === 'loading'}
          period={effectivePeriod}
        />
        <TopServiceKpi
          report={renderableReport}
          loading={cost.entry.status === 'loading'}
          currency={renderableReport?.currency ?? 'USD'}
          stale={showStale}
        />
      </div>

      <SectionCard
        title="Cost Trend"
        description={`Daily spend · ${periodText}`}
        actions={
          <StatusBadge tone={report?.estimated ? 'warning' : 'success'}>
            {report?.estimated ? 'Estimated' : 'Final'}
          </StatusBadge>
        }
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeleton className="h-[220px] w-full" />
        ) : cost.entry.error && !cost.entry.data && !hasLastGood ? (
          <ErrorState
            title="Cost trend unavailable"
            message={
              friendlyAccountScopeErrorMessage(cost.entry.error, provider) ??
              `${providerCostSourceLabel(provider)} request failed.`
            }
          />
        ) : provider === 'RACKSPACE' && renderableReport ? (
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
        ) : renderableReport ? (
          <CostTrendChart
            points={renderableReport.daily_trend}
            currency={renderableReport.currency}
            ariaLabel={`Daily cost trend for ${periodText}`}
          />
        ) : (
          <p className="text-xs text-fg-muted">No spend data</p>
        )}
      </SectionCard>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Cost by Service"
          description="Top services by spend"
          actions={
            <StatusBadge tone="neutral">
              {renderableReport?.by_service.length ?? 0} services
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : cost.entry.error && !cost.entry.data && !hasLastGood ? (
            <ErrorState
              title="Service breakdown unavailable"
              message="Service breakdown request failed."
            />
          ) : renderableReport ? (
            <HorizontalBarChart
              rows={serviceRows(renderableReport.by_service)}
              currency={renderableReport.currency}
              maxRows={10}
              showShare
              safeShare
            />
          ) : (
            <p className="text-xs text-fg-muted">No service breakdown</p>
          )}
        </SectionCard>
        <SectionCard
          title={provider === 'AZURE' ? 'Cost by Region / Location' : 'Cost by Region'}
          description={
            provider === 'AZURE'
              ? 'Azure ResourceLocation distribution'
              : 'Regional distribution'
          }
          actions={
            <StatusBadge tone="neutral">
              {provider === 'RACKSPACE'
                ? 'Not available'
                : `${renderableReport?.by_region.length ?? 0} regions`}
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : cost.entry.error && !cost.entry.data && !hasLastGood ? (
            <ErrorState
              title="Region breakdown unavailable"
              message="Region breakdown request failed."
            />
          ) : provider === 'RACKSPACE' && renderableReport ? (
            // Phase 9D.1 — regional granularity is an unsupported
            // dimension for Rackspace Billing (charges are scoped
            // to the billing account, not a datacenter).
            <UnavailableDimensionNotice
              testId="rackspace-region-unavailable"
              title="Regional cost breakdown not available"
              message="Not available for the current Rackspace Billing integration. Rackspace Billing scopes charges to the billing account, not to individual datacenters."
            />
          ) : renderableReport ? (
            <HorizontalBarChart
              rows={regionRows(renderableReport.by_region)}
              currency={renderableReport.currency}
              maxRows={8}
              showShare
              safeShare
            />
          ) : (
            <p className="text-xs text-fg-muted">No region breakdown</p>
          )}
        </SectionCard>
      </div>

      <SectionCard
        title="Service Breakdown"
        description={`Top services for ${periodText}`}
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeletonRows rows={6} />
        ) : renderableReport ? (
          <ServiceBreakdownTable
            services={renderableReport.by_service}
            currency={renderableReport.currency}
            total={renderableReport.total_cost}
          />
        ) : cost.entry.error && !hasLastGood ? (
          <ErrorState
            title="Service breakdown unavailable"
            message="Service breakdown request failed."
          />
        ) : (
          <p className="text-xs text-fg-muted">No service breakdown</p>
        )}
      </SectionCard>
    </div>
  )
}

function SpendKpi({
  report,
  lastGood,
  loading,
  period,
  stale,
  freshness,
  labelOverride,
}: {
  report: CostReportResponse['report'] | null
  lastGood: CostReportResponse['report'] | null
  loading: boolean
  period: BillingPeriod
  stale: boolean
  freshness?: ReturnType<typeof deriveDataFreshness>
  /** Phase 9D.1 — provider-inherent period label.  Rackspace
   *  billing cycles are NOT calendar months; the caller passes the
   *  actual source-cycle label so the KPI never claims a calendar
   *  month for a billing cycle. */
  labelOverride?: string | null
}) {
  const label =
    labelOverride ??
    (period.kind === 'month' ? monthLabel(period.month) : `Last ${period.days} days`)
  const completeness = deriveCostCompleteness(report)
  const fresh = freshness ?? deriveDataFreshness(null)
  // Phase 7D.3.3 — if the report is unavailable but the server
  // told us ``data_freshness='STALE'`` (the canonical Azure
  // cache layer served an LKG), surface that fact in the
  // subtitle so the user understands why the page still has a
  // value despite the failure.
  if (loading && !report) return <MetricCard label="Total spend" value={undefined} description="Loading…" />
  if (completeness.kind === 'unavailable') {
    if (lastGood && deriveCostCompleteness(lastGood).kind !== 'unavailable') {
      const staleDesc = fresh.kind === 'stale'
        ? `Stale · last successful ${fresh.description.replace(/^Stale\s*·\s*last successful\s*/, '')} · refresh unavailable`
        : `${label} · ${lastGood.currency} · Stale · refresh unavailable`
      return (
        <MetricCard
          label="Total spend"
          value={formatCurrencyCompact(lastGood.total_cost, lastGood.currency)}
          description={staleDesc}
          tone="warning"

        />
      )
    }
    return (
      <MetricCard
        label="Total spend"
        value="Unavailable"
        description={stale ? `${label} · no data · Stale` : `${label} · no data`}
        tone="warning"

      />
    )
  }
  if (completeness.kind === 'partial') {
    const live = report as NonNullable<CostReportResponse['report']>
    return (
      <MetricCard
        label="Total spend"
        value={formatCurrencyCompact(live.total_cost, live.currency)}
        description={`${label} · ${live.currency} · Partial (${completeness.failedAccounts} unavailable)`}
        tone="warning"

      />
    )
  }
  const live = report as NonNullable<CostReportResponse['report']>
  // Phase 7D.3.3 — for STALE / CACHED, surface the freshness
  // caption so the user can tell the data isn't brand-new.
  let description = `${label} · ${live.currency}`
  if (fresh.kind === 'stale') {
    description = `${label} · ${live.currency} · ${fresh.description}`
  } else if (fresh.kind === 'cached') {
    description = `${label} · ${live.currency} · Cached`
  }
  return (
    <MetricCard
      label="Total spend"
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
  period: BillingPeriod
}) {
  if (loading && !report) return <MetricCard label="Cost change" value={undefined} description="Loading…" />
  if (!report) return <MetricCard label="Cost change" value={undefined} description="—" />
  // Phase 7D.3.3 (correctness) — when the previous-period fetch
  // failed, ``previous_period_cost`` / ``change_amount`` /
  // ``change_percent`` are all ``null``.  We MUST NOT fabricate a
  // percentage (even ``0%`` would imply "no change" against an
  // invented baseline).  Render "—" / "Not available".
  const previousUnavailable =
    report.previous_period_unavailable === true ||
    report.previous_period_cost == null
  if (previousUnavailable) {
    return (
      <MetricCard
        label="Cost change"
        value="—"
        tone="neutral"
        description="Previous period unavailable"
      />
    )
  }
  const pct = formatChangePercent(report.total_cost, report.previous_period_cost)
  const abs = formatSignedChange(report.change_amount, report.currency)
  const tone = changeTone(report.total_cost, report.previous_period_cost)
  const word =
    parseDecimal(report.change_amount) === null
      ? 'No comparison'
      : parseDecimal(report.change_amount) === 0
        ? 'No change'
        : parseDecimal(report.change_amount)! > 0
          ? 'Increase'
          : 'Decrease'
  const compare = period.kind === 'month'
    ? 'vs previous month'
    : `vs previous ${period.days} days`
  return (
    <MetricCard
      label="Cost change"
      value={pct}
      tone={tone}
      description={`${word} ${compare} · ${abs}`}
    />
  )
}

function TopServiceKpi({
  report,
  loading,
  currency,
  stale,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  currency: string
  stale: boolean
}) {
  if (loading && !report) return <MetricCard label="Top service" value={undefined} description="Loading…" />
  if (!report || report.by_service.length === 0) {
    return (
      <MetricCard
        label="Top service"
        value={undefined}
        description={stale ? '— · Stale' : '—'}
      />
    )
  }
  const top = report.by_service.slice().sort((a, b) => (parseDecimal(b.amount) ?? 0) - (parseDecimal(a.amount) ?? 0))[0]!
  return (
    <MetricCard
      label="Top service"
      value={top.service}
      description={`${formatCurrencyDecimal(top.amount, currency)} · ${report.period.days} day window`}
    />
  )
}

function serviceRows(services: ServiceCost[]): HorizontalBarRow[] {
  return services.map((s) => ({
    key: s.service,
    label: s.service,
    title: s.service,
    value: s.amount,
  }))
}

interface ServiceBreakdownRow {
  key: string
  service: string
  amount: string
  share: string
  unit: string
}

function ServiceBreakdownTable({
  services,
  currency,
  total,
}: {
  services: ServiceCost[]
  currency: string
  total: string
}) {
  const rows = useMemo(() => {
    const totalN = parseDecimal(total) ?? 0
    const sorted = services.slice().sort((a, b) => (parseDecimal(b.amount) ?? 0) - (parseDecimal(a.amount) ?? 0))
    return sorted.map<ServiceBreakdownRow>((s) => {
      const amt = parseDecimal(s.amount) ?? 0
      const share = totalN === 0 ? '—' : `${((amt / totalN) * 100).toFixed(1)}%`
      return {
        key: s.service,
        service: s.service,
        amount: s.amount,
        share,
        unit: currency,
      }
    })
  }, [services, currency, total])
  const columns: DataTableColumn<ServiceBreakdownRow>[] = [
    { key: 'service', header: 'Service', cell: (r) => <span className="truncate" title={r.service}>{r.service}</span> },
    {
      key: 'amount',
      header: 'Amount',
      numeric: true,
      cell: (r) => formatCurrencyDecimal(r.amount, r.unit),
    },
    { key: 'share', header: 'Share', numeric: true, cell: (r) => r.share },
  ]
  return (
    <DataTable columns={columns} rows={rows} rowKey={(r) => r.key} caption="Service breakdown" />
  )
}

// ---------------------------------------------------------------------------
//  Helpers (local)
// ---------------------------------------------------------------------------

function isInFlight(status: string): boolean {
  return status === 'loading' || status === 'refreshing'
}

/**
 * Sanitise an error message before it lands in the visible UI.
 * Internal control-flow signals (e.g. ``stale-generation:0->1``)
 * MUST NOT reach the end user; if such a token slips through the
 * defensive layer we replace the message with a friendly default
 * instead of leaking implementation detail.
 */
function sanitiseUserMessage(message: string | undefined): string {
  if (!message) return 'Cost request failed.'
  if (/stale-generation/i.test(message)) return 'Cost request superseded by a fresher one.'
  return message
}

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

/**
 * Phase 7D.3.2 — generation token bumped on every change that
 * should invalidate a stale in-flight cost request.  Captured at
 * fetch-start; the store discards the response if the captured
 * generation differs from the live one.
 */
function useCostPageGeneration(
  provider: string,
  selection: string | null,
  period: BillingPeriod,
  region: string | null,
  providerVersion: number,
): number {
  const periodKey = period.kind === 'month' ? `month:${period.month}` : `days:${period.days}`
  const [gen, setGen] = useState(0)
  const prev = useRef({
    provider,
    selection,
    period: periodKey,
    region,
    providerVersion,
  })
  useEffect(() => {
    if (
      prev.current.provider !== provider ||
      prev.current.selection !== selection ||
      prev.current.period !== periodKey ||
      prev.current.region !== region ||
      prev.current.providerVersion !== providerVersion
    ) {
      setGen((g) => g + 1)
      prev.current = { provider, selection, period: periodKey, region, providerVersion }
    }
  }, [provider, selection, periodKey, region, providerVersion])
  return gen
}
