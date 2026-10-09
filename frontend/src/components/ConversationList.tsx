// Phase 6C — Conversation list (left rail).
//
// Renders the user's conversations as a vertically compact, dense
// list.  Selection is keyboard-accessible.  Empty + loading + error
// states are distinguished explicitly (per the Phase 6C spec).

import { useMemo } from 'react'
import type { Conversation } from '../types/ai'
import { EmptyState } from './EmptyState'
import { LoadingSkeleton } from './LoadingSkeleton'

export interface ConversationListProps {
  conversations: Conversation[]
  loading: boolean
  error: string | null
  activeId: number | null
  onSelect: (id: number) => void
  onCreate: () => void
  busyCreate?: boolean
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function ConversationList({
  conversations,
  loading,
  error,
  activeId,
  onSelect,
  onCreate,
  busyCreate,
}: ConversationListProps) {
  const sorted = useMemo(
    () =>
      [...conversations].sort((a, b) => {
        const aT = a.last_message_at ?? a.updated_at
        const bT = b.last_message_at ?? b.updated_at
        return new Date(bT).getTime() - new Date(aT).getTime()
      }),
    [conversations],
  )

  return (
    <aside
      aria-label="Conversations"
      data-testid="conversation-list"
      className="
        flex h-full flex-col gap-2 border-r border-border bg-bg-elevated
        p-3
      "
    >
      <button
        type="button"
        onClick={onCreate}
        disabled={busyCreate}
        aria-busy={busyCreate ? 'true' : 'false'}
        data-testid="new-chat"
        className="
          inline-flex w-full items-center justify-center rounded-md
          border border-primary/40 bg-primary px-3 py-2 text-sm
          font-medium text-primary-foreground hover:bg-primary-hover
          focus:outline-none focus-visible:shadow-focus
          disabled:cursor-not-allowed disabled:opacity-60
        "
      >
        {busyCreate ? 'Creating…' : 'New chat'}
      </button>

      <div className="min-h-0 flex-1 overflow-y-auto" role="list">
        {loading && conversations.length === 0 ? (
          <div className="space-y-2" aria-busy="true">
            <LoadingSkeleton className="h-12 w-full" />
            <LoadingSkeleton className="h-12 w-full" />
            <LoadingSkeleton className="h-12 w-full" />
          </div>
        ) : null}

        {!loading && error ? (
          <div
            role="alert"
            className="
              rounded-md border border-danger/30 bg-danger-soft/40 p-3 text-xs
              text-danger
            "
          >
            Could not load conversations: {error}
          </div>
        ) : null}

        {!loading && !error && conversations.length === 0 ? (
          <EmptyState
            title="No conversations yet"
            description="Press “New chat” to start a grounded analysis."
          />
        ) : null}

        {!loading && !error && sorted.length > 0 ? (
          <ul className="space-y-1">
            {sorted.map((c) => {
              const active = c.id === activeId
              return (
                <li key={c.id} role="listitem">
                  <button
                    type="button"
                    onClick={() => onSelect(c.id)}
                    aria-current={active ? 'true' : undefined}
                    data-testid={`conversation-row-${c.id}`}
                    className={[
                      'flex w-full flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left text-sm transition-colors',
                      'focus:outline-none focus-visible:shadow-focus',
                      active
                        ? 'border-primary/40 bg-primary-soft text-fg-primary'
                        : 'border-border bg-surface text-fg-primary hover:bg-surface-hover',
                    ].join(' ')}
                  >
                    <span className="line-clamp-2 font-medium">{c.title}</span>
                    <span className="text-[0.7rem] text-fg-muted">
                      {c.is_archived ? 'Archived · ' : ''}
                      {formatDate(c.last_message_at ?? c.updated_at)}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        ) : null}
      </div>
    </aside>
  )
}
