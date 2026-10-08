// Phase 6B — Inline PARTIAL_SUCCESS warning.
//
// Compact banner shown when a backend endpoint returns
// status=PARTIAL_SUCCESS or surfaces warnings.  Distinct from
// ErrorState — partial data is still rendered below the banner.

import type { OptimizationNotice, UtilizationWarning } from '../types/finops'

type AnyWarning = OptimizationNotice | UtilizationWarning

export function PartialWarning({
  warnings,
  title = 'Partial data',
}: {
  warnings: AnyWarning[]
  title?: string
}) {
  if (!warnings || warnings.length === 0) return null
  return (
    <div
      role="status"
      className="flex items-start gap-2 rounded-md border border-border bg-surface px-3 py-2 text-xs text-warning"
      data-testid="partial-warning"
    >
      <svg
        aria-hidden
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="mt-0.5 shrink-0"
      >
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        <ul className="mt-0.5 space-y-0.5 text-warning/90">
          {warnings.slice(0, 3).map((w, i) => (
            <li key={i} className="truncate" title={w.message}>
              <span className="font-medium">{w.source}</span>
              {w.code ? <span className="ml-1 opacity-80">({w.code})</span> : null}
              {w.message ? <span className="ml-1 opacity-80">— {w.message}</span> : null}
            </li>
          ))}
          {warnings.length > 3 && (
            <li className="opacity-80">+ {warnings.length - 3} more</li>
          )}
        </ul>
      </div>
    </div>
  )
}
