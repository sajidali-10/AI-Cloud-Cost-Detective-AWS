// Phase 6B — Costs page wired to real Phase 2 Cost Explorer data.
//
// Layout mirrors the HipLink Security Operations reference: dense,
// operational, no marketing-style analytics.  Sections:
//   1. PageHeader with period + region + refresh
//   2. Top KPI row (Spend / Change / Top service)
//   3. Cost Trend (large card, full width)
//   4. Two side-by-side ranked cards: by Service, by Region
//   5. Cost Detail table — date × service × region entries for the
//      selected period (top 50 by amount)
//
// A slow CloudWatch or recommendation call does NOT blank this page
// (those services are not used here); however a failing Cost
// Explorer call is surfaced via the partial-success banner without
// zeroing the layout.

import { useMemo } from 'react'
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
import { useFinopsQuery } from '../lib/finops/store'
import { fetchCostReport, buildCostsCacheKey } from '../lib/finops/costs'
import { useFinopsPeriod } from '../lib/finops/period'
import type { CostReportResponse, ServiceCost } from '../types/finops'
import {
  changeTone,
  formatChangePercent,
  formatCurrencyCompact,
  formatCurrencyDecimal,
  formatLocalTime,
  formatSignedChange,
  parseDecimal,
  periodLabel,
} from '../lib/format'
import { regionRows } from './DashboardPage'

export function CostsPage() {
  const { days, region } = useFinopsPeriod()
  const effectiveRegion = region === 'all' ? null : region

  const cost = useFinopsQuery<CostReportResponse>(
    buildCostsCacheKey(days, effectiveRegion),
    () => fetchCostReport(days, { region: effectiveRegion }),
  )

  const report = cost.entry.data?.report ?? null
  const regionOptions = useMemo(
    () => buildRegionOptions(cost.entry.data?.report.by_region.map((r) => r.region) ?? []),
    [cost.entry.data],
  )

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Costs"
        subtitle="Browse AWS cost data by account, service and region"
        context={
          <>
            <span className="text-fg-muted">Period</span>
            <span className="text-fg-secondary">{periodLabel(days)}</span>
            <span className="text-fg-muted">Currency</span>
            <span className="text-fg-secondary">{report?.currency ?? '—'}</span>
            <span className="text-fg-muted">Cache</span>
            <span className="text-fg-secondary">{cost.entry.data?.cache_status ?? '—'}</span>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">{formatLocalTime(cost.entry.refreshedAt)}</span>
          </>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <PeriodSelector />
            <RegionFilter options={regionOptions} />
            <RefreshButton onClick={cost.refresh} />
          </div>
        }
      />

      {cost.entry.error && !cost.entry.data && (
        <ErrorState title="Could not load costs" message={cost.entry.error.message} onRetry={cost.refresh} />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SpendKpi report={report} loading={cost.entry.status === 'loading'} days={days} />
        <ChangeKpi report={report} loading={cost.entry.status === 'loading'} days={days} />
        <TopServiceKpi
          report={report}
          loading={cost.entry.status === 'loading'}
          currency={report?.currency ?? 'USD'}
        />
      </div>

      <SectionCard
        title="Cost Trend"
        description={`Daily spend · ${periodLabel(days)}`}
        actions={
          <StatusBadge tone={report?.estimated ? 'warning' : 'success'}>
            {report?.estimated ? 'Estimated' : 'Final'}
          </StatusBadge>
        }
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeleton className="h-[220px] w-full" />
        ) : (
          cost.entry.data && (
            <CostTrendChart
              points={cost.entry.data.report.daily_trend}
              currency={cost.entry.data.report.currency}
              ariaLabel={`Daily cost trend over ${periodLabel(days)}`}
            />
          )
        )}
      </SectionCard>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard
          title="Cost by Service"
          description="Top services by spend"
          actions={
            <StatusBadge tone="neutral">
              {report?.by_service.length ?? 0} services
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : (
            cost.entry.data && (
              <HorizontalBarChart
                rows={serviceRows(cost.entry.data.report.by_service)}
                currency={cost.entry.data.report.currency}
                maxRows={10}
                showShare
                safeShare
              />
            )
          )}
        </SectionCard>
        <SectionCard
          title="Cost by Region"
          description="Regional distribution"
          actions={
            <StatusBadge tone="neutral">
              {report?.by_region.length ?? 0} regions
            </StatusBadge>
          }
        >
          {cost.entry.status === 'loading' && !cost.entry.data ? (
            <LoadingSkeletonRows rows={6} />
          ) : (
            cost.entry.data && (
              <HorizontalBarChart
                rows={regionRows(cost.entry.data.report.by_region)}
                currency={cost.entry.data.report.currency}
                maxRows={8}
                showShare
                safeShare
              />
            )
          )}
        </SectionCard>
      </div>

      <SectionCard
        title="Service Breakdown"
        description={`Top services for ${periodLabel(days)}`}
      >
        {cost.entry.status === 'loading' && !cost.entry.data ? (
          <LoadingSkeletonRows rows={6} />
        ) : (
          cost.entry.data && (
            <ServiceBreakdownTable
              services={cost.entry.data.report.by_service}
              currency={cost.entry.data.report.currency}
              total={cost.entry.data.report.total_cost}
            />
          )
        )}
      </SectionCard>
    </div>
  )
}

function SpendKpi({
  report,
  loading,
  days,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  days: number
}) {
  if (loading && !report) return <MetricCard label="Total spend" value={undefined} description="Loading…" />
  if (!report) return <MetricCard label="Total spend" value={undefined} description="—" />
  return (
    <MetricCard
      label="Total spend"
      value={formatCurrencyCompact(report.total_cost, report.currency)}
      description={`${periodLabel(days)} · ${report.currency}`}
    />
  )
}

function ChangeKpi({
  report,
  loading,
  days,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  days: number
}) {
  if (loading && !report) return <MetricCard label="Cost change" value={undefined} description="Loading…" />
  if (!report) return <MetricCard label="Cost change" value={undefined} description="—" />
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
  return (
    <MetricCard
      label="Cost change"
      value={pct}
      tone={tone}
      description={`${word} vs previous ${days} days · ${abs}`}
    />
  )
}

function TopServiceKpi({
  report,
  loading,
  currency,
}: {
  report: CostReportResponse['report'] | null
  loading: boolean
  currency: string
}) {
  if (loading && !report) return <MetricCard label="Top service" value={undefined} description="Loading…" />
  if (!report || report.by_service.length === 0) {
    return <MetricCard label="Top service" value={undefined} description="—" />
  }
  const top = report.by_service.slice().sort((a, b) => (parseDecimal(b.amount) ?? 0) - (parseDecimal(a.amount) ?? 0))[0]!
  return (
    <MetricCard
      label="Top service"
      value={top.service}
      description={`${formatCurrencyDecimal(top.amount, currency)} · ${periodLabel(report.period.days)}`}
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
