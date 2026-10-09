// Phase 6C — Typed surface for the AI Cost Analyst.
//
// Mirrors the backend schemas verbatim (no widening, no fabrication).
// The frontend is a presentation + interaction layer; AWS-backed
// numbers always come from Phase 1–3 evidence, never from the LLM
// and never from these TypeScript definitions.

// `LookbackDays` lives in `./finops.ts` (same value space — `7|30|60|90`).
// We re-use the existing type to keep the surface uniform.
import type { LookbackDays } from './finops'

// ---------------------------------------------------------------------------
// AI status (GET /api/ai/status)
// ---------------------------------------------------------------------------

export type AIProviderStatus = 'OK' | 'DISABLED' | 'DEGRADED'

export interface AIStatusResponse {
  status: AIProviderStatus
  ai_enabled: boolean
  litellm_reachable: boolean
  model_alias: string
  /** scheme://host:port (no credentials in the wire). */
  litellm_base_url: string
  timeout_seconds: number
  max_output_tokens: number
  context_limits: Record<string, number>
  message?: string | null
}

// ---------------------------------------------------------------------------
// AI generation envelope
// ---------------------------------------------------------------------------

export type AIGenerationStatus =
  | 'SUCCESS'
  | 'PARTIAL_SUCCESS'
  | 'FAILED'
  | 'DISABLED'
  | 'UNAVAILABLE'

export interface AIGrounding {
  account_id?: string | null
  region?: string | null
  days: number
  cost_evidence_used: boolean
  recommendations_used: number
  capabilities_used: boolean
}

export interface AICitation {
  /** Citation shape is intentionally permissive; backend validates
   *  against the evidence package, so we don't fabricate keys. */
  [key: string]: unknown
}

export interface AIResponse {
  status: AIGenerationStatus
  operation: 'executive_summary' | 'analyze' | 'explain' | string
  answer: string
  model?: string | null
  grounding: AIGrounding
  citations: AICitation[]
  warnings: string[]
}

// ---------------------------------------------------------------------------
// Conversations (Phase 5B)
// ---------------------------------------------------------------------------

export type MessageRole = 'USER' | 'ASSISTANT' | 'SYSTEM_EVENT'

export interface Conversation {
  id: number
  user_id: number
  title: string
  is_archived: boolean
  created_at: string
  updated_at: string
  last_message_at: string | null
}

export interface ConversationListResponse {
  conversations: Conversation[]
  count: number
  limit: number
  offset: number
}

export interface ConversationDetailResponse {
  conversation: Conversation
  message_count: number
}

export interface ConversationMessage {
  id: number
  conversation_id: number
  role: MessageRole
  content: string
  created_at: string
  operation_type?: string | null
  model_alias?: string | null
  grounding_metadata?: Record<string, unknown> | null
  evidence_references?: AICitation[] | null
  warnings?: string[] | null
  token_usage?: Record<string, unknown> | null
  error_code?: string | null
}

export interface ConversationMessagesResponse {
  messages: ConversationMessage[]
  count: number
  limit: number
  offset: number
  has_more: boolean
}

// ---------------------------------------------------------------------------
// WebSocket protocol (Phase 5C)
// ---------------------------------------------------------------------------

/** Server-emitted protocol version; pinned against the `connected` frame. */
export type ProtocolVersion = 'v1'

/** Conversation state surfaced to the UI by `useConversationSocket`. */
export type ConnectionState =
  | 'idle'
  | 'connecting'
  | 'connected'
  | 'processing'
  | 'completed'
  | 'disconnected'
  | 'authorization_failure'
  | 'retryable_failure'
  | 'protocol_violation'

/** Stable, sanitized error codes from the backend. */
export type WSErrorCode =
  | 'ProtocolViolation'
  | 'InvalidJSON'
  | 'UnsupportedEventType'
  | 'OversizedFrame'
  | 'InvalidQuestion'
  | 'InvalidLookbackDays'
  | 'InvalidRequestId'
  | 'AIUnavailable'
  | 'AIDisabled'
  | 'AIUnreachable'
  | 'Busy'
  | 'Timeout'
  | 'Unauthorized'
  | 'Forbidden'
  | 'ConversationNotFound'

export interface ServerConnectedFrame {
  type: 'connected'
  protocol_version: ProtocolVersion
  conversation_id: number
  user_id: number
  role: string
  heartbeat_interval_seconds: number
}

export interface ServerUserAcceptedFrame {
  type: 'user_message_accepted'
  request_id: string
  conversation_id: number
  message_id: number
}

export interface ServerAiProcessingFrame {
  type: 'ai_processing'
  request_id: string
  conversation_id: number
}

export interface ServerAssistantFrame {
  type: 'assistant_message'
  request_id: string
  conversation_id: number
  message_id: number
  operation: string
  model?: string | null
  answer: string
  grounding: AIGrounding
  citations: AICitation[]
  warnings: string[]
}

export interface ServerErrorFrame {
  type: 'error'
  request_id?: string | null
  conversation_id?: number | null
  code: WSErrorCode | string
  message: string
}

export interface ServerPongFrame {
  type: 'pong'
  ts?: number | null
}

export type ServerFrame =
  | ServerConnectedFrame
  | ServerUserAcceptedFrame
  | ServerAiProcessingFrame
  | ServerAssistantFrame
  | ServerErrorFrame
  | ServerPongFrame

// Client events sent over the wire (kept narrow to match the Pydantic union).
export interface ClientUserMessageFrame {
  type: 'user_message'
  request_id: string
  question: string
  region?: string | null
  days: LookbackDays
}

export interface ClientPingFrame {
  type: 'ping'
  ts?: number | null
}

export type ClientFrame = ClientUserMessageFrame | ClientPingFrame

// ---------------------------------------------------------------------------
// Cross-cutting constants
// ---------------------------------------------------------------------------

/** Hard cap on the user-question length; mirrors `MAX_QUESTION_LENGTH`. */
export const MAX_QUESTION_LENGTH = 2000

/** Allowed lookback windows; mirrors the Pydantic `ALLOWED_LOOKBACK_DAYS`. */
export const AI_ALLOWED_LOOKBACK_DAYS: readonly LookbackDays[] = [7, 30, 60, 90] as const

/** Auth-disabled error code; mirrored from `app/api/conversations.py`. */
export const ERR_AUTH_DISABLED = 'AuthDisabled'

/** Recv-side heartbeat multiplier — we ping at 0.7× the server hint. */
export const HEARTBEAT_MULTIPLIER = 0.7

/** Bounded FIFO for duplicate `request_id` suppression. */
export const MAX_RECENT_REQUEST_IDS = 128

/** User-facing label mapping for the `WSErrorCode` set. */
export const WS_ERROR_LABELS: Record<string, string> = {
  ProtocolViolation: 'Protocol violation',
  InvalidJSON: 'Invalid event payload',
  UnsupportedEventType: 'Unsupported event type',
  OversizedFrame: 'Message too large',
  InvalidQuestion: 'Question was rejected by the server',
  InvalidLookbackDays: 'Lookback period was rejected',
  InvalidRequestId: 'Request identifier was rejected',
  AIUnavailable: 'AI analysis is temporarily unavailable',
  AIDisabled: 'AI analysis is currently disabled',
  AIUnreachable: 'AI analyst is unreachable',
  Busy: 'Another question is already in progress',
  Timeout: 'AI analysis timed out',
  Unauthorized: 'You are not authorized for this conversation',
  Forbidden: 'Your role cannot use this conversation',
  ConversationNotFound: 'This conversation no longer exists',
}

/** Indicates the AI response reflects insufficient fresh evidence. */
export function isInsufficientEvidence(
  grounding: AIGrounding | undefined | null,
  warnings: string[] | undefined | null,
): boolean {
  if (!grounding) return false
  const noEvidence =
    !grounding.cost_evidence_used &&
    grounding.recommendations_used === 0 &&
    !grounding.capabilities_used
  if (!noEvidence) return false
  const ws = warnings ?? []
  return ws.some((w) => /insufficient|no.{0,4}evidence/i.test(w))
}
