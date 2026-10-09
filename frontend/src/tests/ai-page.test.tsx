// Phase 6C — AIAnalystPage integration tests.
//
// Covers the major UI states end-to-end without depending on a live
// backend.  The transport is mocked so every fetch and WebSocket
// frame can be driven by the test.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, useAuth, __authTestInternals } from '../lib/auth'
import { Router, Routes, useLocation, useNavigate } from '../lib/router'
import { __resetApiForTests, configureApi } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { AIAnalystPage } from '../pages/AIAnalystPage'
import {
  __setWebSocketFactoryForTests as __setWS,
  __resetWebSocketFactoryForTests,
  type WebSocketLike,
  type ServerFrame,
} from '../lib/ai/connection'

// ---------------------------------------------------------------------------
// Mock WebSocket (per-test)
// ---------------------------------------------------------------------------

class MockWebSocket implements WebSocketLike {
  readyState = 0
  onopen: ((ev: unknown) => void) | null = null
  onclose: ((ev: { code: number; reason: string }) => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  sent: string[] = []
  url: string
  protocols?: string | string[]
  constructor(url: string, protocols?: string | string[]) {
    this.url = url
    this.protocols = protocols
  }
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
  __setWS((url, protocols) => {
    const s = new MockWebSocket(url, protocols)
    lastMock = s
    return s
  })
}

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const authInfoEnabled = {
  auth_enabled: true,
  issuer: 'i',
  audience: 'a',
  algorithm: 'HS256',
  access_token_minutes: 60,
}
const sampleUser = {
  id: 1,
  email: 'admin@example.com',
  display_name: 'Admin',
  role: 'ADMIN' as const,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

function writeStoredSession(token = 'header.payload.sig') {
  window.localStorage.setItem(
    __authTestInternals.SESSION_STORAGE_KEY,
    JSON.stringify({
      access_token: token,
      expires_at: Date.now() + 60_000,
      user: sampleUser,
    }),
  )
}

function buildHarness(handlers: {
  authInfo?: () => Response
  status?: () => Response
  conversationsList?: () => Response
  conversation?: (id: number) => Response
  messages?: (id: number) => Response
  create?: () => Response
}) {
  const transport = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    if (url === '/api/auth/info') return Promise.resolve(handlers.authInfo?.() ?? mockJson(200, authInfoEnabled))
    if (url === '/api/auth/me') {
      return Promise.resolve(
        mockJson(200, {
          id: 1,
          email: 'admin@example.com',
          display_name: 'Admin',
          role: 'ADMIN',
          is_active: true,
          created_at: '2024-01-01T00:00:00Z',
          updated_at: '2024-01-01T00:00:00Z',
          last_login_at: null,
        }),
      )
    }
    if (url === '/api/ai/status')
      return Promise.resolve(
        handlers.status?.() ??
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
    if (init?.method === 'POST' && url === '/api/conversations') {
      return Promise.resolve(
        handlers.create?.() ??
          mockJson(201, {
            id: 1,
            user_id: 1,
            title: 'New Cost Analysis',
            is_archived: false,
            created_at: '2026-01-01T00:00:00Z',
            updated_at: '2026-01-01T00:00:00Z',
            last_message_at: null,
          }),
      )
    }
    if (url === '/api/conversations?limit=100' || url === '/api/conversations?limit=200' || url === '/api/conversations')
      return Promise.resolve(
        handlers.conversationsList?.() ??
          mockJson(200, { conversations: [], count: 0, limit: 100, offset: 0 }),
      )
    const m1 = url.match(/^\/api\/conversations\/(\d+)$/)
    if (m1) {
      return Promise.resolve(
        handlers.conversation?.(Number(m1[1])) ??
          mockJson(200, {
            conversation: {
              id: Number(m1[1]),
              user_id: 1,
              title: 'Cost Q1',
              is_archived: false,
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-02T00:00:00Z',
              last_message_at: null,
            },
            message_count: 0,
          }),
      )
    }
    const m2 = url.match(/^\/api\/conversations\/(\d+)\/messages/)
    if (m2) {
      return Promise.resolve(
        handlers.messages?.(Number(m2[1])) ??
          mockJson(200, {
            messages: [
              {
                id: 1,
                conversation_id: Number(m2[1]),
                role: 'USER',
                content: 'Why did EC2 spend rise?',
                created_at: '2026-01-02T00:00:00Z',
              },
              {
                id: 2,
                conversation_id: Number(m2[1]),
                role: 'ASSISTANT',
                content: 'EC2 is the driver.',
                created_at: '2026-01-02T00:01:00Z',
                model_alias: 'cost-detective-free',
              },
            ],
            count: 2,
            limit: 200,
            offset: 0,
            has_more: false,
          }),
      )
    }
    return Promise.resolve(mockJson(404, { status: 'error', error_code: 'NotFound', message: 'Not found' }))
  })
  return transport
}

function App() {
  return (
    <Routes
      routes={[
        {
          path: '/analyst',
          element: <AIAnalystPage />,
          requiredRoles: ['ADMIN', 'ANALYST'],
        },
      ]}
      currentRole="ADMIN"
    />
  )
}

function renderAnalyst(handlers: Parameters<typeof buildHarness>[0] = {}) {
  writeStoredSession()
  const transport = buildHarness(handlers)
  configureApi({
    getToken: () => {
      try {
        const raw = window.localStorage.getItem(__authTestInternals.SESSION_STORAGE_KEY)
        if (!raw) return null
        const parsed = JSON.parse(raw) as { access_token: string; expires_at: number }
        if (parsed.expires_at <= Date.now()) return null
        return parsed.access_token
      } catch {
        return null
      }
    },
    onSessionExpired: () => undefined,
    transport: transport as unknown as typeof fetch,
  })
  installMock()
  return render(
    <ThemeProvider>
      <Router>
        <AuthProvider>
          <FinopsPeriodProvider initialDays={30}>
            <App />
          </FinopsPeriodProvider>
        </AuthProvider>
      </Router>
    </ThemeProvider>,
  )
}

beforeEach(() => {
  __resetApiForTests()
  invalidateCache()
  window.history.replaceState({}, '', '/analyst')
})
afterEach(() => {
  __resetApiForTests()
  __resetWebSocketFactoryForTests()
  lastMock = null
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('AIAnalystPage', () => {
  it('initial state: header, "New chat", empty right pane', async () => {
    renderAnalyst()
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'AI Cost Analyst', level: 1 })).toBeInTheDocument()
    })
    expect(screen.getAllByTestId('new-chat').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/start a grounded analysis/i).length).toBeGreaterThan(0)
  })

  it('auth-disabled state: renders AuthDisabledNotice', async () => {
    const transport = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/auth/info')
        return Promise.resolve(mockJson(200, { ...authInfoEnabled, auth_enabled: false }))
      if (url === '/api/ai/status')
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
      return Promise.resolve(mockJson(503, { status: 'error', error_code: 'AuthDisabled', message: 'x' }))
    })
    writeStoredSession()
    configureApi({
      getToken: () => 'header.payload.sig',
      onSessionExpired: () => undefined,
      transport: transport as unknown as typeof fetch,
    })
    installMock()
    render(
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <FinopsPeriodProvider initialDays={30}>
              <App />
            </FinopsPeriodProvider>
          </AuthProvider>
        </Router>
      </ThemeProvider>,
    )
    await waitFor(() => {
      expect(screen.getByText(/Authentication must be enabled/i)).toBeInTheDocument()
    })
  })

  it('ai-disabled state: renders the disabled banner', async () => {
    renderAnalyst({
      status: () =>
        mockJson(200, {
          status: 'DISABLED',
          ai_enabled: false,
          litellm_reachable: false,
          model_alias: 'cost-detective-free',
          litellm_base_url: 'http://litellm:4000',
          timeout_seconds: 30,
          max_output_tokens: 800,
          context_limits: {},
          message: 'AI_DISABLED',
        }),
    })
    await waitFor(() => {
      expect(screen.getByText(/AI analysis is unavailable/i)).toBeInTheDocument()
    })
  })

  it('ADMIN happy path: open conversation, send question, receive assistant answer', async () => {
    renderAnalyst()
    await waitFor(() => expect(screen.getAllByTestId('new-chat').length).toBeGreaterThan(0))
    await userEvent.setup().click(screen.getAllByTestId('new-chat')[0])

    // WS opens for the new conversation.
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

    // Send a question via the composer.
    const ta = (await screen.findByTestId('composer-textarea')) as HTMLTextAreaElement
    const user = userEvent.setup()
    await user.type(ta, 'Why did EC2 spend rise?')
    await user.keyboard('{Enter}')

    const frame = JSON.parse(m.sent[0])
    act(() =>
      m.__test__receive({
        type: 'ai_processing',
        request_id: frame.request_id,
        conversation_id: 1,
      }),
    )
    act(() =>
      m.__test__receive({
        type: 'assistant_message',
        request_id: frame.request_id,
        conversation_id: 1,
        message_id: 1,
        operation: 'analyze',
        model: 'cost-detective-free',
        answer: 'EC2 is the driver.',
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
    await waitFor(() => {
      expect(screen.getByTestId('evidence-panel')).toBeInTheDocument()
    })
  })

  it('duplicate submit while inflight is blocked', async () => {
    renderAnalyst()
    await waitFor(() => expect(screen.getAllByTestId('new-chat').length).toBeGreaterThan(0))
    await userEvent.setup().click(screen.getAllByTestId('new-chat')[0])
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
    const ta = await screen.findByTestId('composer-textarea')
    const user = userEvent.setup()
    await user.type(ta, 'first')
    await user.keyboard('{Enter}')
    // Only one frame sent so far:
    expect(m.sent.length).toBe(1)
    // Without producing a terminal frame, the second send must be
    // blocked: the textarea stays focused but the Send button is
    // disabled while `inflight` is true.
    await waitFor(() =>
      expect((screen.getByTestId('composer-send') as HTMLButtonElement).disabled).toBe(true),
    )
  })

  it('disconnect shows the Reconnect button', async () => {
    renderAnalyst()
    await waitFor(() => expect(screen.getAllByTestId('new-chat').length).toBeGreaterThan(0))
    await userEvent.setup().click(screen.getAllByTestId('new-chat')[0])
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
    act(() => m.__test__close(1006, 'abnormal'))
    // The hook auto-reconnects ONCE on retryable close codes.  The new
    // socket overwrites `lastMock`.  Wait for that, then close the
    // auto-reconnected socket too — only then does the UI land in
    // `retryable_failure` and render the manual Reconnect button.
    await waitFor(() => expect(lastMock).not.toBe(m))
    const m2 = lastMock as MockWebSocket
    act(() => m2.__test__close(1006, 'abnormal'))
    await waitFor(() => {
      expect(screen.getByTestId('reconnect-button')).toBeInTheDocument()
    })
  })

  it('VIEWER path: route guard renders AccessDenied', async () => {
    writeStoredSession()
    const transport = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/auth/info')
        return Promise.resolve(mockJson(200, authInfoEnabled))
      if (url === '/api/auth/me')
        return Promise.resolve(
          mockJson(200, { ...sampleUser, role: 'VIEWER' }),
        )
      return Promise.resolve(mockJson(200, {}))
    })
    configureApi({
      getToken: () => 'header.payload.sig',
      onSessionExpired: () => undefined,
      transport: transport as unknown as typeof fetch,
    })
    installMock()
    function ViewerApp() {
      return (
        <Routes
          routes={[
            {
              path: '/analyst',
              element: <AIAnalystPage />,
              requiredRoles: ['ADMIN', 'ANALYST'],
            },
          ]}
          currentRole="VIEWER"
        />
      )
    }
    render(
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <FinopsPeriodProvider initialDays={30}>
              <ViewerApp />
            </FinopsPeriodProvider>
          </AuthProvider>
        </Router>
      </ThemeProvider>,
    )
    // The router's inline AccessDenied renders for VIEWER.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /access denied/i, level: 1 })).toBeInTheDocument()
    })
  })
})
