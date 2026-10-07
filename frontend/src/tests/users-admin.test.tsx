// Phase 6A — Users admin page tests.
//
// Covers:
//   - ADMIN can list / create / patch / toggle
//   - non-ADMIN sees AccessDenied
//   - actions persist via the Phase 5A admin endpoints
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, type AuthUser } from '../lib/auth'
import { Router, Routes } from '../lib/router'
import { UsersPage } from '../pages/UsersPage'
import { configureApi, __resetApiForTests } from '../lib/api'

const adminUser: AuthUser = {
  id: 1,
  email: 'admin@x.com',
  display_name: 'Admin',
  role: 'ADMIN',
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

const analystUser: AuthUser = { ...adminUser, id: 2, email: 'analyst@x.com', display_name: 'Analyst', role: 'ANALYST' }

const authInfoEnabled = {
  auth_enabled: true,
  issuer: 'i',
  audience: 'a',
  algorithm: 'HS256',
  access_token_minutes: 60,
}

function mockResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function seedSession(user: AuthUser) {
  window.localStorage.setItem(
    'accd.session',
    JSON.stringify({
      access_token: 'good',
      expires_at: Date.now() + 60_000,
      user,
    }),
  )
}

function renderUsers(extraRoutes: { path: string; element: React.ReactNode }[] = []) {
  return render(
    <ThemeProvider>
      <Router>
        <AuthProvider>
          <Routes
            routes={[
              { path: '/users', element: <UsersPage /> },
              { path: '/', element: <div>home</div> },
              { path: '/login', element: <div>login</div> },
              ...extraRoutes,
            ]}
          />
        </AuthProvider>
      </Router>
    </ThemeProvider>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState({}, '', '/users')
  __resetApiForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('UsersPage — access control', () => {
  it('non-admin sees AccessDenied', async () => {
    seedSession(analystUser)
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, analystUser)
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderUsers()
    await waitFor(() => screen.getByText(/access denied/i))
    expect(screen.getByText(/only administrators/i)).toBeInTheDocument()
  })

  it('admin can load the user list and see the create button', async () => {
    seedSession(adminUser)
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, adminUser)
      if (path === '/api/admin/users') {
        return mockResponse(200, {
          users: [
            {
              id: 1,
              email: 'admin@x.com',
              display_name: 'Admin',
              role: 'ADMIN',
              is_active: true,
              created_at: '2024-01-01T00:00:00Z',
              updated_at: '2024-01-01T00:00:00Z',
              last_login_at: null,
            },
            {
              id: 2,
              email: 'analyst@x.com',
              display_name: 'Analyst',
              role: 'ANALYST',
              is_active: true,
              created_at: '2024-01-02T00:00:00Z',
              updated_at: '2024-01-02T00:00:00Z',
              last_login_at: '2024-01-03T00:00:00Z',
            },
          ],
          count: 2,
        })
      }
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderUsers()
    await waitFor(() => screen.getByTestId('users-create-button'))
    await waitFor(() => screen.getByText('admin@x.com'))
    expect(screen.getByText('analyst@x.com')).toBeInTheDocument()
    expect(screen.getAllByText('Administrator').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Analyst').length).toBeGreaterThan(0)
  })
})

describe('UsersPage — create user', () => {
  it('creates a user via POST /api/admin/users', async () => {
    seedSession(adminUser)
    let postCalled = false
    const transport = vi.fn().mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, adminUser)
      if (path === '/api/admin/users' && (!init?.method || init.method === 'GET')) {
        return mockResponse(200, { users: [adminUser], count: 1 })
      }
      if (path === '/api/admin/users' && init?.method === 'POST') {
        postCalled = true
        const body = JSON.parse(String(init.body))
        expect(body.email).toBe('new@x.com')
        expect(body.role).toBe('ANALYST')
        return mockResponse(201, {
          id: 3,
          email: 'new@x.com',
          display_name: 'New',
          role: 'ANALYST',
          is_active: true,
          created_at: '2024-01-04T00:00:00Z',
          updated_at: '2024-01-04T00:00:00Z',
          last_login_at: null,
        })
      }
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderUsers()
    await waitFor(() => screen.getByTestId('users-create-button'))
    const user = userEvent.setup()
    await user.click(screen.getByTestId('users-create-button'))
    await waitFor(() => screen.getByTestId('create-email'))
    await user.type(screen.getByTestId('create-email'), 'new@x.com')
    await user.type(screen.getByTestId('create-display-name'), 'New')
    await user.type(screen.getByTestId('create-password'), 'good-password-123')
    await user.selectOptions(screen.getByTestId('create-role'), 'ANALYST')
    await user.click(screen.getByTestId('create-submit'))
    await waitFor(() => expect(postCalled).toBe(true))
  })
})

describe('UsersPage — deactivate / reactivate', () => {
  it('PATCHes /api/admin/users/{id} when deactivate is confirmed', async () => {
    seedSession(adminUser)
    const other: AuthUser = { ...adminUser, id: 99, email: 'other@x.com', display_name: 'Other', role: 'VIEWER' }
    let patched = false
    const transport = vi.fn().mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, adminUser)
      if (path === '/api/admin/users' && (!init?.method || init.method === 'GET')) {
        return mockResponse(200, { users: [adminUser, other], count: 2 })
      }
      if (path === '/api/admin/users/99' && init?.method === 'PATCH') {
        patched = true
        const body = JSON.parse(String(init.body))
        expect(body.is_active).toBe(false)
        return mockResponse(200, { ...other, is_active: false })
      }
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderUsers()
    await waitFor(() => screen.getByTestId('users-toggle-99'))
    const user = userEvent.setup()
    await user.click(screen.getByTestId('users-toggle-99'))
    const dialog = await screen.findByRole('dialog', { name: /deactivate user\?/i })
    await waitFor(() => expect(dialog).toBeInTheDocument())
    // Confirm via the dialog's primary button (Deactivate).
    const confirmBtn = dialog.querySelector('button.bg-warning') as HTMLButtonElement
    expect(confirmBtn).toBeInTheDocument()
    await user.click(confirmBtn)
    await waitFor(() => expect(patched).toBe(true))
  })

  it('cannot deactivate the currently signed-in admin', async () => {
    seedSession(adminUser)
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, adminUser)
      if (path === '/api/admin/users') {
        return mockResponse(200, { users: [adminUser], count: 1 })
      }
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderUsers()
    await waitFor(() => screen.getByTestId('users-toggle-1'))
    expect(screen.getByTestId('users-toggle-1')).toBeDisabled()
  })
})
