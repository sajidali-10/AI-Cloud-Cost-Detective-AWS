// Phase 6A — SectionCard.
//
// Generic themed container for grouped content.  Restrained border,
// subtle elevation, optional title + actions.

import type { ReactNode } from 'react'

export function SectionCard({
  title,
  description,
  actions,
  children,
  className = '',
  padded = true,
}: {
  title?: string
  description?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  padded?: boolean
}) {
  return (
    <section
      className={[
        'rounded-lg border border-border bg-surface shadow-card-sm',
        className,
      ].join(' ')}
    >
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {title && (
              <h2 className="truncate text-sm font-semibold text-fg-primary">
                {title}
              </h2>
            )}
            {description && (
              <p className="mt-0.5 text-xs text-fg-muted">{description}</p>
            )}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={padded ? 'p-4' : ''}>{children}</div>
    </section>
  )
}
