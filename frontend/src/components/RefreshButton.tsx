// Phase 6B — Global refresh action.
//
// Invalidates the in-memory query store and re-fetches every active
// query.  Restrained by the backend cache (Cost Explorer responses
// are cached on the server) so this does not generate extra AWS
// API calls.

import { invalidateCache } from '../lib/finops/store'

export function RefreshButton({
  onClick,
  label = 'Refresh',
  testId = 'refresh-button',
}: {
  onClick?: () => void
  label?: string
  testId?: string
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={() => {
        invalidateCache()
        onClick?.()
      }}
      className="
        inline-flex h-8 items-center gap-1.5 rounded-md border border-border
        bg-surface px-2.5 text-xs font-medium text-fg-primary
        hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
      "
      aria-label={label}
    >
      <svg
        aria-hidden
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M3 12a9 9 0 0 1 15.93-5.39L21 8" />
        <path d="M21 3v5h-5" />
        <path d="M21 12a9 9 0 0 1-15.93 5.39L3 16" />
        <path d="M3 21v-5h5" />
      </svg>
      {label}
    </button>
  )
}
