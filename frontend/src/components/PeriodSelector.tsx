// Phase 7A — Calendar-month billing selector.
//
// Replaces the Phase 6B 7d/30d/60d/90d segmented control with a
// native `<select>` of the last 12 calendar months plus a hidden
// back-compat hook for tests that drive the rolling-lookback path.

import { useFinopsPeriod } from '../lib/finops/period'
import { monthLabel } from '../types/finops'

export function PeriodSelector({
  disabled = false,
  testId = 'period-selector',
}: {
  disabled?: boolean
  testId?: string
}) {
  const { period, setMonth, monthList } = useFinopsPeriod()
  if (period.kind !== 'month') {
    // Back-compat path: when the provider was given a days-based
    // period (legacy or test fixture), expose no selector — the
    // caller decides how to render it.
    return null
  }
  return (
    <div
      data-testid={testId}
      className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1"
    >
      <label
        htmlFor={`${testId}-select`}
        className="text-xs font-medium text-fg-secondary"
      >
        Month
      </label>
      <select
        id={`${testId}-select`}
        disabled={disabled}
        value={period.month}
        onChange={(e) => setMonth(e.target.value)}
        className="rounded border border-border bg-surface px-2 py-1 text-xs text-fg-primary focus:outline-none focus-visible:shadow-focus"
        data-testid={`${testId}-input`}
      >
        {monthList.map((m) => (
          <option key={m} value={m}>
            {monthLabel(m)}
          </option>
        ))}
      </select>
    </div>
  )
}
