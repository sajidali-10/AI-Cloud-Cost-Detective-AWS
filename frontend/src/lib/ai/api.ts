// Phase 6C — REST API wrappers for the AI workspace.
//
// Centralizes all `/api/ai` and `/api/conversations` traffic so no
// component composes URL strings inline and no component injects the
// bearer token by hand.  Errors normalize through `ApiError`; the
// `AuthDisabled` 503 contract is preserved verbatim.

import { apiFetch } from '../api'
import type {
  AIStatusResponse,
  Conversation,
  ConversationDetailResponse,
  ConversationListResponse,
  ConversationMessagesResponse,
} from '../../types/ai'
import type { LookbackDays } from '../../types/finops'

// ---------------------------------------------------------------------------
// AI status
// ---------------------------------------------------------------------------

export async function fetchAIStatus(): Promise<AIStatusResponse> {
  return apiFetch<AIStatusResponse>('/api/ai/status', { timeoutMs: 10_000 })
}

// ---------------------------------------------------------------------------
// Conversations
// ---------------------------------------------------------------------------

export interface ListConversationsParams {
  limit?: number
  offset?: number
  archived?: boolean
}

export async function fetchConversations(
  params: ListConversationsParams = {},
): Promise<ConversationListResponse> {
  const qs = new URLSearchParams()
  if (typeof params.limit === 'number') qs.set('limit', String(params.limit))
  if (typeof params.offset === 'number') qs.set('offset', String(params.offset))
  if (typeof params.archived === 'boolean') qs.set('archived', String(params.archived))
  const suffix = qs.toString()
  return apiFetch<ConversationListResponse>(
    suffix ? `/api/conversations?${suffix}` : '/api/conversations',
    { timeoutMs: 10_000 },
  )
}

export async function fetchConversation(id: number): Promise<ConversationDetailResponse> {
  return apiFetch<ConversationDetailResponse>(`/api/conversations/${id}`, {
    timeoutMs: 10_000,
  })
}

export interface ListMessagesParams {
  limit?: number
  offset?: number
}

export async function fetchConversationMessages(
  id: number,
  params: ListMessagesParams = {},
): Promise<ConversationMessagesResponse> {
  const qs = new URLSearchParams()
  if (typeof params.limit === 'number') qs.set('limit', String(params.limit))
  if (typeof params.offset === 'number') qs.set('offset', String(params.offset))
  const suffix = qs.toString()
  return apiFetch<ConversationMessagesResponse>(
    suffix ? `/api/conversations/${id}/messages?${suffix}` : `/api/conversations/${id}/messages`,
    { timeoutMs: 10_000 },
  )
}

export async function createConversation(title?: string): Promise<Conversation> {
  return apiFetch<Conversation>('/api/conversations', {
    method: 'POST',
    json: title ? { title } : {},
    timeoutMs: 10_000,
  })
}

export async function renameConversation(id: number, title: string): Promise<Conversation> {
  return apiFetch<Conversation>(`/api/conversations/${id}`, {
    method: 'PATCH',
    json: { title },
    timeoutMs: 10_000,
  })
}

export async function archiveConversation(
  id: number,
  archived: boolean,
): Promise<Conversation> {
  return apiFetch<Conversation>(`/api/conversations/${id}`, {
    method: 'PATCH',
    json: { is_archived: archived },
    timeoutMs: 10_000,
  })
}

export async function deleteConversation(
  id: number,
): Promise<{ status: 'ok'; deleted: boolean; conversation_id: number }> {
  return apiFetch(`/api/conversations/${id}`, {
    method: 'DELETE',
    timeoutMs: 10_000,
  })
}

// ---------------------------------------------------------------------------
// REST conversation AI (auth-enabled only — backend rejects with
// AuthDisabled when AUTH_ENABLED=false).
// ---------------------------------------------------------------------------

export interface ConversationAnalyzePayload {
  question: string
  region?: string | null
  days: LookbackDays
}

export async function analyzeViaREST(
  conversationId: number,
  payload: ConversationAnalyzePayload,
): Promise<unknown> {
  return apiFetch(`/api/conversations/${conversationId}/analyze`, {
    method: 'POST',
    json: {
      question: payload.question,
      region: payload.region ?? null,
      days: payload.days,
    },
    timeoutMs: 60_000,
  })
}

// ---------------------------------------------------------------------------
// Cache keys — kept here so tests can pin against the same string
// the store uses without re-deriving it.
// ---------------------------------------------------------------------------

export function conversationsListKey(archived?: boolean): string {
  return `ai:conversations:${archived === undefined ? 'all' : archived ? 'archived' : 'active'}`
}

export function conversationDetailKey(id: number): string {
  return `ai:conversation:${id}`
}

export function conversationMessagesKey(id: number, offset: number): string {
  return `ai:conversation:${id}:messages:${offset}`
}

export function aiStatusKey(): string {
  return 'ai:status'
}
