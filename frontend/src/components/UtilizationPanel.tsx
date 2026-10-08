// Phase 6B — Per-resource utilization panel.
//
// Reads CloudWatch utilization evidence and renders a compact
// metric summary.  Missing metrics render "No data" — never 0 —
// per the Phase 6B spec.
//
// Grouping is type-aware:
//   EC2        → CPU avg/max, network in/out
//   RDS        → CPU avg, connections avg, freeable memory avg
//   Lambda     → invocations sum, duration avg, errors sum, throttles sum
//   ELBv2      → request count sum, processed bytes sum
//
// Statistics: average for periodic metrics, sum for counter
// metrics.  The CloudWatch batch already returns `statistic` per
// series so we just use the first series per metric.

import type { ResourceUtilization as ResUtil } from '../types/finops'
import { LoadingSkeleton } from './LoadingSkeleton'
import { EmptyState } from './EmptyState'

export interface UtilizationPanelProps {
  resource: ResUtil | undefined
  isLoading: boolean
  error: Error | null
  /** Force a particular type (defaults to resource.resource_type). */
  typeOverride?: string
}

export function UtilizationPanel({ resource, isLoading, error, typeOverride }: UtilizationPanelProps) {
  if (isLoading) {
    return (
      <div className="space-y-2" data-testid="utilization-loading">
        <LoadingSkeleton className="h-3 w-32" />
        <LoadingSkeleton className="h-3 w-40" />
        <LoadingSkeleton className="h-3 w-36" />
      </div>
    )
  }
  if (error) {
    return (
      <EmptyState title="CloudWatch unavailable" description={error.message} />
    )
  }
  if (!resource) {
    return <EmptyState title="No utilization" description="No CloudWatch evidence for this resource." />
  }
  if (resource.metrics.length === 0) {
    return <EmptyState title="No data" description="CloudWatch returned no datapoints for this resource." />
  }

  const type = typeOverride ?? resource.resource_type
  const grouped = groupMetricsByName(resource)

  return (
    <div className="space-y-2" data-testid="utilization-panel">
      {type === 'ec2' && <Ec2Rows metrics={grouped} />}
      {type === 'rds' && <RdsRows metrics={grouped} />}
      {type === 'lambda' && <LambdaRows metrics={grouped} />}
      {(type === 'alb' || type === 'nlb' || type === 'elbv2') && <ElbRows metrics={grouped} />}
      {type !== 'ec2' && type !== 'rds' && type !== 'lambda' && type !== 'alb' && type !== 'nlb' && type !== 'elbv2' && <GenericRows metrics={grouped} />}
    </div>
  )
}

type GroupedMetrics = Record<string, { datapoints: { timestamp: string; value: string }[]; unit: string; statistic: string; data_quality: string }>

function groupMetricsByName(resource: ResUtil): GroupedMetrics {
  const out: GroupedMetrics = {}
  for (const m of resource.metrics) {
    if (out[m.metric_name]) continue
    out[m.metric_name] = {
      datapoints: m.datapoints,
      unit: m.unit,
      statistic: m.statistic,
      data_quality: m.data_quality,
    }
  }
  return out
}

function aggregate(values: { value: string }[], kind: 'avg' | 'sum' | 'max'): number | null {
  const nums: number[] = []
  for (const v of values) {
    const n = Number(v.value)
    if (Number.isFinite(n)) nums.push(n)
  }
  if (nums.length === 0) return null
  if (kind === 'sum') return nums.reduce((a, b) => a + b, 0)
  if (kind === 'max') return nums.reduce((a, b) => Math.max(a, b), -Infinity)
  return nums.reduce((a, b) => a + b, 0) / nums.length
}

function formatValue(v: number | null, unit: string): string {
  if (v === null || !Number.isFinite(v)) return 'No data'
  const formatted = v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  return unit ? `${formatted} ${unit}`.trim() : formatted
}

function MetricRow({
  label,
  value,
  quality,
}: {
  label: string
  value: string
  quality: string
}) {
  const muted = value === 'No data'
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-fg-muted">{label}</span>
      <span className={['tabular-nums', muted ? 'text-fg-muted' : 'text-fg-primary'].join(' ')} data-quality={quality}>
        {value}
      </span>
    </div>
  )
}

function Ec2Rows({ metrics }: { metrics: GroupedMetrics }) {
  const cpu = metrics['CPUUtilization']
  const netIn = metrics['NetworkIn']
  const netOut = metrics['NetworkOut']
  return (
    <>
      <MetricRow
        label="CPU (avg / max)"
        quality={cpu?.data_quality ?? 'no_data'}
        value={
          cpu
            ? `${formatValue(aggregate(cpu.datapoints, 'avg'), cpu.unit)} / ${formatValue(
                aggregate(cpu.datapoints, 'max'),
                cpu.unit,
              )}`
            : 'No data'
        }
      />
      <MetricRow
        label="Network in (avg)"
        quality={netIn?.data_quality ?? 'no_data'}
        value={netIn ? formatValue(aggregate(netIn.datapoints, 'avg'), netIn.unit) : 'No data'}
      />
      <MetricRow
        label="Network out (avg)"
        quality={netOut?.data_quality ?? 'no_data'}
        value={netOut ? formatValue(aggregate(netOut.datapoints, 'avg'), netOut.unit) : 'No data'}
      />
    </>
  )
}

function RdsRows({ metrics }: { metrics: GroupedMetrics }) {
  const cpu = metrics['CPUUtilization']
  const conns = metrics['DatabaseConnections']
  const mem = metrics['FreeableMemory']
  return (
    <>
      <MetricRow
        label="CPU (avg)"
        quality={cpu?.data_quality ?? 'no_data'}
        value={cpu ? formatValue(aggregate(cpu.datapoints, 'avg'), cpu.unit) : 'No data'}
      />
      <MetricRow
        label="Connections (avg)"
        quality={conns?.data_quality ?? 'no_data'}
        value={conns ? formatValue(aggregate(conns.datapoints, 'avg'), conns.unit) : 'No data'}
      />
      <MetricRow
        label="Freeable memory (avg)"
        quality={mem?.data_quality ?? 'no_data'}
        value={mem ? formatValue(aggregate(mem.datapoints, 'avg'), mem.unit) : 'No data'}
      />
    </>
  )
}

function LambdaRows({ metrics }: { metrics: GroupedMetrics }) {
  const inv = metrics['Invocations']
  const dur = metrics['Duration']
  const err = metrics['Errors']
  const thr = metrics['Throttles']
  return (
    <>
      <MetricRow
        label="Invocations (sum)"
        quality={inv?.data_quality ?? 'no_data'}
        value={inv ? formatValue(aggregate(inv.datapoints, 'sum'), '') : 'No data'}
      />
      <MetricRow
        label="Duration avg (ms)"
        quality={dur?.data_quality ?? 'no_data'}
        value={dur ? formatValue(aggregate(dur.datapoints, 'avg'), dur.unit) : 'No data'}
      />
      <MetricRow
        label="Errors (sum)"
        quality={err?.data_quality ?? 'no_data'}
        value={err ? formatValue(aggregate(err.datapoints, 'sum'), '') : 'No data'}
      />
      <MetricRow
        label="Throttles (sum)"
        quality={thr?.data_quality ?? 'no_data'}
        value={thr ? formatValue(aggregate(thr.datapoints, 'sum'), '') : 'No data'}
      />
    </>
  )
}

function ElbRows({ metrics }: { metrics: GroupedMetrics }) {
  const req = metrics['RequestCount']
  const bytes = metrics['ProcessedBytes']
  return (
    <>
      <MetricRow
        label="Request count (sum)"
        quality={req?.data_quality ?? 'no_data'}
        value={req ? formatValue(aggregate(req.datapoints, 'sum'), '') : 'No data'}
      />
      <MetricRow
        label="Processed bytes (sum)"
        quality={bytes?.data_quality ?? 'no_data'}
        value={bytes ? formatValue(aggregate(bytes.datapoints, 'sum'), bytes.unit) : 'No data'}
      />
    </>
  )
}

function GenericRows({ metrics }: { metrics: GroupedMetrics }) {
  const entries = Object.entries(metrics)
  if (entries.length === 0) {
    return <div className="text-xs text-fg-muted">No data</div>
  }
  return (
    <>
      {entries.map(([name, m]) => (
        <MetricRow
          key={name}
          label={`${name} (${m.statistic})`}
          quality={m.data_quality}
          value={formatValue(aggregate(m.datapoints, m.statistic === 'Sum' ? 'sum' : 'avg'), m.unit)}
        />
      ))}
    </>
  )
}
