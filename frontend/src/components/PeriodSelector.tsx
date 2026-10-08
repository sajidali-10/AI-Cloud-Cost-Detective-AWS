// Phase 6B — Compact HipLink-style period selector.
//
// 7d · 30d · 60d · 90d segmented control.  Reads / writes the
// FinopsPeriod context so a selection on the Costs page flows to
// the Dashboard without manual prop-drilling.

import { useFinopsPeriod } from '../lib/finops/period'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../types/finops'
import { periodShortLabel } from '../lib/format'

export function PeriodSelector({
  disabled = false,
  testId = 'period-selector',
}: {
  disabled?: boolean
  testId?: string
}) {
  const { days, setDays } = useFinopsPeriod()
  return (
    <div
      role="radiogroup"
      aria-label="Lookback period"
      data-testid={testId}
      className="inline-flex items-center rounded-md border border-border bg-surface p-0.5"
    >
      {ALLOWED_LOOKBACK_DAYS.map((opt) => (
        <PeriodOption
          key={opt}
          value={opt}
          active={opt === days}
          disabled={disabled}
          onSelect={setDays}
        />
      ))}
    </div>
  )
}

function PeriodOption({
  value,
  active,
  disabled,
  onSelect,
}: {
  value: LookbackDays
  active: boolean
  disabled: boolean
  onSelect: (v: LookbackDays) => void
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      disabled={disabled}
      onClick={() => onSelect(value)}
      className={[
        'rounded px-2 py-1 text-xs transition-colors',
        'focus:outline-none focus-visible:shadow-focus',
        active
          ? 'bg-primary-soft text-primary ring-1 ring-primary/30'
          : 'text-fg-secondary hover:bg-surface-hover hover:text-fg-primary',
        disabled ? 'cursor-not-allowed opacity-60' : '',
      ].join(' ')}
      data-active={active ? 'true' : 'false'}
    >
      {periodShortLabel(value)}
    </button>
  )
}
