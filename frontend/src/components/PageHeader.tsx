// Phase 6A — PageHeader.
//
// Compact enterprise page title block patterned after the HipLink
// reference.  Hierarchy:
//
//   PRODUCT TITLE   (page-specific; e.g. "AI Cloud Cost Detective")
//   PAGE TITLE      (h1; e.g. "Dashboard")
//   SUBTITLE        (h2; small descriptive line)
//   CONTEXT ROW     (account • region • status • last updated)
//
// Optional `actions` slot on the right for page-level buttons.

import type { ReactNode } from 'react'

export interface PageHeaderProps {
  /** Tiny eyebrow text above the title — e.g. "AI Cloud Cost Detective". */
  eyebrow?: string
  /** Primary page title. */
  title: string
  /** Small descriptive subtitle below the title. */
  subtitle?: string
  /** Context row (e.g. "Account • Region • Status • Last Updated"). */
  context?: ReactNode
  /** Right-aligned action area. */
  actions?: ReactNode
}

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  context,
  actions,
}: PageHeaderProps) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow && (
          <p className="text-xs font-medium uppercase tracking-wider text-fg-muted">
            {eyebrow}
          </p>
        )}
        <h1 className="mt-1 truncate text-xl font-semibold text-fg-primary">
          {title}
        </h1>
        {subtitle && (
          <p className="mt-1 text-sm text-fg-secondary">{subtitle}</p>
        )}
        {context && (
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
            {context}
          </div>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </div>
  )
}

/** Inline pill used inside PageHeader context rows. */
export function ContextItem({
  label,
  value,
}: {
  label: string
  value: ReactNode
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-fg-muted">{label}</span>
      <span className="text-fg-secondary">{value}</span>
    </span>
  )
}
