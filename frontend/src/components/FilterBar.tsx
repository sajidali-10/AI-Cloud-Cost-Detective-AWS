// Phase 6A — FilterBar.
//
// Horizontal row of filter controls (search, selects).  Pure
// presentational — the parent owns the state.

import type { ReactNode } from 'react'

export function FilterBar({ children }: { children: ReactNode }) {
  return (
    <div
      role="toolbar"
      aria-label="Filters"
      className="flex flex-wrap items-center gap-2"
    >
      {children}
    </div>
  )
}

export interface FilterSelectProps {
  label: string
  value: string
  options: ReadonlyArray<{ value: string; label: string }>
  onChange: (value: string) => void
  disabled?: boolean
  testId?: string
}

export function FilterSelect({
  label,
  value,
  options,
  onChange,
  disabled = false,
  testId,
}: FilterSelectProps) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
      <span>{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        data-testid={testId}
        className="
          rounded-md border border-border bg-surface px-2 py-1 text-xs
          text-fg-primary focus:outline-none focus-visible:shadow-focus
          disabled:opacity-60
        "
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  )
}
