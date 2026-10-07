// Phase 6A — EmptyState.
//
// Centralised "nothing here yet" message.  Uses restrained copy
// (e.g. "No data loaded", "Available in Phase 6B") and never
// shows fabricated placeholder values.

import type { ReactNode } from 'react'

export function EmptyState({
  title,
  description,
  icon,
  action,
}: {
  title: string
  description?: string
  icon?: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      role="status"
      className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center"
    >
      {icon && <div className="text-fg-muted">{icon}</div>}
      <p className="text-sm font-medium text-fg-secondary">{title}</p>
      {description && (
        <p className="max-w-md text-xs text-fg-muted">{description}</p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
