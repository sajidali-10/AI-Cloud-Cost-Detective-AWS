// Phase 6A — ErrorState.
//
// Centralised error UI.  Renders a sanitized message — the caller
// passes the message (already safe to display).  Never shows
// stack traces, never renders raw exception objects.

import type { ReactNode } from 'react'

export function ErrorState({
  title = 'Something went wrong',
  message,
  onRetry,
}: {
  title?: string
  message?: string
  onRetry?: () => void
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-3 rounded-lg border border-danger/30 bg-danger-soft/40 px-6 py-10 text-center"
    >
      <div className="text-sm font-semibold text-danger">{title}</div>
      {message && (
        <p className="max-w-md text-xs text-fg-secondary">{message}</p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="
            mt-2 inline-flex items-center rounded-md border border-border
            bg-surface px-3 py-1.5 text-xs font-medium text-fg-primary
            hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
          "
        >
          Try again
        </button>
      )}
    </div>
  )
}

export function BackendUnavailable({ onRetry }: { onRetry?: () => void }) {
  return (
    <ErrorState
      title="Backend unavailable"
      message="We could not reach the backend service. Please try again in a moment."
      onRetry={onRetry}
    />
  )
}

export function ForbiddenState(): ReactNode {
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-warning/30 bg-warning-soft/40 px-6 py-10 text-center"
    >
      <div className="text-sm font-semibold text-warning">Forbidden</div>
      <p className="max-w-md text-xs text-fg-secondary">
        Your account does not have permission to perform this action.
      </p>
    </div>
  )
}
