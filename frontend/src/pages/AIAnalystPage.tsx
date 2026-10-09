// Phase 6C — AI Cost Analyst workspace.
//
// HipLink-style two-pane layout:
//   ┌─────────────────────────────────────────────────────────────┐
//   │ Header: title, subtitle, period selector, provider badge   │
//   ├──────────────┬──────────────────────────────────────────────┤
//   │ Conversation │ Conversation view (header + stream +         │
//   │ list         │ citations + composer)                        │
//   │              │                                              │
//   │ New chat     │                                              │
//   └──────────────┴──────────────────────────────────────────────┘
//
// On narrow screens the conversation list collapses into a drawer
// toggled by the "Conversations" button in the page header.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../lib/auth'
import { ApiError } from '../lib/api'
import { useFinopsQuery } from '../lib/finops/store'
import { useFinopsPeriod } from '../lib/finops/period'
import {
  fetchAIStatus,
  fetchConversations,
  fetchConversation,
  fetchConversationMessages,
  createConversation,
  aiStatusKey,
  conversationsListKey,
  conversationDetailKey,
  conversationMessagesKey,
} from '../lib/ai/api'
import {
  useConversationSocket,
} from '../lib/ai/connection'
import type {
  AIStatusResponse,
  Conversation,
  ConversationMessage,
} from '../types/ai'
import { ERR_AUTH_DISABLED, MAX_QUESTION_LENGTH } from '../types/ai'
import { useLocation, useNavigate } from '../lib/router'

import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { AiProviderBadge } from '../components/AiProviderBadge'
import { AuthDisabledNotice } from '../components/AuthDisabledNotice'
import { ConversationList } from '../components/ConversationList'
import { ConversationView } from '../components/ConversationView'
import { StatusBadge } from '../components/StatusBadge'
import { PeriodSelector } from '../components/PeriodSelector'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../types/finops'

function getConversationIdFromLocation(locationSearch: string): number | null {
  if (!locationSearch) return null
  const params = new URLSearchParams(locationSearch)
  const raw = params.get('conversation')
  if (!raw) return null
  const id = Number.parseInt(raw, 10)
  if (!Number.isFinite(id) || id <= 0) return null
  return id
}

export function AIAnalystPage() {
  const { authEnabled } = useAuth()
  const { days } = useFinopsPeriod()
  const location = useLocation()
  const navigate = useNavigate()

  const safeDays: LookbackDays = ALLOWED_LOOKBACK_DAYS.includes(days) ? days : 30

  // ---- AI provider status ----
  const statusQ = useFinopsQuery<AIStatusResponse>(aiStatusKey(), () => fetchAIStatus(), {
    ttlMs: 60_000,
  })
  const aiStatus: AIStatusResponse | null = statusQ.entry.data

  // ---- conversations list ----
  const listQ = useFinopsQuery<{ conversations: Conversation[]; count: number }>(
    conversationsListKey(false),
    async () => {
      const r = await fetchConversations({ limit: 100 })
      return { conversations: r.conversations, count: r.count }
    },
    { ttlMs: 30_000 },
  )

  // ---- active conversation ----
  const [activeId, setActiveId] = useState<number | null>(() =>
    getConversationIdFromLocation(location.search),
  )

  // Sync state -> URL.
  useEffect(() => {
    const fromUrl = getConversationIdFromLocation(location.search)
    if (fromUrl !== activeId) {
      if (activeId === null) {
        navigate('/analyst', { replace: true })
      } else {
        navigate(`/analyst?conversation=${activeId}`, { replace: true })
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  // ---- detail ----
  const detailQ = useFinopsQuery<Conversation>(
    activeId === null ? 'ai:conversation:none' : conversationDetailKey(activeId),
    async () => {
      if (activeId === null) throw new Error('no active conversation')
      const r = await fetchConversation(activeId)
      return r.conversation
    },
    { ttlMs: 30_000 },
  )

  // ---- messages ----
  const messagesQ = useFinopsQuery<ConversationMessage[]>(
    activeId === null ? 'ai:conversation:none:messages' : conversationMessagesKey(activeId, 0),
    async () => {
      if (activeId === null) throw new Error('no active conversation')
      const r = await fetchConversationMessages(activeId, { limit: 200, offset: 0 })
      return r.messages
    },
    { ttlMs: 30_000 },
  )

  const conversation = detailQ.entry.data ?? null
  const messages = messagesQ.entry.data ?? []
  const conversations = listQ.entry.data?.conversations ?? []

  // ---- errors ----
  const listError = listQ.entry.error
  const detailError = detailQ.entry.error
  const messagesError = messagesQ.entry.error

  const isAuthDisabled = useCallback((err: unknown): boolean => {
    if (err instanceof ApiError) {
      if (err.status === 503 && err.errorCode === ERR_AUTH_DISABLED) return true
      if (err.errorCode === ERR_AUTH_DISABLED) return true
    }
    return false
  }, [])

  const authDisabled = useMemo(() => {
    return (
      isAuthDisabled(listError) ||
      isAuthDisabled(detailError) ||
      isAuthDisabled(messagesError) ||
      (authEnabled === false)
    )
  }, [listError, detailError, messagesError, authEnabled, isAuthDisabled])

  // ---- WebSocket ----
  const socket = useConversationSocket({
    conversationId: activeId,
    getToken: () => {
      try {
        const raw = window.localStorage.getItem('accd.session')
        if (!raw) return null
        const parsed = JSON.parse(raw) as { access_token?: string; expires_at?: number }
        if (typeof parsed.access_token !== 'string') return null
        if (typeof parsed.expires_at !== 'number') return null
        if (parsed.expires_at <= Date.now()) return null
        return parsed.access_token
      } catch {
        return null
      }
    },
    authEnabled,
  })

  const handleAssistant = useCallback(() => {
    // Refresh message list so the persisted assistant message shows up.
    if (activeId !== null) {
      // Invalidate only the relevant cache key.
      // We do a coarse refresh by calling refresh on the messages query.
      messagesQ.refresh()
      listQ.refresh()
    }
  }, [activeId, listQ, messagesQ])

  const socketWithCb = useMemo(
    () => ({
      ...socket,
      onAssistant: handleAssistant,
    }),
    [socket, handleAssistant],
  )

  // Manual reconnect wiring.  We re-create the socket through the hook's
  // reconnect method, which already closes + reopens with a fresh frame.
  const reconnectRef = useRef(socketWithCb.reconnect)
  reconnectRef.current = socketWithCb.reconnect

  // ---- submission ----
  const handleSubmit = useCallback(
    (question: string) => {
      const trimmed = question.trim()
      if (!trimmed) return
      if (trimmed.length > MAX_QUESTION_LENGTH) return
      if (socketWithCb.inflightRequestId !== null) return
      try {
        socketWithCb.sendUserMessage({ question: trimmed, days: safeDays, region: null })
      } catch {
        // The connection error banner already surfaces failures.
      }
    },
    [socketWithCb, safeDays],
  )

  // ---- new chat ----
  const [busyCreate, setBusyCreate] = useState(false)
  const handleCreate = useCallback(async () => {
    if (busyCreate) return
    setBusyCreate(true)
    try {
      const conv = await createConversation(undefined)
      listQ.refresh()
      setActiveId(conv.id)
    } catch {
      // Already surfaced via listQ.error on subsequent fetches; we keep
      // the UI here clean by leaving the previous state alone.
    } finally {
      setBusyCreate(false)
    }
  }, [busyCreate, listQ])

  // ---- selection ----
  const handleSelect = useCallback((id: number) => setActiveId(id), [])

  // ---- narrow viewport drawer ----
  const [drawerOpen, setDrawerOpen] = useState(false)

  const headerContext = (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <span className="text-fg-muted">Context:</span>
      <span className="text-fg-secondary">Last {safeDays} days</span>
      <span className="text-fg-muted">·</span>
      <AiProviderBadge status={aiStatus} />
    </span>
  )

  const headerActions = (
    <div className="flex items-center gap-2">
      <PeriodSelector testId="analyst-period-selector" />
      <button
        type="button"
        className="lg:hidden"
        onClick={() => setDrawerOpen((o) => !o)}
        aria-expanded={drawerOpen}
        aria-controls="analyst-drawer"
        data-testid="analyst-drawer-toggle"
        title="Conversations"
      >
        <StatusBadge tone="info">Conversations</StatusBadge>
      </button>
    </div>
  )

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="AI Cost Analyst"
        subtitle="Ask grounded questions about your AWS environment"
        context={headerContext}
        actions={headerActions}
      />

      {authDisabled ? (
        <AuthDisabledNotice />
      ) : aiStatus && !aiStatus.ai_enabled ? (
        <SectionCard title="AI analysis is unavailable" padded>
          <div
            role="alert"
            className="
              flex flex-col gap-2 text-sm text-fg-secondary
            "
          >
            <p>
              AI generation is disabled in configuration. The AI Cost
              Analyst can still browse conversation history, but it cannot
              produce new answers until an operator enables the AI
              provider.
            </p>
            <p className="text-xs text-fg-muted">
              {aiStatus.message ?? 'AI_DISABLED'}
            </p>
          </div>
        </SectionCard>
      ) : null}

      {listError && !authDisabled ? (
        <ErrorState
          title="Could not load conversations"
          message={
            listError instanceof Error ? listError.message : 'Unknown error'
          }
          onRetry={() => listQ.refresh()}
        />
      ) : null}

      <div
        className="
          grid h-[calc(100vh-220px)] min-h-[480px] gap-3
          lg:grid-cols-[260px_minmax(0,1fr)]
        "
        data-testid="analyst-workspace"
      >
        {/* Desktop left rail */}
        <div className="hidden h-full lg:block">
          <ConversationList
            conversations={conversations}
            loading={listQ.entry.status === 'loading' || listQ.entry.status === 'refreshing'}
            error={null}
            activeId={activeId}
            onSelect={handleSelect}
            onCreate={handleCreate}
            busyCreate={busyCreate}
          />
        </div>

        {/* Mobile drawer */}
        <div
          id="analyst-drawer"
          className={[
            'fixed inset-0 z-30 lg:hidden',
            drawerOpen ? 'pointer-events-auto' : 'pointer-events-none',
          ].join(' ')}
          aria-hidden={!drawerOpen}
        >
          <div
            className={[
              'absolute inset-0 bg-fg-primary/40 transition-opacity',
              drawerOpen ? 'opacity-100' : 'opacity-0',
            ].join(' ')}
            onClick={() => setDrawerOpen(false)}
          />
          <div
            className={[
              'absolute left-0 top-0 h-full w-72 max-w-[85vw] bg-bg-elevated transition-transform',
              drawerOpen ? 'translate-x-0' : '-translate-x-full',
            ].join(' ')}
          >
            <ConversationList
              conversations={conversations}
              loading={listQ.entry.status === 'loading' || listQ.entry.status === 'refreshing'}
              error={null}
              activeId={activeId}
              onSelect={(id) => {
                handleSelect(id)
                setDrawerOpen(false)
              }}
              onCreate={() => {
                void handleCreate()
                setDrawerOpen(false)
              }}
              busyCreate={busyCreate}
            />
          </div>
        </div>

        <div className="h-full">
          {authDisabled ? (
            <SectionCard title="AI Cost Analyst" padded>
              <div
                role="alert"
                className="text-sm text-fg-secondary"
              >
                Persistent conversations are unavailable while authentication
                is disabled. Enable authentication to use this workspace.
              </div>
            </SectionCard>
          ) : activeId === null ? (
            <SectionCard title="AI Cost Analyst" padded>
              <div className="flex flex-col gap-2 text-sm text-fg-secondary">
                <p>
                  Press “New chat” to start a grounded analysis. Each
                  question is answered using fresh AWS evidence — Cost
                  Explorer, Compute Optimizer, Cost Optimization Hub and
                  deterministic rule findings.
                </p>
                {listQ.entry.status === 'loading' && conversations.length === 0 ? (
                  <LoadingSkeletonRows rows={3} />
                ) : null}
              </div>
            </SectionCard>
          ) : detailError ? (
            <ErrorState
              title="Could not load conversation"
              message={
                detailError instanceof Error ? detailError.message : 'Unknown error'
              }
              onRetry={() => detailQ.refresh()}
            />
          ) : (
            <ConversationView
              conversation={conversation}
              messages={messages}
              loadingMessages={
                messagesQ.entry.status === 'loading' || messagesQ.entry.status === 'refreshing'
              }
              historyError={
                messagesError instanceof Error ? messagesError.message : null
              }
              connectionState={socketWithCb.state}
              closeCode={socketWithCb.closeCode}
              inflight={socketWithCb.inflightRequestId !== null}
              lastAssistant={socketWithCb.lastAssistant}
              lastErrorMessage={
                socketWithCb.lastError
                  ? (socketWithCb.lastError.message ?? null)
                  : null
              }
              unavailable={authDisabled ? 'auth_disabled' : null}
              days={safeDays}
              onSubmit={handleSubmit}
              onRetryConnection={() => reconnectRef.current()}
            />
          )}
        </div>
      </div>
    </div>
  )
}
