// Phase 6C.1 — Integration tests for the runtime/UX closure.
//
// Specifically locks the conversation-availability state machine
// surfaced by the Phase 6C.1 dev-runtime changes:
//
//   - empty state: "No conversations yet" only when the backend
//     genuinely returns an empty list
//   - auth-disabled state: a 503 AuthDisabled surfaces as the
//     AuthDisabled notice (NOT the empty placeholder)
//   - failure state: any 500 surfaces as ErrorState (NOT the
//     empty placeholder)
//
// The dashboard hero is locked by dashboard-hero.test.tsx and the
// full page rendering by Phase 6B's dashboard.test.tsx; this file
// does NOT re-test them at the page level to keep the response-
// shape coupling narrow.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, __authTestInternals } from '../lib/auth'
import { Router, Routes } from '../lib/router'
import { __resetApiForTests, configureApi } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { ConversationsPage } from '../pages/ConversationsPage'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
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

beforeEach(() => {
  __resetApiForTests()
  invalidateCache()
  window.history.replaceState({}, '', '/conversations')
})
afterEach(() => {
  __resetApiForTests()
  vi.restoreAllMocks()
})

describe('ConversationsPage availability states', () => {
  function mount(handlers: { list?: () => Response; authInfo?: Response }) {
    writeStoredSession()
    const transport = vi.fn().mockImplementation((url: string) => {
      if (url === '/api/auth/info')
        return Promise.resolve(handlers.authInfo ?? mockJson(200, { auth_enabled: true }))
      if (url === '/api/auth/me') return Promise.resolve(mockJson(200, sampleUser))
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
      if (url === '/api/conversations' || url.startsWith('/api/conversations?'))
        return Promise.resolve(
          handlers.list?.() ??
            mockJson(200, { conversations: [], count: 0, limit: 200, offset: 0 }),
        )
      return Promise.resolve(mockJson(404, {}))
    })
    configureApi({
      getToken: () => 'header.payload.sig',
      onSessionExpired: () => undefined,
      transport: transport as unknown as typeof fetch,
    })
    render(
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <Routes
              routes={[{ path: '/conversations', element: <ConversationsPage /> }]}
              currentRole="ADMIN"
            />
          </AuthProvider>
        </Router>
      </ThemeProvider>,
    )
  }

  it('empty state — "No conversations yet" only when list is genuinely empty', async () => {
    mount({})
    await waitFor(() => {
      expect(screen.getByText(/No conversations yet/i)).toBeInTheDocument()
    })
  })

  it('auth-disabled state — renders AuthDisabled notice (distinct from empty)', async () => {
    mount({
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
    expect(screen.queryByText(/No conversations yet/i)).not.toBeInTheDocument()
  })

  it('failure state — renders ErrorState, never the empty placeholder', async () => {
    mount({
      list: () =>
        mockJson(500, { status: 'error', error_code: 'Error', message: 'boom' }),
    })
    await waitFor(() => {
      expect(screen.getByText(/Could not load conversations/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/No conversations yet/i)).not.toBeInTheDocument()
  })

  it('auth-disabled backend info: AUTH_ENABLED=false surfaces the notice', async () => {
    mount({ authInfo: mockJson(200, { auth_enabled: false }) })
    await waitFor(() => {
      expect(screen.getByText(/Authentication must be enabled/i)).toBeInTheDocument()
    })
  })
})
