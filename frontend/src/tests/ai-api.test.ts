// Phase 6C — REST API wrapper tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, __resetApiForTests, configureApi } from '../lib/api'
import {
  analyzeViaREST,
  archiveConversation,
  createConversation,
  deleteConversation,
  fetchAIStatus,
  fetchConversation,
  fetchConversationMessages,
  fetchConversations,
  renameConversation,
} from '../lib/ai/api'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('AI / Conversations REST wrappers', () => {
  beforeEach(() => {
    __resetApiForTests()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('fetchAIStatus calls /api/ai/status with no auth', async () => {
    let captured: { url: string; init?: RequestInit } | null = null
    const transport = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      captured = { url, init }
      return Promise.resolve(
        mockJson(200, {
          status: 'OK',
          ai_enabled: true,
          litellm_reachable: true,
          model_alias: 'cost-detective-free',
          litellm_base_url: 'http://litellm:4000',
          timeout_seconds: 30,
          max_output_tokens: 800,
          context_limits: {},
        }),
      )
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    const r = await fetchAIStatus()
    expect(r.status).toBe('OK')
    expect(captured?.url).toBe('/api/ai/status')
  })

  it('fetchConversations encodes limit + offset + archived', async () => {
    const calls: string[] = []
    const transport = vi.fn().mockImplementation((url: string) => {
      calls.push(url)
      return Promise.resolve(
        mockJson(200, { conversations: [], count: 0, limit: 50, offset: 0 }),
      )
    })
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await fetchConversations({ limit: 25, offset: 5, archived: false })
    expect(calls[0]).toBe('/api/conversations?limit=25&offset=5&archived=false')
  })

  it('createConversation POSTs with optional title', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockJson(201, {
        id: 1,
        user_id: 1,
        title: 'New',
        is_archived: false,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        last_message_at: null,
      }),
    )
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    const conv = await createConversation('My chat')
    expect(conv.id).toBe(1)
    expect((transport.mock.calls[0][1] as RequestInit).method).toBe('POST')
    expect((transport.mock.calls[0][1] as RequestInit).body).toBe(JSON.stringify({ title: 'My chat' }))
  })

  it('createConversation omits title when not provided', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockJson(201, {
        id: 2,
        user_id: 1,
        title: 'New Cost Analysis',
        is_archived: false,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        last_message_at: null,
      }),
    )
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await createConversation()
    expect((transport.mock.calls[0][1] as RequestInit).body).toBe(JSON.stringify({}))
  })

  it('renameConversation + archiveConversation PATCH', async () => {
    const transport = vi
      .fn()
      .mockResolvedValueOnce(
        mockJson(200, {
          id: 1,
          user_id: 1,
          title: 'Renamed',
          is_archived: false,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          last_message_at: null,
        }),
      )
      .mockResolvedValueOnce(
        mockJson(200, {
          id: 1,
          user_id: 1,
          title: 'Renamed',
          is_archived: true,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z',
          last_message_at: null,
        }),
      )
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await renameConversation(1, 'Renamed')
    await archiveConversation(1, true)
    expect(transport.mock.calls[0][0]).toBe('/api/conversations/1')
    expect((transport.mock.calls[0][1] as RequestInit).method).toBe('PATCH')
    expect((transport.mock.calls[1][1] as RequestInit).method).toBe('PATCH')
  })

  it('deleteConversation sends DELETE and returns the deleted envelope', async () => {
    const transport = vi
      .fn()
      .mockResolvedValueOnce(mockJson(200, { status: 'ok', deleted: true, conversation_id: 1 }))
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    const r = await deleteConversation(1)
    expect(r.deleted).toBe(true)
    expect((transport.mock.calls[0][1] as RequestInit).method).toBe('DELETE')
  })

  it('fetchConversation + fetchConversationMessages hit the right paths', async () => {
    const transport = vi
      .fn()
      .mockResolvedValueOnce(
        mockJson(200, {
          conversation: {
            id: 1,
            user_id: 1,
            title: 'T',
            is_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            last_message_at: null,
          },
          message_count: 0,
        }),
      )
      .mockResolvedValueOnce(
        mockJson(200, { messages: [], count: 0, limit: 100, offset: 0, has_more: false }),
      )
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await fetchConversation(1)
    await fetchConversationMessages(1, { limit: 50, offset: 0 })
    expect(transport.mock.calls[0][0]).toBe('/api/conversations/1')
    expect(transport.mock.calls[1][0]).toBe('/api/conversations/1/messages?limit=50&offset=0')
  })

  it('AuthDisabled 503 surfaces as ApiError.errorCode === "AuthDisabled"', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockJson(503, {
        status: 'error',
        error_code: 'AuthDisabled',
        message: 'Authentication must be enabled to use persistent AI conversations.',
      }),
    )
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    try {
      await fetchConversations()
      throw new Error('expected throw')
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).errorCode).toBe('AuthDisabled')
      expect((err as ApiError).status).toBe(503)
    }
  })

  it('attaches Authorization: Bearer <token> when token is set', async () => {
    let captured: RequestInit | undefined
    const transport = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      captured = init
      return Promise.resolve(
        mockJson(200, { conversations: [], count: 0, limit: 50, offset: 0 }),
      )
    })
    configureApi({
      getToken: () => 't0k3n',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await fetchConversations()
    const headers = captured?.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer t0k3n')
  })

  it('analyzeViaREST POSTs the question with days and region', async () => {
    let captured: RequestInit | undefined
    const transport = vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      captured = init
      return Promise.resolve(
        mockJson(200, { status: 'SUCCESS', operation: 'analyze', answer: 'ok', grounding: {}, citations: [], warnings: [] }),
      )
    })
    configureApi({
      getToken: () => 't',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await analyzeViaREST(7, { question: 'Why?', region: 'us-east-1', days: 30 })
    expect(captured?.method).toBe('POST')
    expect(captured?.body).toBe(JSON.stringify({ question: 'Why?', region: 'us-east-1', days: 30 }))
  })
})
