// Phase 6A — AccessDenied.
//
// Standalone full-page Access Denied screen (not the inline route
// fallback).  Used when a page component chooses to render
// "denied" itself, e.g. after a 403 from a data fetch.

import { Link } from '../lib/router'
import type { Role } from '../lib/tokens'
import { roleDisplayLabel } from '../lib/tokens'

export function AccessDenied({
  requiredRoles,
  message,
}: {
  requiredRoles?: Role[]
  message?: string
}) {
  return (
    <main className="flex min-h-[50vh] flex-col items-center justify-center px-6 text-center">
      <div
        className="
          flex h-12 w-12 items-center justify-center rounded-full
          border border-warning/30 bg-warning-soft text-warning
        "
        aria-hidden
      >
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
          <path d="M7 11V7a5 5 0 0 1 10 0v4" />
        </svg>
      </div>
      <h1 className="mt-4 text-2xl font-semibold text-fg-primary">Access denied</h1>
      <p className="mt-2 max-w-md text-sm text-fg-secondary">
        {message ?? 'Your role does not have permission to view this page.'}
      </p>
      {requiredRoles && requiredRoles.length > 0 && (
        <p className="mt-1 text-xs text-fg-muted">
          Required role{requiredRoles.length > 1 ? 's' : ''}:{' '}
          {requiredRoles.map(roleDisplayLabel).join(', ')}
        </p>
      )}
      <Link
        to="/"
        className="
          mt-5 inline-flex items-center rounded-md border border-border
          bg-surface px-3 py-1.5 text-sm font-medium text-fg-primary
          hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
        "
      >
        Return to dashboard
      </Link>
    </main>
  )
}
