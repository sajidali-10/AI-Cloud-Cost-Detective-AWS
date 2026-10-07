// Phase 6A — MetricCard.
//
// Compact KPI tile patterned after the HipLink reference.  Renders
// label + value + description in a single column with an optional
// icon at the top-left.  No fake data — callers pass undefined for
// `value` when no data is loaded yet and the card renders a "—"
// placeholder + a neutral "No data" helper line.  This prevents
// fabricated financial numbers from appearing in Phase 6A while
// real values are deferred to Phase 6B.

import type { ReactNode } from 'react'

export interface MetricCardProps {
  label: string
  /** When undefined the card renders a "—" placeholder. */
  value?: ReactNode
  description?: ReactNode
  icon?: ReactNode
  /** Status tone for the value text. */
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'ai'
}

export function MetricCard({
  label,
  value,
  description,
  icon,
  tone = 'neutral',
}: MetricCardProps) {
  const valueText = value === undefined || value === null || value === '' ? '—' : value
  const isEmpty = value === undefined || value === null || value === ''
  return (
    <div
      className="
        flex h-full flex-col gap-2 rounded-lg border border-border
        bg-surface p-4 shadow-card-sm
      "
      data-testid="metric-card"
      data-empty={isEmpty ? 'true' : 'false'}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wider text-fg-muted">
          {label}
        </span>
        {icon && <span className={['shrink-0', iconColor(tone)].join(' ')}>{icon}</span>}
      </div>
      <div
        className={[
          'text-2xl font-semibold tabular-nums leading-tight',
          isEmpty ? 'text-fg-muted' : valueColor(tone),
        ].join(' ')}
        data-testid="metric-card-value"
      >
        {valueText}
      </div>
      {description && (
        <p className="text-xs text-fg-secondary">{description}</p>
      )}
    </div>
  )
}

function iconColor(tone: MetricCardProps['tone']): string {
  switch (tone) {
    case 'success':
      return 'text-success'
    case 'warning':
      return 'text-warning'
    case 'danger':
      return 'text-danger'
    case 'info':
      return 'text-info'
    case 'ai':
      return 'text-ai'
    case 'neutral':
      return 'text-fg-secondary'
    default:
      return 'text-fg-secondary'
  }
}

function valueColor(tone: MetricCardProps['tone']): string {
  switch (tone) {
    case 'success':
      return 'text-success'
    case 'warning':
      return 'text-warning'
    case 'danger':
      return 'text-danger'
    case 'info':
      return 'text-info'
    case 'ai':
      return 'text-ai'
    case 'neutral':
      return 'text-fg-primary'
    default:
      return 'text-fg-primary'
  }
}
