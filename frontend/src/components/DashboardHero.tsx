// Phase 6C — Dashboard hero card.
//
// Centred, theme-aware brand hero modelled after the HipLink AI
// Assistant visual language.  Reuses the centralized <HiplinkLogo
// size="hero" /> so logo selection is not duplicated.
//
// Layout (no live data means subtle skeleton placeholders, never
// fabricated numbers):
//
//   ┌──────────────────────────────────────────────┐
//   │                                              │
//   │             [Hiplink logo]                   │
//   │             AI Cloud Cost Detective          │
//   │   AWS cost visibility, optimization and      │
//   │   AI-powered FinOps analysis                 │
//   │                                              │
//   │   Account · Region · Refreshed · Status      │
//   │                                              │
//   └──────────────────────────────────────────────┘
//
// Hero height is deliberately compact (~ py-8) so the existing
// Phase 6B KPI tiles + charts remain the primary focus below.

import { HiplinkLogo } from './HiplinkLogo'
import { StatusBadge } from './StatusBadge'

export interface DashboardHeroProps {
  /** Optional account id (already masked by the caller). */
  accountLabel?: string | null
  /** Optional region label. */
  regionLabel?: string | null
  /** Optional "last refreshed" human-readable string. */
  refreshedLabel?: string | null
  /** Optional data status — used to colour the pill. */
  dataStatus?: 'live' | 'loading' | 'refreshing' | 'unavailable' | 'idle'
}

const STATUS_LABEL: Record<NonNullable<DashboardHeroProps['dataStatus']>, string> = {
  live: 'Live',
  loading: 'Loading',
  refreshing: 'Refreshing',
  unavailable: 'Unavailable',
  idle: 'Idle',
}

const STATUS_TONE: Record<NonNullable<DashboardHeroProps['dataStatus']>, 'success' | 'info' | 'warning' | 'danger' | 'neutral'> = {
  live: 'success',
  loading: 'info',
  refreshing: 'info',
  unavailable: 'danger',
  idle: 'neutral',
}

export function DashboardHero(props: DashboardHeroProps) {
  const status = props.dataStatus ?? 'idle'
  return (
    <section
      data-testid="dashboard-hero"
      className="
        rounded-xl border border-border bg-surface
        px-6 py-8 text-center shadow-card-sm
      "
    >
      <div className="flex flex-col items-center gap-3">
        <HiplinkLogo size="hero" testId="dashboard-hero-logo" />
        <h1
          className="text-lg font-semibold tracking-wide text-fg-primary sm:text-xl"
          data-testid="dashboard-hero-title"
        >
          AI Cloud Cost Detective
        </h1>
        <p
          className="max-w-2xl text-sm text-fg-secondary sm:text-base"
          data-testid="dashboard-hero-tagline"
        >
          AWS cost visibility, optimization and AI-powered FinOps analysis.
        </p>
      </div>

      <dl
        className="
          mt-6 flex flex-wrap items-center justify-center gap-x-6 gap-y-2
          text-xs text-fg-muted
        "
        data-testid="dashboard-hero-meta"
      >
        {props.accountLabel ? (
          <div className="flex items-center gap-1">
            <dt className="font-medium text-fg-secondary">Account</dt>
            <dd className="font-mono text-fg-primary">{props.accountLabel}</dd>
          </div>
        ) : null}
        {props.regionLabel ? (
          <div className="flex items-center gap-1">
            <dt className="font-medium text-fg-secondary">Region</dt>
            <dd className="text-fg-primary">{props.regionLabel}</dd>
          </div>
        ) : null}
        {props.refreshedLabel ? (
          <div className="flex items-center gap-1">
            <dt className="font-medium text-fg-secondary">Refreshed</dt>
            <dd className="text-fg-primary">{props.refreshedLabel}</dd>
          </div>
        ) : null}
        <div className="flex items-center gap-1">
          <dt className="font-medium text-fg-secondary">Status</dt>
          <dd data-testid="dashboard-hero-status">
            <StatusBadge tone={STATUS_TONE[status]}>
              {STATUS_LABEL[status]}
            </StatusBadge>
          </dd>
        </div>
      </dl>
    </section>
  )
}
