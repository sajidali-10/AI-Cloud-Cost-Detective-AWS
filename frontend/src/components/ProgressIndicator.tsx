// Phase 6C — Compact connection-state indicator.
//
// Maps the `ConnectionState` enum from `useConversationSocket` to:
//   * a colour tone
//   * a stable user-facing label
//   * an aria-live announcer so screen readers are notified of
//     state transitions (without being spammed on every frame).

import { useEffect, useRef, useState } from 'react'
import type { ConnectionState } from '../types/ai'
import { StatusBadge, type StatusTone } from './StatusBadge'

interface ProgressIndicatorProps {
  state: ConnectionState
  /** Optional human hint shown to the right of the label. */
  hint?: string
}

const STATE_LABELS: Record<ConnectionState, string> = {
  idle: 'Not connected',
  connecting: 'Connecting…',
  connected: 'Connected',
  processing: 'Analyzing AWS data',
  completed: 'Complete',
  disconnected: 'Disconnected',
  authorization_failure: 'Authorization failed',
  retryable_failure: 'Connection lost',
  protocol_violation: 'Protocol error',
}

const STATE_TONES: Record<ConnectionState, StatusTone> = {
  idle: 'neutral',
  connecting: 'info',
  connected: 'success',
  processing: 'ai',
  completed: 'success',
  disconnected: 'warning',
  authorization_failure: 'danger',
  retryable_failure: 'warning',
  protocol_violation: 'danger',
}

export function ProgressIndicator({ state, hint }: ProgressIndicatorProps) {
  const label = STATE_LABELS[state]
  const tone = STATE_TONES[state]
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="inline-flex items-center gap-2"
    >
      <StatusBadge tone={tone} title={label}>
        {label}
      </StatusBadge>
      {hint && <span className="text-xs text-fg-muted">{hint}</span>}
    </div>
  )
}

/**
 * Speak state transitions via an aria-live polite announcer, but
 * suppress repeats so the screen reader is not notified on every
 * render of the same state.
 */
export function useConnectionAnnouncer(state: ConnectionState): string {
  const [announced, setAnnounced] = useState<string>('')
  const last = useRef<string | null>(null)
  useEffect(() => {
    const label = STATE_LABELS[state]
    if (last.current !== label) {
      last.current = label
      setAnnounced(label)
    }
  }, [state])
  return announced
}
