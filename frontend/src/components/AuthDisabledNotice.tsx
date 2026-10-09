// Phase 6C — Explanatory auth-disabled notice.
//
// The Phase 5B contract returns 503 `AuthDisabled` for every
// `/api/conversations/*` route when `AUTH_ENABLED=false`.  Rather
// than silently empty the UI, we render an honest dev-mode state.

import { SectionCard } from './SectionCard'

export function AuthDisabledNotice() {
  return (
    <SectionCard title="Persistent conversations unavailable" padded>
      <div
        role="alert"
        className="flex flex-col gap-2 text-sm text-fg-secondary"
      >
        <p>
          Authentication must be enabled to use persistent AI conversations.
        </p>
        <p className="text-xs text-fg-muted">
          Set <code className="rounded bg-surface-2 px-1 py-0.5 font-mono">AUTH_ENABLED=true</code>{' '}
          and authenticate the request. Until then, only the anonymous
          <code className="rounded bg-surface-2 px-1 py-0.5 font-mono"> /api/ai/* </code>
          endpoints are available.
        </p>
      </div>
    </SectionCard>
  )
}
