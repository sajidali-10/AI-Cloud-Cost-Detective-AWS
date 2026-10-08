// Phase 6B — Generic horizontal ranked-bar visualization.
//
// Renders a list of `{label, value}` rows with a bar whose width is
// proportional to the maximum value.  The chart is plain HTML + CSS
// so it inherits dark/light tokens automatically.  Long labels are
// truncated visually with a `title` attribute for the full text.
//
// `showShare` controls whether each row carries a percentage column
// derived from the total.  Pass `safeShare` to render only when the
// total is non-zero (used for region shares where the source totals
// differ from cost-by-service totals).

import type { ReactNode } from 'react'
import { formatCurrencyDecimal, parseDecimal, formatSharePercent } from '../lib/format'

export interface HorizontalBarRow {
  key: string
  label: ReactNode
  /** Decimal-serialized amount. */
  value: string | null
  /** Optional tooltip for truncated labels. */
  title?: string
  /** Optional inline right-side value (e.g. "$629.88"). */
  trailing?: ReactNode
  /** Optional additional context (e.g. "EBS / 27"). */
  meta?: ReactNode
  /** When true, force this row to render as the "Other" bucket. */
  isOther?: boolean
}

export interface HorizontalBarChartProps {
  rows: HorizontalBarRow[]
  currency?: string
  /** Maximum rows to render; tail rows collapse into "Other". */
  maxRows?: number
  /** Show a "% share of total" column. */
  showShare?: boolean
  /** Render only when total > 0; otherwise show "—" with safe=false. */
  safeShare?: boolean
  emptyState?: ReactNode
}

export function HorizontalBarChart({
  rows,
  currency = 'USD',
  maxRows = 10,
  showShare = true,
  safeShare = true,
  emptyState,
}: HorizontalBarChartProps) {
  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-border bg-surface-2 p-6 text-center text-xs text-fg-muted">
        {emptyState ?? 'No data'}
      </div>
    )
  }

  const collapsed = collapseRows(rows, maxRows)
  const max = collapsed.reduce((m, r) => {
    const v = parseDecimal(r.value) ?? 0
    return Math.max(m, v)
  }, 0)
  const total = collapsed.reduce((s, r) => s + (parseDecimal(r.value) ?? 0), 0)

  return (
    <div className="space-y-2" data-testid="horizontal-bar-chart">
      {collapsed.map((row) => {
        const v = parseDecimal(row.value) ?? 0
        const widthPct = max === 0 ? 0 : Math.max(2, (v / max) * 100)
        const share = formatSharePercent(v, total, { fractionDigits: 1 })
        return (
          <div
            key={row.key}
            className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 text-xs"
            data-testid="bar-row"
          >
            <div className="flex min-w-0 items-center gap-2">
              <div className="min-w-0 flex-1 truncate" title={row.title ?? (typeof row.label === 'string' ? row.label : undefined)}>
                <span className="text-fg-primary">{row.label}</span>
                {row.meta && <span className="ml-2 text-fg-muted">{row.meta}</span>}
              </div>
              <div className="relative h-2 w-32 shrink-0 overflow-hidden rounded-full bg-surface-2">
                <div
                  className={['absolute inset-y-0 left-0 rounded-full', row.isOther ? 'bg-fg-muted' : 'bg-primary'].join(' ')}
                  style={{ width: `${widthPct}%` }}
                />
              </div>
            </div>
            <div className="flex items-baseline justify-end gap-2 tabular-nums">
              {row.trailing ?? (
                <span className="text-fg-primary">{formatCurrencyDecimal(v, currency, { emptyFallback: '—' })}</span>
              )}
              {showShare && (
                <span className="w-12 text-right text-fg-muted" data-safe={share.safe}>
                  {safeShare && !share.safe ? '—' : share.text}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function collapseRows(rows: HorizontalBarRow[], maxRows: number): HorizontalBarRow[] {
  if (rows.length <= maxRows) return rows
  const head = rows.slice(0, maxRows - 1)
  const tail = rows.slice(maxRows - 1)
  const tailSum = tail.reduce((s, r) => s + (parseDecimal(r.value) ?? 0), 0)
  return [
    ...head,
    {
      key: 'other',
      label: 'Other',
      value: String(tailSum),
      meta: `${tail.length} entries`,
      isOther: true,
    },
  ]
}
