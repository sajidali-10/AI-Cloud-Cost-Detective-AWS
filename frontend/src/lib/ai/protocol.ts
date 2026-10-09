// Phase 6C — WebSocket protocol primitives.
//
// Builds client frames and validates that JWTs NEVER end up in:
//   * URLs
//   * path parameters
//   * error messages
//   * logs
//
// The subprotocol mechanism (`bearer.<jwt>`) is the only place the
// token travels.  See `app/api/ws_auth.py` for the matching server
// contract.

import type {
  ClientFrame,
  ClientPingFrame,
  ClientUserMessageFrame,
  ProtocolVersion,
  ServerFrame,
} from '../../types/ai'
import { MAX_QUESTION_LENGTH } from '../../types/ai'
import { ALLOWED_LOOKBACK_DAYS, type LookbackDays } from '../../types/finops'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Path component for the WS endpoint — single source of truth. */
export const WS_PATH_PREFIX = '/api/ws/conversations'

/** Subprotocol prefix; matches `app/api/ws_auth.py::extract_ws_token`. */
export const WS_SUBPROTOCOL_PREFIX = 'bearer.'

/** Server hint; we send pings at `HEARTBEAT_MULTIPLIER * heartbeat_interval`. */
export const HEARTBEAT_DEFAULT_SECONDS = 30

/** Protocol version pinned against the `connected` frame. */
export const PROTOCOL_VERSION: ProtocolVersion = 'v1'

// ---------------------------------------------------------------------------
// UUID v4 (RFC 4122 §4.4).  Uses crypto.randomUUID when available and
// falls back to a Math.random implementation for old test browsers.
// ---------------------------------------------------------------------------

export function uuidv4(): string {
  const g = globalThis as unknown as { crypto?: { randomUUID?: () => string } }
  if (g.crypto && typeof g.crypto.randomUUID === 'function') {
    return g.crypto.randomUUID()
  }
  // Fallback: RFC 4122 §4.4 algorithm.
  const bytes = new Uint8Array(16)
  for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // variant 10xx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'))
  return (
    hex.slice(0, 4).join('') +
    '-' +
    hex.slice(4, 6).join('') +
    '-' +
    hex.slice(6, 8).join('') +
    '-' +
    hex.slice(8, 10).join('') +
    '-' +
    hex.slice(10, 16).join('')
  )
}

// ---------------------------------------------------------------------------
// URL construction — NO token in URL.
// ---------------------------------------------------------------------------

export interface BuildWebSocketUrlArgs {
  conversationId: number
  /** Override origin (test only). */
  originOverride?: string
  /** Override protocol (test only). */
  protocolOverride?: 'ws:' | 'wss:'
}

/**
 * Build the WS URL for `/api/ws/conversations/{id}`.
 *
 * SECURITY: The token MUST NOT appear anywhere in the returned URL —
 * not in the path, not as a query parameter, not as a fragment.  The
 * caller passes the token separately to `buildSubprotocols()`.
 *
 * The scheme is derived from `window.location.protocol`; `https:` →
 * `wss:`.  If `window` is undefined (test harness with jsdom defaults
 * to an example host) we fall back to `ws:`.
 */
export function buildWebSocketUrl(args: BuildWebSocketUrlArgs): string {
  const id = Math.floor(args.conversationId)
  if (!Number.isFinite(id) || id <= 0) {
    throw new Error(`buildWebSocketUrl: invalid conversationId=${args.conversationId}`)
  }
  const override = args.protocolOverride ?? null
  const overrideOrigin = args.originOverride ?? null
  let scheme: 'ws:' | 'wss:'
  let host = ''
  if (typeof window !== 'undefined' && window.location) {
    const proto = String(window.location.protocol || '').toLowerCase()
    scheme = override ?? (proto === 'https:' ? 'wss:' : 'ws:')
    host = window.location.host
  } else {
    scheme = override ?? 'ws:'
    host = ''
  }
  const base = overrideOrigin ?? (host ? `${scheme}//${host}` : `${scheme}//`)
  const path = `${WS_PATH_PREFIX}/${id}`
  // Belt-and-braces: assert the URL contains no token material.  We
  // do this by checking for any of `?token=`, `?jwt=`, `&token=`,
  // `&jwt=`, `Authorization`, or `Bearer` substrings.
  const fullUrl = `${base}${path}`
  assertUrlHasNoToken(fullUrl)
  return fullUrl
}

export function assertUrlHasNoToken(url: string): void {
  if (!url) return
  if (/\?(token|jwt|access_token)=/i.test(url)) {
    throw new Error('buildWebSocketUrl: token must not appear in URL')
  }
  if (/[?&](token|jwt|access_token)=/i.test(url)) {
    throw new Error('buildWebSocketUrl: token must not appear in URL')
  }
  if (/\b(Bearer|Authorization)\b/i.test(url)) {
    throw new Error('buildWebSocketUrl: token must not appear in URL')
  }
}

// ---------------------------------------------------------------------------
// Subprotocol construction
// ---------------------------------------------------------------------------

/**
 * Build the `Sec-WebSocket-Protocol` value the browser should offer.
 *
 * Returns `["bearer.<token>"]` when a token is supplied, `[]` otherwise
 * (so the caller can detect "no token" without ambiguity).
 */
export function buildSubprotocols(token: string | null | undefined): string[] {
  if (!token) return []
  // Defensive: if the caller somehow passed a token with newlines we
  // throw — that is a credential-leak bug.
  if (/[\r\n]/.test(token)) {
    throw new Error('buildSubprotocols: token contains invalid characters')
  }
  return [`${WS_SUBPROTOCOL_PREFIX}${token}`]
}

/**
 * Returns true if the supplied token shape looks like a JWT.  Used
 * purely as a defensive guard in tests to assert that constructed
 * subprotocols embed a real token (not a placeholder like "secret").
 */
export function isJwtLike(token: string | null | undefined): boolean {
  if (!token) return false
  const parts = token.split('.')
  return parts.length === 3 && parts.every((p) => /^[A-Za-z0-9_-]+$/.test(p))
}

// ---------------------------------------------------------------------------
// Client frame builders
// ---------------------------------------------------------------------------

export interface BuildUserMessageArgs {
  requestId?: string
  question: string
  region?: string | null
  days: LookbackDays
}

export function buildUserMessageFrame(args: BuildUserMessageArgs): ClientUserMessageFrame {
  const q = (args.question ?? '').trim()
  if (!q) throw new Error('buildUserMessageFrame: question must be non-empty')
  if (q.length > MAX_QUESTION_LENGTH) {
    throw new Error(
      `buildUserMessageFrame: question length ${q.length} exceeds MAX_QUESTION_LENGTH`,
    )
  }
  if (!ALLOWED_LOOKBACK_DAYS.includes(args.days)) {
    throw new Error(`buildUserMessageFrame: days=${args.days} is not allowed`)
  }
  const requestId = args.requestId ?? uuidv4()
  if (!isUuid(requestId)) {
    throw new Error('buildUserMessageFrame: request_id must be a UUID')
  }
  const region = args.region ? String(args.region).trim().slice(0, 64) : null
  return {
    type: 'user_message',
    request_id: requestId,
    question: q,
    region: region && region.length > 0 ? region : undefined,
    days: args.days,
  }
}

export function buildPingFrame(ts?: number | null): ClientPingFrame {
  return { type: 'ping', ts: ts ?? null }
}

// ---------------------------------------------------------------------------
// Server-frame parsing (runtime narrowing — no `any` leakage)
// ---------------------------------------------------------------------------

export function isServerFrame(value: unknown): value is ServerFrame {
  if (!value || typeof value !== 'object') return false
  const t = (value as { type?: unknown }).type
  return (
    t === 'connected' ||
    t === 'user_message_accepted' ||
    t === 'ai_processing' ||
    t === 'assistant_message' ||
    t === 'error' ||
    t === 'pong'
  )
}

export function parseServerFrame(payload: unknown): ServerFrame | null {
  if (!isServerFrame(payload)) return null
  return payload as ServerFrame
}

// ---------------------------------------------------------------------------
// Misc helpers
// ---------------------------------------------------------------------------

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/

export function isUuid(value: string): boolean {
  return UUID_RE.test(value)
}

/** Build a `ClientFrame` JSON-string and assert no token material leaks. */
export function serializeClientFrame(frame: ClientFrame): string {
  const s = JSON.stringify(frame)
  // Belt-and-braces: the wire payload must never contain token-shaped
  // strings.  We refuse to send if a JWT-shaped value is embedded.
  if (/\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/.test(s)) {
    throw new Error('serializeClientFrame: token-shaped value present in frame')
  }
  return s
}
