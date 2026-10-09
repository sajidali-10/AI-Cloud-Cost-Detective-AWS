// Phase 6C — ConversationsPage integration tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, __authTestInternals } from '../lib/auth'
import { Router, Routes, useNavigate } from '../lib/router'
import { __resetApiForTests, configureApi } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { ConversationsPage } from '../pages/ConversationsPage'

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

function writeStoredSession() {
  window.localStorage.setItem(
    __authTestInternals.SESSION_STORAGE_KEY,
    JSON.stringify({
      access_token: 'header.payload.sig',
      expires_at: Date.now() + 60_000,
      user: sampleUser,
    }),
  )
}

function buildTransport(handlers: {
  authInfo?: () => Response
  status?: () => Response
  list?: () => Response
}) {
  return vi.fn().mockImplementation((url: string) => {
    if (url === '/api/auth/info')
      return Promise.resolve(handlers.authInfo?.() ?? mockJson(200, authInfoEnabled))
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
    if (url === '/api/conversations' || url.startsWith('/api/conversations?'))
      return Promise.resolve(
        handlers.list?.() ??
          mockJson(200, { conversations: [], count: 0, limit: 100, offset: 0 }),
      )
    return Promise.resolve(mockJson(404, { status: 'error', error_code: 'NotFound', message: 'x' }))
  })
}

function App() {
  return <Routes routes={[{ path: '/conversations', element: <ConversationsPage /> }]} currentRole="ADMIN" />
}

function renderConversationsPage(handlers: Parameters<typeof buildTransport>[0] = {}) {
  writeStoredSession()
  const transport = buildTransport(handlers)
  configureApi({
    getToken: () => 'header.payload.sig',
    onSessionExpired: () => undefined,
    transport: transport as unknown as typeof fetch,
  })
  window.history.replaceState({}, '', '/conversations')
  return render(
    <ThemeProvider>
      <Router>
        <AuthProvider>
          <App />
        </AuthProvider>
      </Router>
    </ThemeProvider>,
  )
}

beforeEach(() => {
  __resetApiForTests()
  invalidateCache()
})
afterEach(() => {
  __resetApiForTests()
  vi.restoreAllMocks()
})

describe('ConversationsPage', () => {
  it('renders the empty state when no conversations exist', async () => {
    renderConversationsPage()
    await waitFor(() => {
      expect(screen.getByText(/No conversations yet/i)).toBeInTheDocument()
    })
  })

  it('renders a failure state on backend error', async () => {
    renderConversationsPage({
      list: () =>
        mockJson(500, { status: 'error', error_code: 'Error', message: 'boom' }),
    })
    await waitFor(() => {
      expect(screen.getByText(/Could not load conversations/i)).toBeInTheDocument()
    })
  })

  it('renders AuthDisabled notice when backend returns 503 AuthDisabled', async () => {
    renderConversationsPage({
      list: () =>
        mockJson(503, {
          status: 'error',
          error_code: 'AuthDisabled',
          message: 'Authentication must be enabled.',
        }),
    })
    await waitFor(() => {
      expect(screen.getByText(/Authentication must be enabled/i)).toBeInTheDocument()
    })
  })

  it('renders the loading skeleton during fetch', async () => {
    let resolveList: (v: Response) => void = () => undefined
    const list = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveList = resolve
        }),
    )
    const transport = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/conversations' || url.startsWith('/api/conversations?'))
        return list()
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
      return Promise.resolve(mockJson(200, authInfoEnabled))
    })
    writeStoredSession()
    configureApi({
      getToken: () => 'header.payload.sig',
      onSessionExpired: () => undefined,
      transport: transport as unknown as typeof fetch,
    })
    window.history.replaceState({}, '', '/conversations')
    render(
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <App />
          </AuthProvider>
        </Router>
      </ThemeProvider>,
    )
    // Skeletons are visible while the list is in flight.
    await waitFor(() => {
      expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
    })
    // Resolve the list so cleanup doesn't hang.
    await waitFor(() => {
      resolveList(mockJson(200, { conversations: [], count: 0, limit: 100, offset: 0 }))
    })
  })

  it('clicking a row navigates to /analyst?conversation={id}', async () => {
    renderConversationsPage({
      list: () =>
        mockJson(200, {
          conversations: [
            {
              id: 7,
              user_id: 1,
              title: 'My chat',
              is_archived: false,
              created_at: '2026-01-01T00:00:00Z',
              updated_at: '2026-01-02T00:00:00Z',
              last_message_at: '2026-01-02T00:00:00Z',
            },
          ],
          count: 1,
          limit: 100,
          offset: 0,
        }),
    })
    await waitFor(() => {
      expect(screen.getByTestId('open-conversation-7')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId('open-conversation-7'))
    await waitFor(() => {
      expect(window.location.pathname).toBe('/analyst')
      expect(window.location.search).toBe('?conversation=7')
    })
  })
})
