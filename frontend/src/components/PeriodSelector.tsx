// Phase 7A — Calendar-month billing selector.
//
// Replaces the Phase 6B 7d/30d/60d/90d segmented control with a
// native `<select>` of the last 12 calendar months plus a hidden
// back-compat hook for tests that drive the rolling-lookback path.
//
// Phase 9D.1 — provider-aware: RACKSPACE Billing answers only for
// the CURRENT billing cycle (the canonical Rackspace cost route
// ignores arbitrary historical months), so the month selector is
// NOT rendered for RACKSPACE.  A static, non-interactive control
// communicates the fixed scope — the historical Month selector
// never appears under the Rackspace provider.

import { useFinopsPeriod } from '../lib/finops/period'
import { useProviderContext } from '../lib/providerContext'
import { monthLabel } from '../types/finops'

export function PeriodSelector({
  disabled = false,
  testId = 'period-selector',
}: {
  disabled?: boolean
  testId?: string
}) {
  const { period, setMonth, monthList } = useFinopsPeriod()
  const { provider } = useProviderContext()
  if (provider === 'RACKSPACE') {
    // Phase 9D.1 — Rackspace exposes no arbitrary historical month
    // selection.  Render the fixed current-cycle scope as a static
    // control; there is no `<select>` to interact with.
    return (
      <div
        data-testid={testId}
        data-provider="RACKSPACE"
        className="inline-flex items-center gap-2 rounded-md border border-border bg-surface px-2 py-1"
        title="Rackspace Billing reports the current billing cycle only"
      >
        <span className="text-xs font-medium text-fg-secondary">Billing cycle</span>
        <span
          className="rounded border border-border bg-surface px-2 py-1 text-xs text-fg-secondary"
          data-testid={`${testId}-fixed-cycle`}
        >
          Current Billing Cycle
        </span>
      </div>
    )
  }
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
