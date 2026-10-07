// Phase 6A — Auth provider tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AuthProvider, useAuth, type AuthUser } from '../lib/auth'
import { ApiError, configureApi, __resetApiForTests } from '../lib/api'
import { Router, Routes } from '../lib/router'

const STORAGE_KEY = 'accd.session'

const sampleUser: AuthUser = {
  id: 1,
  email: 'admin@x.com',
  display_name: 'Admin',
  role: 'ADMIN',
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

function mockResponse(status: number, body: unknown): Response {
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
const authInfoDisabled = { ...authInfoEnabled, auth_enabled: false }

function Harness() {
  const api = useAuth()
  return (
    <div>
      <span data-testid="phase">{api.status.phase}</span>
      <span data-testid="role">{api.role ?? 'null'}</span>
      <span data-testid="authEnabled">{String(api.authEnabled)}</span>
      <span data-testid="user-email">{api.user?.email ?? 'null'}</span>
      <span data-testid="ready">{String(api.ready)}</span>
      <button data-testid="login" onClick={() => api.login('a@b.com', 'pw')}>
        login
      </button>
      <button data-testid="logout" onClick={() => api.logout()}>
        logout
      </button>
      {api.notice && <span data-testid="notice-kind">{api.notice.kind}</span>}
    </div>
  )
}

// Wraps the harness in a Router + AuthProvider in the correct order
// (Router outside AuthProvider — AuthProvider uses useNavigate).
// The Tree always renders the Harness at every known path so that
// the test can read the auth status regardless of which route the
// underlying browser history happens to point at.
function Tree({ extra }: { extra?: React.ReactNode }) {
  const HarnessOrExtra = () => <>{extra ?? <Harness />}</>
  return (
    <Router>
      <AuthProvider>
        <Routes
          routes={[
            { path: '/', element: <HarnessOrExtra /> },
            { path: '/login', element: <HarnessOrExtra /> },
            { path: '*', element: <HarnessOrExtra /> },
          ]}
        />
      </AuthProvider>
    </Router>
  )
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  __resetApiForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

describe('AuthProvider — bootstrap', () => {
  it('reports booting until /api/auth/info resolves', async () => {
    let resolveInfo!: (r: Response) => void
    const transport = vi.fn().mockImplementation(
      () => new Promise<Response>((res) => (resolveInfo = res)),
    )
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    render(<Tree />)
    expect(screen.getByTestId('phase').textContent).toBe('booting')

    await act(async () => {
      resolveInfo(mockResponse(200, authInfoDisabled))
    })
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('disabled'))
    expect(screen.getByTestId('authEnabled').textContent).toBe('false')
    expect(screen.getByTestId('role').textContent).toBe('ADMIN')
  })

  it('reports unauthenticated when auth_enabled=true and no stored session', async () => {
    const transport = vi.fn().mockResolvedValue(mockResponse(200, authInfoEnabled))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    render(<Tree />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('unauthenticated'))
    expect(screen.getByTestId('authEnabled').textContent).toBe('true')
    expect(screen.getByTestId('role').textContent).toBe('null')
  })

  it('reports authenticated when /api/auth/me succeeds against stored session', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, sampleUser)
      return mockResponse(404, { message: 'nf' })
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        access_token: 'good',
        expires_at: Date.now() + 60_000,
        user: sampleUser,
      }),
    )
    render(<Tree />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('authenticated'))
    expect(screen.getByTestId('role').textContent).toBe('ADMIN')
    expect(screen.getByTestId('user-email').textContent).toBe('admin@x.com')
  })

  it('drops a stored session that fails /api/auth/me with 401', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(401, { message: 'token expired' })
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        access_token: 'stale',
        expires_at: Date.now() + 60_000,
        user: sampleUser,
      }),
    )
    render(<Tree />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('unauthenticated'))
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Login / logout / errors
// ---------------------------------------------------------------------------

describe('AuthProvider — login', () => {
  it('persists the session and updates phase on success', async () => {
    const analystUser: AuthUser = { ...sampleUser, id: 2, email: 'analyst@x.com', display_name: 'Analyst', role: 'ANALYST' }
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') {
        return mockResponse(200, {
          access_token: 'fresh',
          token_type: 'bearer',
          expires_in: 3600,
          user: analystUser,
        })
      }
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    render(<Tree />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('unauthenticated'))
    await act(async () => {
      screen.getByTestId('login').click()
    })
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('authenticated'))
    expect(screen.getByTestId('role').textContent).toBe('ANALYST')
    const stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? 'null')
    expect(stored.access_token).toBe('fresh')
    expect(stored.user.role).toBe('ANALYST')
  })

  it('rejects with ApiError(Unauthorized) on 401', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') {
        return mockResponse(401, { error_code: 'InvalidCredentials', message: 'invalid credentials' })
      }
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    const LoginBtn = () => {
      const api = useAuth()
      return (
        <button
          data-testid="login-btn"
          onClick={async () => {
            try {
              await api.login('a@b.com', 'wrong')
            } catch (e) {
              ;(window as unknown as { __loginErr?: unknown }).__loginErr = e
            }
          }}
        >
          go
        </button>
      )
    }
    render(<Tree extra={<LoginBtn />} />)
    await waitFor(() => screen.getByTestId('login-btn'))
    await userEvent.setup().click(screen.getByTestId('login-btn'))
    await waitFor(() => {
      const err = (window as unknown as { __loginErr?: unknown }).__loginErr
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).code).toBe('Unauthorized')
    })
  })

  it('rejects with ApiError(Unavailable) on network failure', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') throw new TypeError('network down')
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    const LoginBtn = () => {
      const api = useAuth()
      return (
        <button
          data-testid="login-btn"
          onClick={async () => {
            try {
              await api.login('a@b.com', 'pw')
            } catch (e) {
              ;(window as unknown as { __loginErr?: unknown }).__loginErr = e
            }
          }}
        >
          go
        </button>
      )
    }
    render(<Tree extra={<LoginBtn />} />)
    await waitFor(() => screen.getByTestId('login-btn'))
    await userEvent.setup().click(screen.getByTestId('login-btn'))
    await waitFor(() => {
      const err = (window as unknown as { __loginErr?: unknown }).__loginErr
      expect(err).toBeInstanceOf(ApiError)
      expect((err as ApiError).code).toBe('Unavailable')
    })
  })
})

describe('AuthProvider — logout', () => {
  it('clears the session and redirects to /login', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(200, sampleUser)
      return mockResponse(200, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        access_token: 'good',
        expires_at: Date.now() + 60_000,
        user: sampleUser,
      }),
    )
    render(<Tree />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('authenticated'))
    await act(async () => {
      screen.getByTestId('logout').click()
    })
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('unauthenticated'))
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(window.location.pathname).toBe('/login')
  })
})

// ---------------------------------------------------------------------------
// Security
// ---------------------------------------------------------------------------

describe('AuthProvider — token handling', () => {
  it('does not include the bearer token in any error message', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/me') return mockResponse(401, { message: 'token expired' })
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => 'SECRET-TOKEN-DO-NOT-LOG',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    const Probe = () => {
      return (
        <button
          data-testid="call"
          onClick={async () => {
            const { apiFetch } = await import('../lib/api')
            try {
              await apiFetch('/api/auth/me')
            } catch (e) {
              ;(window as unknown as { __err?: unknown }).__err = e
            }
          }}
        >
          call
        </button>
      )
    }
    render(<Tree extra={<Probe />} />)
    await waitFor(() => screen.getByTestId('call'))
    await userEvent.setup().click(screen.getByTestId('call'))
    await waitFor(() => {
      const err = (window as unknown as { __err?: unknown }).__err
      expect(err).toBeDefined()
      const msg = String((err as Error).message ?? '')
      expect(msg).not.toContain('SECRET-TOKEN-DO-NOT-LOG')
    })
  })
})
