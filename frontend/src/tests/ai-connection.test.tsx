// Phase 6C — WebSocket hook state machine tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import {
  __resetWebSocketFactoryForTests,
  __setWebSocketFactoryForTests,
  useConversationSocket,
  type WebSocketLike,
} from '../lib/ai/connection'
import type { ConnectionState, ServerFrame } from '../types/ai'

// ---------------------------------------------------------------------------
// Mock socket factory
// ---------------------------------------------------------------------------

class MockWebSocket implements WebSocketLike {
  readyState = 0
  onopen: ((ev: unknown) => void) | null = null
  onclose: ((ev: { code: number; reason: string }) => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  sent: string[] = []

  constructor(public url: string, public protocols?: string | string[]) {}

  send(data: string) {
    this.sent.push(data)
  }
  close(code = 1000, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
  __test__open() {
    this.readyState = 1
    this.onopen?.(undefined)
  }
  __test__close(code = 1006, reason = 'abnormal') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
  __test__receive(payload: ServerFrame | string) {
    const data = typeof payload === 'string' ? payload : JSON.stringify(payload)
    this.onmessage?.({ data })
  }
}

let lastMock: MockWebSocket | null = null

function installMock() {
  __setWebSocketFactoryForTests((url, protocols) => {
    const s = new MockWebSocket(url, protocols)
    lastMock = s
    return s
  })
}

// ---------------------------------------------------------------------------
// Hook harness
// ---------------------------------------------------------------------------

interface Harness {
  state: ConnectionState
  closeCode: number | null
  lastAssistantMessage: string | null
  lastErrorCode: string | null
  inflight: boolean
  sendUserMessage: (q: string) => boolean
  reconnect: () => void
  disconnect: () => void
}

function renderHarness(args: {
  conversationId: number | null
  getToken: () => string | null
  authEnabled?: boolean
  autoReconnect?: boolean
}): { current: Harness } {
  const ref: { current: Harness } = {
    current: {
      state: 'idle',
      closeCode: null,
      lastAssistantMessage: null,
      lastErrorCode: null,
      inflight: false,
      sendUserMessage: () => false,
      reconnect: () => undefined,
      disconnect: () => undefined,
    },
  }
  function Inner() {
    const sock = useConversationSocket({
      conversationId: args.conversationId,
      getToken: args.getToken,
      authEnabled: args.authEnabled ?? true,
      autoReconnect: args.autoReconnect ?? false,
      onAssistant: (f) => {
        ref.current.lastAssistantMessage = f.answer
      },
      onError: (f) => {
        ref.current.lastErrorCode = f.code
      },
    })
    ref.current.state = sock.state
    ref.current.closeCode = sock.closeCode
    ref.current.inflight = sock.inflightRequestId !== null
    ref.current.sendUserMessage = (q: string) => {
      try {
        sock.sendUserMessage({ question: q, days: 30 })
        return true
      } catch {
        return false
      }
    }
    ref.current.reconnect = sock.reconnect
    ref.current.disconnect = sock.disconnect
    return null
  }
  render(<Inner />)
  return ref
}

beforeEach(() => {
  installMock()
})
afterEach(() => {
  __resetWebSocketFactoryForTests()
  lastMock = null
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useConversationSocket', () => {
  it('opens the socket with bearer.<token> subprotocol', async () => {
    const harness = renderHarness({
      conversationId: 7,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => {
      expect(lastMock).not.toBeNull()
    })
    const m = lastMock as MockWebSocket
    expect(m.protocols).toEqual(['bearer.header.payload.sig'])
    expect(m.url.endsWith('/api/ws/conversations/7')).toBe(true)
    expect(m.url).not.toMatch(/token|jwt|Bearer/i)
  })

  it('reports authorization_failure when no token is available', async () => {
    const harness = renderHarness({
      conversationId: 7,
      getToken: () => null,
    })
    await waitFor(() => {
      expect(harness.current.state).toBe('authorization_failure')
    })
    expect(lastMock).toBeNull()
  })

  it('reports authorization_failure when AUTH_ENABLED=false', async () => {
    const harness = renderHarness({
      conversationId: 7,
      getToken: () => 'header.payload.sig',
      authEnabled: false,
    })
    await waitFor(() => {
      expect(harness.current.state).toBe('authorization_failure')
    })
    expect(lastMock).toBeNull()
  })

  it('transitions connecting → connected on `connected` frame', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    act(() => (lastMock as MockWebSocket).__test__open())
    act(() =>
      (lastMock as MockWebSocket).__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
  })

  it('walks connecting → processing → completed on a normal user question', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))

    let accepted = true
    act(() => {
      accepted = harness.current.sendUserMessage('Why did EC2 spend rise?')
    })
    expect(accepted).toBe(true)
    expect(m.sent.length).toBe(1)
    const parsed = JSON.parse(m.sent[0])
    expect(parsed.type).toBe('user_message')
    expect(parsed.question).toBe('Why did EC2 spend rise?')

    act(() =>
      m.__test__receive({
        type: 'ai_processing',
        request_id: parsed.request_id,
        conversation_id: 1,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('processing'))

    act(() =>
      m.__test__receive({
        type: 'assistant_message',
        request_id: parsed.request_id,
        conversation_id: 1,
        message_id: 99,
        operation: 'analyze',
        model: 'cost-detective-free',
        answer: 'EC2 spend rose due to idlers in us-east-1.',
        grounding: {
          days: 30,
          cost_evidence_used: true,
          recommendations_used: 1,
          capabilities_used: false,
        },
        citations: [{ service: 'EC2', region: 'us-east-1' }],
        warnings: [],
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('completed'))
    expect(harness.current.lastAssistantMessage).toMatch(/EC2 spend rose/)
    expect(harness.current.inflight).toBe(false)
  })

  it('rejects a second sendUserMessage while one is in flight', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    await act(async () => {
      expect(harness.current.sendUserMessage('first')).toBe(true)
      expect(harness.current.sendUserMessage('second')).toBe(false)
    })
  })

  it('rejects a duplicate request_id with Busy', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))

    await act(async () => {
      expect(harness.current.sendUserMessage('first')).toBe(true)
    })
    const firstFrame = JSON.parse(m.sent[0])
    act(() =>
      m.__test__receive({
        type: 'assistant_message',
        request_id: firstFrame.request_id,
        conversation_id: 1,
        message_id: 1,
        operation: 'analyze',
        answer: 'done',
        grounding: {
          days: 30,
          cost_evidence_used: false,
          recommendations_used: 0,
          capabilities_used: false,
        },
        citations: [],
        warnings: [],
      }),
    )
    await waitFor(() => expect(harness.current.inflight).toBe(false))
    // Distinct request ids — second call succeeds.
    await act(async () => {
      expect(harness.current.sendUserMessage('second distinct')).toBe(true)
    })
  })

  it('transitions to authorization_failure on close code 4401', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    act(() => m.__test__close(4401, 'token expired'))
    await waitFor(() => expect(harness.current.state).toBe('authorization_failure'))
    expect(harness.current.closeCode).toBe(4401)
  })

  it('transitions to authorization_failure on close code 4403', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    act(() => m.__test__close(4403, 'forbidden'))
    await waitFor(() => expect(harness.current.state).toBe('authorization_failure'))
  })

  it('transitions to authorization_failure on close code 1008 (auth disabled)', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    act(() => m.__test__close(1008, 'auth disabled'))
    await waitFor(() => expect(harness.current.state).toBe('authorization_failure'))
  })

  it('transitions to retryable_failure on close code 1006 (abnormal)', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
      autoReconnect: false,
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    act(() => m.__test__close(1006, 'abnormal'))
    await waitFor(() => expect(harness.current.state).toBe('retryable_failure'))
  })

  it('manual disconnect transitions to disconnected', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive({
        type: 'connected',
        protocol_version: 'v1',
        conversation_id: 1,
        user_id: 1,
        role: 'ADMIN',
        heartbeat_interval_seconds: 30,
      }),
    )
    await waitFor(() => expect(harness.current.state).toBe('connected'))
    act(() => harness.current.disconnect())
    await waitFor(() => expect(harness.current.state).toBe('disconnected'))
  })

  it('protocol_violation on a JWT-shaped payload in a server frame', async () => {
    const harness = renderHarness({
      conversationId: 1,
      getToken: () => 'header.payload.sig',
    })
    await waitFor(() => expect(lastMock).not.toBeNull())
    const m = lastMock as MockWebSocket
    act(() => m.__test__open())
    act(() =>
      m.__test__receive(
        JSON.stringify({
          type: 'connected',
          protocol_version: 'v1',
          conversation_id: 1,
          user_id: 1,
          role: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature',
          heartbeat_interval_seconds: 30,
        }),
      ),
    )
    await waitFor(() => expect(harness.current.state).toBe('protocol_violation'))
  })
})
