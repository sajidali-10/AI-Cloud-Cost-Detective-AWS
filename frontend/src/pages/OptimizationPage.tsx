// Phase 6B — Optimization page wired to real Phase 3 data.
//
// Sections:
//   1. PageHeader with period + refresh.
//   2. Five KPI tiles (total / high confidence / AWS-native /
//      deterministic / known savings).
//   3. Capability panel — Compute Optimizer / Cost Optimization Hub
//      / Deterministic Engine with explicit enrollment state.
//   4. Recommendation table — type-specific metadata columns,
//      null-savings → "Not available", savings source + confidence
//      badges, finding → title column.
//   5. Detail drawer — current vs recommended configuration,
//      evidence, reason codes.  NO AI explanation (Phase 6C).

import { useMemo, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { EmptyState } from '../components/EmptyState'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { PeriodSelector } from '../components/PeriodSelector'
import { RefreshButton } from '../components/RefreshButton'
import { CapabilityStatusRow } from '../components/CapabilityStatusRow'
import { RecommendationSourceBadge, ConfidenceBadge } from '../components/RecommendationSourceBadge'
import { PartialWarning } from '../components/PartialWarning'
import { useFinopsQuery } from '../lib/finops/store'
import {
  fetchCapabilities,
  fetchRecommendations,
  fetchOptimizationSummary,
  buildCapabilitiesCacheKey,
  buildRecommendationsCacheKey,
  buildSummaryCacheKey,
} from '../lib/finops/optimization'
import { useFinopsPeriod } from '../lib/finops/period'
import type {
  CapabilitiesResponse,
  OptimizationSummaryResponse,
  Recommendation,
  RecommendationsResponse,
  SavingsSource,
} from '../types/finops'
import {
  formatCurrencyCompact,
  formatCurrencyDecimal,
  formatLocalTime,
  parseDecimal,
  periodLabel,
  savingsSourceLabel,
} from '../lib/format'

export function OptimizationPage() {
  const { days, region } = useFinopsPeriod()
  const effectiveRegion = region === 'all' ? null : region

  const caps = useFinopsQuery<CapabilitiesResponse>(
    buildCapabilitiesCacheKey(effectiveRegion),
    () => fetchCapabilities(effectiveRegion),
    { ttlMs: 60_000 },
  )
  const recs = useFinopsQuery<RecommendationsResponse>(
    buildRecommendationsCacheKey(days, effectiveRegion),
    () => fetchRecommendations(days, effectiveRegion),
  )
  const summary = useFinopsQuery<OptimizationSummaryResponse>(
    buildSummaryCacheKey(days, effectiveRegion),
    () => fetchOptimizationSummary(days, effectiveRegion),
    { ttlMs: 60_000 },
  )

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = useMemo(
    () => recs.entry.data?.recommendations.find((r) => r.recommendation_id === selectedId) ?? null,
    [recs.entry.data, selectedId],
  )

  const refresh = () => {
    caps.refresh()
    recs.refresh()
    summary.refresh()
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Optimization"
        subtitle="Recommendations grouped by impact and confidence"
        context={
          <>
            <span className="text-fg-muted">Period</span>
            <span className="text-fg-secondary">{periodLabel(days)}</span>
            <span className="text-fg-muted">Status</span>
            <StatusBadge
              tone={
                recs.entry.data?.status === 'SUCCESS'
                  ? 'success'
                  : recs.entry.data?.status === 'PARTIAL_SUCCESS'
                    ? 'warning'
                    : recs.entry.data?.status === 'FAILED'
                      ? 'danger'
                      : 'neutral'
              }
            >
              {recs.entry.data?.status ?? (recs.entry.status === 'loading' ? 'Loading' : 'Idle')}
            </StatusBadge>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">{formatLocalTime(recs.entry.refreshedAt)}</span>
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            <PeriodSelector />
            <RefreshButton onClick={refresh} />
          </div>
        }
      />

      {recs.entry.error && !recs.entry.data && (
        <ErrorState
          title="Could not load recommendations"
          message={recs.entry.error.message}
          onRetry={recs.refresh}
        />
      )}

      {/* KPI tiles */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <TotalKpi summary={summary.entry.data} loading={summary.entry.status === 'loading' && !summary.entry.data} />
        <HighConfidenceKpi summary={summary.entry.data} loading={summary.entry.status === 'loading' && !summary.entry.data} />
        <AwsNativeKpi summary={summary.entry.data} loading={summary.entry.status === 'loading' && !summary.entry.data} />
        <DeterministicKpi summary={summary.entry.data} loading={summary.entry.status === 'loading' && !summary.entry.data} />
        <SavingsKpi summary={summary.entry.data} loading={summary.entry.status === 'loading' && !summary.entry.data} />
      </div>

      <SectionCard
        title="Capability status"
        description="AWS-native source enrollment and deterministic engine availability"
      >
        {caps.entry.status === 'loading' && !caps.entry.data ? (
          <LoadingSkeletonRows rows={3} />
        ) : caps.entry.data ? (
          <div className="space-y-1" data-testid="capability-rows">
            <CapabilityStatusRow label="Compute Optimizer" capability={caps.entry.data.compute_optimizer} />
            <CapabilityStatusRow
              label="Cost Optimization Hub"
              capability={caps.entry.data.cost_optimization_hub}
            />
            <CapabilityStatusRow
              label="Deterministic Engine"
              capability={caps.entry.data.deterministic_engine}
            />
            {caps.entry.data.warnings.length > 0 && (
              <PartialWarning warnings={caps.entry.data.warnings} title="Capability warnings" />
            )}
          </div>
        ) : (
          <EmptyState title="No capability data" />
        )}
      </SectionCard>

      <SectionCard
        title="Recommendations"
        description={`${(recs.entry.data?.count ?? 0).toLocaleString('en-US')} recommendations · ${periodLabel(days)}`}
      >
        {recs.entry.status === 'loading' && !recs.entry.data ? (
          <LoadingSkeletonRows rows={6} />
        ) : recs.entry.data && recs.entry.data.recommendations.length === 0 ? (
          <EmptyState
            title="No recommendations"
            description={
              recs.entry.data.warnings.length > 0
                ? 'All sources returned warnings. See above.'
                : 'No optimization opportunities were identified for this period.'
            }
          />
        ) : (
          recs.entry.data && (
            <DataTable
              columns={columns}
              rows={recs.entry.data.recommendations}
              rowKey={(r) => r.recommendation_id}
              onRowClick={(r) => setSelectedId(r.recommendation_id)}
              isRowClickable={() => true}
              emptyState={<EmptyState title="No recommendations" />}
              caption="AWS optimization recommendations"
            />
          )
        )}
        {recs.entry.data && recs.entry.data.warnings.length > 0 && (
          <div className="mt-3">
            <PartialWarning warnings={recs.entry.data.warnings} title="Optimization warnings" />
          </div>
        )}
      </SectionCard>

      {selected && (
        <SectionCard
          title="Recommendation detail"
          description={`${selected.resource_type.replace('_', ' ')} · ${selected.region}`}
          actions={
            <button
              type="button"
              className="text-xs text-fg-muted hover:text-fg-primary"
              onClick={() => setSelectedId(null)}
            >
              Close
            </button>
          }
        >
          <DetailPanel recommendation={selected} />
        </SectionCard>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// KPI tiles
// ---------------------------------------------------------------------------

function TotalKpi({ summary, loading }: { summary: OptimizationSummaryResponse | null; loading: boolean }) {
  if (loading) return <MetricCard label="Recommendations" value={undefined} description="Loading…" />
  if (!summary) return <MetricCard label="Recommendations" value={undefined} description="—" />
  return (
    <MetricCard
      label="Recommendations"
      value={summary.total_recommendations.toLocaleString('en-US')}
      description={`${summary.status.toLowerCase()} · ${periodLabel(summary.days)}`}
    />
  )
}

function HighConfidenceKpi({ summary, loading }: { summary: OptimizationSummaryResponse | null; loading: boolean }) {
  if (loading) return <MetricCard label="High confidence" value={undefined} description="Loading…" />
  if (!summary) return <MetricCard label="High confidence" value={undefined} description="—" />
  const high = summary.by_confidence.find((c) => c.key === 'HIGH')?.count ?? 0
  return <MetricCard label="High confidence" value={high.toLocaleString('en-US')} description="HIGH bucket" />
}

function AwsNativeKpi({ summary, loading }: { summary: OptimizationSummaryResponse | null; loading: boolean }) {
  if (loading) return <MetricCard label="AWS-native" value={undefined} description="Loading…" />
  if (!summary) return <MetricCard label="AWS-native" value={undefined} description="—" />
  const awsNative = summary.by_source
    .filter((s) => s.key === 'AWS_COST_OPTIMIZATION_HUB' || s.key === 'AWS_COMPUTE_OPTIMIZER')
    .reduce((sum, s) => sum + s.count, 0)
  return (
    <MetricCard
      label="AWS-native"
      value={awsNative.toLocaleString('en-US')}
      description={
        awsNative === 0
          ? 'No AWS-native source reported'
          : 'From Compute Optimizer or Cost Optimization Hub'
      }
      tone={awsNative === 0 ? 'warning' : 'info'}
    />
  )
}

function DeterministicKpi({ summary, loading }: { summary: OptimizationSummaryResponse | null; loading: boolean }) {
  if (loading) return <MetricCard label="Deterministic" value={undefined} description="Loading…" />
  if (!summary) return <MetricCard label="Deterministic" value={undefined} description="—" />
  const det = summary.by_source.find((s) => s.key === 'UNKNOWN')?.count ?? 0
  return (
    <MetricCard
      label="Deterministic"
      value={det.toLocaleString('en-US')}
      description="From the in-house engine"
    />
  )
}

function SavingsKpi({ summary, loading }: { summary: OptimizationSummaryResponse | null; loading: boolean }) {
  if (loading) return <MetricCard label="Known savings" value={undefined} description="Loading…" />
  if (!summary) return <MetricCard label="Known savings" value={undefined} description="—" />
  const savings = parseDecimal(summary.total_estimated_monthly_savings)
  if (savings === null) {
    return (
      <MetricCard
        label="Known savings"
        value={undefined}
        description={
          summary.recommendations_without_savings > 0
            ? `${summary.recommendations_without_savings} without savings`
            : 'No authoritative figure'
        }
      />
    )
  }
  return (
    <MetricCard
      label="Known savings"
      value={formatCurrencyCompact(savings, summary.currency)}
      description="Authoritative · USD/mo"
      tone="success"
    />
  )
}

// ---------------------------------------------------------------------------
// Recommendation table
// ---------------------------------------------------------------------------

const columns: DataTableColumn<Recommendation>[] = [
  {
    key: 'resource_id',
    header: 'Resource',
    cell: (r) => (
      <span className="truncate font-mono text-[11px] text-fg-primary" title={r.resource_id}>
        {r.resource_id}
      </span>
    ),
  },
  {
    key: 'resource_type',
    header: 'Type',
    cell: (r) => (
      <span className="inline-flex items-center rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-fg-secondary">
        {r.resource_type.replace('_', ' ')}
      </span>
    ),
    width: 'w-32',
  },
  { key: 'region', header: 'Region', cell: (r) => r.region, width: 'w-32' },
  {
    key: 'title',
    header: 'Finding / Action',
    cell: (r) => (
      <div className="space-y-0.5">
        <div className="text-fg-primary">{r.title}</div>
        <div className="text-[10px] text-fg-muted">{r.action.replace('_', ' ')}</div>
      </div>
    ),
  },
  {
    key: 'confidence',
    header: 'Confidence',
    cell: (r) => <ConfidenceBadge confidence={r.confidence} />,
    width: 'w-32',
  },
  {
    key: 'savings_source',
    header: 'Source',
    cell: (r) => <RecommendationSourceBadge source={r.primary_source} />,
    width: 'w-40',
  },
  {
    key: 'savings',
    header: 'Est. savings',
    numeric: true,
    cell: (r) => savingsCell(r),
    width: 'w-40',
  },
  {
    key: 'data_quality',
    header: 'Quality',
    cell: (r) => (
      <span className="text-[10px] uppercase tracking-wider text-fg-muted">{r.data_quality}</span>
    ),
    width: 'w-24',
  },
]

function savingsCell(r: Recommendation): React.ReactNode {
  const amount = parseDecimal(r.estimated_monthly_savings)
  if (amount === null) {
    return (
      <span className="text-xs text-fg-muted" data-testid="savings-not-available">
        Not available
      </span>
    )
  }
  if (amount === 0) {
    return <span className="text-xs text-fg-secondary">$0.00</span>
  }
  return (
    <span className="text-xs text-fg-primary">
      {formatCurrencyDecimal(amount, r.currency)}
      <span className="ml-1 text-fg-muted">/ mo</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

function DetailPanel({ recommendation: r }: { recommendation: Recommendation }) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2" data-testid="recommendation-detail">
      <div>
        <h3 className="text-sm font-semibold text-fg-primary">Finding</h3>
        <p className="mt-1 text-xs text-fg-secondary">{r.finding}</p>
        <p className="mt-2 text-[10px] uppercase tracking-wider text-fg-muted">Reason codes</p>
        <ul className="mt-1 flex flex-wrap gap-1">
          {r.reason_codes.length === 0 ? (
            <li className="text-xs text-fg-muted">—</li>
          ) : (
            r.reason_codes.map((c) => (
              <li
                key={c}
                className="rounded border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] text-fg-secondary"
              >
                {c}
              </li>
            ))
          )}
        </ul>
        <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">Confidence</p>
        <p className="mt-0.5 text-xs text-fg-primary">
          <ConfidenceBadge confidence={r.confidence} /> · data_quality: {r.data_quality}
        </p>
        <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">Source</p>
        <p className="mt-0.5 text-xs text-fg-primary">
          <RecommendationSourceBadge source={r.primary_source} /> ·{' '}
          {r.sources.map((s: SavingsSource) => savingsSourceLabel(s)).join(', ')}
        </p>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-fg-primary">Current configuration</h3>
        <pre className="mt-1 overflow-x-auto rounded-md border border-border bg-surface-2 p-2 text-[11px] text-fg-secondary">
          {JSON.stringify(r.current_configuration, null, 2)}
        </pre>
        <h3 className="mt-3 text-sm font-semibold text-fg-primary">Recommended configuration</h3>
        <pre className="mt-1 overflow-x-auto rounded-md border border-border bg-surface-2 p-2 text-[11px] text-fg-secondary">
          {JSON.stringify(r.recommended_configuration, null, 2)}
        </pre>
        <h3 className="mt-3 text-sm font-semibold text-fg-primary">Savings</h3>
        <p className="mt-1 text-xs text-fg-secondary">
          {parseDecimal(r.estimated_monthly_savings) === null
            ? 'Not available'
            : `${formatCurrencyDecimal(r.estimated_monthly_savings, r.currency)} / mo`}
          {r.rollback_possible === true ? ' · rollback possible' : ''}
          {r.restart_needed === true ? ' · restart needed' : ''}
        </p>
      </div>
    </div>
  )
}
