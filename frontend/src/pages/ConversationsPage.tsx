// Phase 6C — Conversations history page.
//
// Operational view: list every conversation owned by the
// authenticated user.  Empty / loading / failure states are
// explicitly distinct (per the Phase 6C spec — no generic "no data"
// fallback).  Clicking a row navigates to `/analyst?conversation={id}`.

import { useCallback, useMemo } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { DataTable } from '../components/DataTable'
import { EmptyState } from '../components/EmptyState'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { RefreshButton } from '../components/RefreshButton'
import { StatusBadge } from '../components/StatusBadge'
import { AiProviderBadge } from '../components/AiProviderBadge'
import { useFinopsQuery } from '../lib/finops/store'
import { useAuth } from '../lib/auth'
import { ApiError } from '../lib/api'
import { fetchAIStatus, fetchConversations, aiStatusKey } from '../lib/ai/api'
import { useNavigate } from '../lib/router'
import { ERR_AUTH_DISABLED } from '../types/ai'

function formatTimestamp(iso: string | null | undefined): string {
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

export function ConversationsPage() {
  const navigate = useNavigate()
  const { authEnabled } = useAuth()

  const statusQ = useFinopsQuery(aiStatusKey(), () => fetchAIStatus(), { ttlMs: 60_000 })

  const listQ = useFinopsQuery(
    'conversations-history',
    async () => {
      const r = await fetchConversations({ limit: 200 })
      return r.conversations
    },
    { ttlMs: 30_000 },
  )

  const error = listQ.entry.error
  const isAuthDisabled =
    (error instanceof ApiError && error.errorCode === ERR_AUTH_DISABLED) ||
    (error instanceof ApiError && error.status === 503) ||
    authEnabled === false

  const loading = listQ.entry.status === 'loading' || listQ.entry.status === 'refreshing'
  const conversations = listQ.entry.data ?? []

  const sorted = useMemo(
    () =>
      [...conversations].sort((a, b) => {
        const aT = a.last_message_at ?? a.updated_at
        const bT = b.last_message_at ?? b.updated_at
        return new Date(bT).getTime() - new Date(aT).getTime()
      }),
    [conversations],
  )

  const onOpen = useCallback(
    (id: number) => {
      navigate(`/analyst?conversation=${id}`)
    },
    [navigate],
  )

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Conversations"
        subtitle="Operational history of your grounded AI analyses"
        actions={
          <div className="flex items-center gap-2">
            <AiProviderBadge status={statusQ.entry.data ?? null} />
            <RefreshButton onClick={() => listQ.refresh()} />
          </div>
        }
      />

      {isAuthDisabled ? (
        <SectionCard title="Persistent conversations unavailable" padded>
          <div role="alert" className="text-sm text-fg-secondary">
            Authentication must be enabled to use persistent AI conversations.
            Enable <code className="rounded bg-surface-2 px-1 py-0.5 font-mono">AUTH_ENABLED=true</code>{' '}
            and authenticate the request, or use the anonymous{' '}
            <code className="rounded bg-surface-2 px-1 py-0.5 font-mono">/api/ai/*</code>{' '}
            endpoints for ad-hoc analysis.
          </div>
        </SectionCard>
      ) : loading && conversations.length === 0 ? (
        <SectionCard title="Saved conversations">
          <LoadingSkeletonRows rows={6} />
        </SectionCard>
      ) : error && !isAuthDisabled ? (
        <SectionCard title="Saved conversations">
          <ErrorState
            title="Could not load conversations"
            message={error instanceof Error ? error.message : 'Unknown error'}
            onRetry={() => listQ.refresh()}
          />
        </SectionCard>
      ) : (
        <SectionCard title="Saved conversations">
          <DataTable
            columns={[
              {
                key: 'title',
                header: 'Title',
                cell: (r) => <span className="font-medium">{r.title || 'New Cost Analysis'}</span>,
              },
              {
                key: 'updated',
                header: 'Last updated',
                cell: (r) => formatTimestamp(r.last_message_at ?? r.updated_at),
              },
              {
                key: 'status',
                header: 'Status',
                cell: (r) =>
                  r.is_archived ? (
                    <StatusBadge tone="warning">Archived</StatusBadge>
                  ) : (
                    <StatusBadge tone="success">Active</StatusBadge>
                  ),
              },
              {
                key: 'open',
                header: '',
                cell: (r) => (
                  <button
                    type="button"
                    onClick={() => onOpen(r.id)}
                    data-testid={`open-conversation-${r.id}`}
                    className="
                      rounded-md border border-primary/30 bg-primary-soft
                      px-2 py-1 text-xs text-primary
                      hover:bg-primary/20 focus:outline-none focus-visible:shadow-focus
                    "
                  >
                    Open
                  </button>
                ),
              },
            ]}
            rows={sorted}
            rowKey={(r) => r.id}
            emptyState={
              <EmptyState
                title="No conversations yet"
                description="Press “New chat” on the AI Cost Analyst page to start a grounded analysis."
              />
            }
          />
        </SectionCard>
      )}
    </div>
  )
}
