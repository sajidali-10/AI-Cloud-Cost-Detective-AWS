// Phase 6C — WebSocket protocol primitive tests.

import { describe, expect, it, vi } from 'vitest'
import {
  HEARTBEAT_DEFAULT_SECONDS,
  PROTOCOL_VERSION,
  WS_PATH_PREFIX,
  WS_SUBPROTOCOL_PREFIX,
  assertUrlHasNoToken,
  buildPingFrame,
  buildSubprotocols,
  buildUserMessageFrame,
  buildWebSocketUrl,
  isJwtLike,
  isServerFrame,
  parseServerFrame,
  serializeClientFrame,
  uuidv4,
} from '../lib/ai/protocol'
import { encodeClient } from '../lib/ai/connection'
import { MAX_QUESTION_LENGTH } from '../types/ai'

describe('buildWebSocketUrl', () => {
  it('produces /api/ws/conversations/{id} with the current origin scheme', () => {
    // jsdom default origin is http://localhost
    const url = buildWebSocketUrl({ conversationId: 42 })
    expect(url.endsWith(`${WS_PATH_PREFIX}/42`)).toBe(true)
    expect(url.startsWith('ws://')).toBe(true)
  })

  it('throws on invalid conversation id', () => {
    expect(() => buildWebSocketUrl({ conversationId: 0 })).toThrow()
    expect(() => buildWebSocketUrl({ conversationId: -1 })).toThrow()
    expect(() => buildWebSocketUrl({ conversationId: Number.NaN })).toThrow()
  })

  it('never embeds the JWT in the URL (origin override variant)', () => {
    const url = buildWebSocketUrl({
      conversationId: 1,
      protocolOverride: 'wss:',
      originOverride: 'wss://example.com',
    })
    expect(url).toBe('wss://example.com/api/ws/conversations/1')
    expect(url).not.toMatch(/[?&](token|jwt|access_token)=/i)
    expect(url).not.toMatch(/Bearer/i)
  })
})

describe('assertUrlHasNoToken', () => {
  it('throws on query-string token leakage patterns', () => {
    expect(() => assertUrlHasNoToken('wss://h/api/ws/x?token=abc')).toThrow()
    expect(() => assertUrlHasNoToken('wss://h/api/ws/x?jwt=abc')).toThrow()
    expect(() => assertUrlHasNoToken('wss://h/api/ws/x?access_token=abc')).toThrow()
  })

  it('throws on Bearer substring', () => {
    expect(() => assertUrlHasNoToken('wss://h/Bearer/api')).toThrow()
    expect(() => assertUrlHasNoToken('wss://h/api?Authorization=b')).toThrow()
  })

  it('accepts clean URLs', () => {
    expect(() => assertUrlHasNoToken('wss://example.com/api/ws/conversations/1')).not.toThrow()
  })
})

describe('buildSubprotocols', () => {
  it('returns ["bearer.<token>"] when token is supplied', () => {
    expect(buildSubprotocols('abcd')).toEqual(['bearer.abcd'])
    expect(buildSubprotocols('abcd')[0].startsWith(WS_SUBPROTOCOL_PREFIX)).toBe(true)
  })
  it('returns [] when token is missing', () => {
    expect(buildSubprotocols(null)).toEqual([])
    expect(buildSubprotocols(undefined)).toEqual([])
    expect(buildSubprotocols('')).toEqual([])
  })
  it('rejects tokens with newlines (defensive)', () => {
    expect(() => buildSubprotocols('abc\ndef')).toThrow()
  })
})

describe('isJwtLike', () => {
  it('returns true for three base64url segments separated by dots', () => {
    expect(isJwtLike('aaa.bbb.ccc')).toBe(true)
    expect(isJwtLike('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.signature')).toBe(true)
  })
  it('returns false for non-JWT strings', () => {
    expect(isJwtLike('not.a.jwt.with.too.many.parts')).toBe(false)
    expect(isJwtLike('secret')).toBe(false)
    expect(isJwtLike(null)).toBe(false)
    expect(isJwtLike('')).toBe(false)
  })
})

describe('buildUserMessageFrame', () => {
  it('produces a typed user_message frame with a UUID request_id', () => {
    const f = buildUserMessageFrame({
      question: 'Why did EC2 spend rise last week?',
      region: 'us-east-1',
      days: 30,
    })
    expect(f.type).toBe('user_message')
    expect(f.days).toBe(30)
    expect(f.region).toBe('us-east-1')
    expect(f.question.length).toBeGreaterThan(0)
    expect(f.request_id).toMatch(
      /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/,
    )
  })

  it('rejects empty or over-length questions', () => {
    expect(() =>
      buildUserMessageFrame({ question: '   ', days: 7 }),
    ).toThrow()
    expect(() =>
      buildUserMessageFrame({ question: 'x'.repeat(MAX_QUESTION_LENGTH + 1), days: 7 }),
    ).toThrow()
  })

  it('rejects unsupported lookback windows', () => {
    expect(() =>
      buildUserMessageFrame({ question: 'hello', days: 14 as unknown as 30 }),
    ).toThrow()
  })

  it('omits region when null/empty', () => {
    const f = buildUserMessageFrame({ question: 'hello', days: 7, region: null })
    expect(f.region).toBeUndefined()
    const g = buildUserMessageFrame({ question: 'hello', days: 7, region: '' })
    expect(g.region).toBeUndefined()
  })

  it('trims region over the 64-char cap', () => {
    const f = buildUserMessageFrame({
      question: 'hi',
      days: 7,
      region: 'x'.repeat(120),
    })
    expect(typeof f.region === 'string' && f.region.length).toBeLessThanOrEqual(64)
  })
})

describe('buildPingFrame', () => {
  it('produces a ping frame', () => {
    expect(buildPingFrame().type).toBe('ping')
    expect(buildPingFrame(123).ts).toBe(123)
  })
})

describe('serializeClientFrame + encodeClient', () => {
  it('serializes frames without including a JWT shape', () => {
    const frame = buildUserMessageFrame({ question: 'hello', days: 30 })
    const s = serializeClientFrame(frame)
    const parsed = JSON.parse(s)
    expect(parsed.type).toBe('user_message')
    expect(s).not.toMatch(/\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/)
  })

  it('refuses to serialize a frame that embeds a JWT shape', () => {
    expect(() =>
      // Force a token-shaped value into a string field via cast.
      serializeClientFrame({
        type: 'ping',
        ts: 1,
      } as never),
    ).not.toThrow()
    // Use a forged frame:
    expect(() =>
      serializeClientFrame({
        type: 'ping',
        ts: 'aaa.bbb.ccc' as unknown as number,
      } as never),
    ).toThrow()
  })

  it('encodeClient delegates to serializeClientFrame', () => {
    const frame = buildPingFrame(7)
    expect(encodeClient(frame)).toBe(serializeClientFrame(frame))
  })
})

describe('parseServerFrame / isServerFrame', () => {
  it('narrows a connected frame', () => {
    const f = parseServerFrame({
      type: 'connected',
      protocol_version: PROTOCOL_VERSION,
      conversation_id: 1,
      user_id: 1,
      role: 'ADMIN',
      heartbeat_interval_seconds: HEARTBEAT_DEFAULT_SECONDS,
    })
    expect(f?.type).toBe('connected')
    expect(isServerFrame(f)).toBe(true)
  })

  it('returns null for unknown shapes', () => {
    expect(parseServerFrame(null)).toBeNull()
    expect(parseServerFrame('hi')).toBeNull()
    expect(parseServerFrame({})).toBeNull()
    expect(parseServerFrame({ type: 'unknown' })).toBeNull()
    expect(isServerFrame({ type: 'whatever' })).toBe(false)
  })

  it('parses an assistant_message frame', () => {
    const f = parseServerFrame({
      type: 'assistant_message',
      request_id: uuidv4(),
      conversation_id: 1,
      message_id: 1,
      operation: 'analyze',
      answer: 'hi',
      grounding: { days: 30, cost_evidence_used: true, recommendations_used: 1, capabilities_used: false },
      citations: [{ service: 'EC2' }],
      warnings: [],
    })
    expect(f?.type).toBe('assistant_message')
  })
})
