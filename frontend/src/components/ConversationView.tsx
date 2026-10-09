// Phase 6C — Conversation view (right pane).
//
// Glues together: connection state, message stream, evidence, composer.

import { useEffect, useMemo, useRef } from 'react'
import type {
  ConnectionState,
  Conversation,
  ConversationMessage,
  ServerAssistantFrame,
} from '../types/ai'
import { isInsufficientEvidence } from '../types/ai'
import type { LookbackDays } from '../types/finops'
import { Composer } from './Composer'
import { EmptyState } from './EmptyState'
import { EvidenceCard } from './EvidenceCard'
import { LoadingSkeletonRows } from './LoadingSkeleton'
import { MessageBubble } from './MessageBubble'
import { ProgressIndicator } from './ProgressIndicator'
import { ErrorState } from './ErrorState'

export interface ConversationViewProps {
  conversation: Conversation | null
  messages: ConversationMessage[]
  loadingMessages: boolean
  historyError: string | null
  connectionState: ConnectionState
  closeCode: number | null
  inflight: boolean
  lastAssistant: ServerAssistantFrame | null
  /** Optional last error frame to render as a banner. */
  lastErrorMessage: string | null
  /** When the parent has determined the WS is unavailable but we still
   *  need to render the page (e.g. auth disabled). */
  unavailable?: 'auth_disabled' | null
  /** Current lookback (forwarded to the composer / sent on each question). */
  days: LookbackDays
  onSubmit: (question: string) => void
  onRetryConnection: () => void
}

export function ConversationView(props: ConversationViewProps) {
  const {
    conversation,
    messages,
    loadingMessages,
    historyError,
    connectionState,
    closeCode,
    inflight,
    lastAssistant,
    lastErrorMessage,
    unavailable,
    days,
    onSubmit,
    onRetryConnection,
  } = props

  const isUnavailable: boolean = unavailable === 'auth_disabled'

  const streamRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const el = streamRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages.length, lastAssistant?.message_id])

  const statusLabel = useMemo(() => {
    if (isUnavailable) return 'Auth disabled'
    switch (connectionState) {
      case 'idle':
        return 'Ready'
      case 'connecting':
        return 'Connecting…'
      case 'connected':
        return 'Connected'
      case 'processing':
        return 'Analyzing AWS data'
      case 'completed':
        return 'Complete'
      case 'disconnected':
        return 'Disconnected'
      case 'authorization_failure':
        return 'Authorization failed'
      case 'retryable_failure':
        return `Connection lost (${closeCode ?? '?'})`
      case 'protocol_violation':
        return 'Protocol error'
    }
  }, [connectionState, closeCode, isUnavailable])

  if (isUnavailable) {
    return (
      <div
        className="
          flex h-full flex-col items-center justify-center p-6 text-center
        "
      >
        <EmptyState
          title="Persistent conversations unavailable"
          description="Enable authentication (AUTH_ENABLED=true) to use persistent AI conversations."
        />
      </div>
    )
  }

  const showEvidence =
    !!lastAssistant && Array.isArray(lastAssistant.citations) && lastAssistant.citations.length > 0
  const showAssistantWarnings =
    !!lastAssistant &&
    Array.isArray(lastAssistant.warnings) &&
    lastAssistant.warnings.length > 0
  const insufficient = isInsufficientEvidence(lastAssistant?.grounding, lastAssistant?.warnings)

  return (
    <section
      className="flex h-full min-h-0 flex-col"
      data-testid="conversation-view"
    >
      <header
        className="
          flex items-center justify-between gap-3 border-b border-border
          bg-bg-elevated px-4 py-2
        "
      >
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-fg-primary">
            {conversation?.title ?? 'AI Cost Analyst'}
          </h2>
          <p className="text-xs text-fg-muted">Context: Last {days} days</p>
        </div>
        <div className="flex items-center gap-2">
          <ProgressIndicator state={connectionState} />
          {connectionState === 'retryable_failure' ||
          connectionState === 'disconnected' ? (
            <button
              type="button"
              onClick={onRetryConnection}
              data-testid="reconnect-button"
              className="
                inline-flex items-center rounded-md border border-border
                bg-surface px-2 py-1 text-xs text-fg-primary
                hover:bg-surface-hover focus:outline-none focus-visible:shadow-focus
              "
            >
              Reconnect
            </button>
          ) : null}
        </div>
      </header>

      <div
        ref={streamRef}
        className="
          min-h-0 flex-1 overflow-y-auto bg-bg p-4
        "
        aria-live="polite"
        aria-busy={loadingMessages ? 'true' : 'false'}
        data-testid="message-stream"
      >
        {loadingMessages && messages.length === 0 ? (
          <LoadingSkeletonRows rows={4} />
        ) : null}

        {historyError ? (
          <ErrorState
            title="Could not load conversation history"
            message={historyError}
          />
        ) : null}

        {!loadingMessages && !historyError && messages.length === 0 ? (
          <EmptyState
            title="Ask about your AWS environment"
            description="Use the composer below to start a grounded analysis."
          />
        ) : null}

        <div className="flex flex-col gap-2">
          {messages.map((m) => (
            <MessageBubble key={m.id} message={m} />
          ))}
        </div>

        {insufficient ? (
          <div
            role="status"
            className="
              mt-3 rounded-md border border-warning/30 bg-warning-soft/40 p-3
              text-xs text-warning
            "
            data-testid="insufficient-evidence"
          >
            The AI response did not have enough fresh Phase 1–3 evidence
            to produce a confident answer. Use the citations panel below to
            verify any claims.
          </div>
        ) : null}

        {showAssistantWarnings && lastAssistant ? (
          <div
            role="status"
            className="
              mt-3 rounded-md border border-warning/30 bg-warning-soft/40 p-3
              text-xs text-warning
            "
            data-testid="assistant-warnings"
          >
            <p className="font-medium">Provider warnings</p>
            <ul className="mt-1 space-y-0.5">
              {lastAssistant.warnings.map((w, idx) => (
                <li key={idx}>· {w}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {showEvidence && lastAssistant ? (
          <section
            aria-label="Citations"
            className="mt-3 space-y-2"
            data-testid="evidence-panel"
          >
            <h3 className="text-xs font-semibold uppercase tracking-wider text-fg-muted">
              Citations ({lastAssistant.citations.length})
            </h3>
            {lastAssistant.citations.map((c, idx) => (
              <EvidenceCard key={idx} citation={c} index={idx} />
            ))}
          </section>
        ) : null}

        {lastErrorMessage && !inflight ? (
          <div
            role="alert"
            className="
              mt-3 rounded-md border border-danger/30 bg-danger-soft/40 p-3
              text-xs text-danger
            "
            data-testid="last-error"
          >
            {lastErrorMessage}
          </div>
        ) : null}
      </div>

      <footer className="border-t border-border bg-bg-elevated p-3">
        <Composer
          onSubmit={onSubmit}
          disabled={isUnavailable}
          inflight={inflight}
          statusLabel={statusLabel}
        />
      </footer>
    </section>
  )
}
