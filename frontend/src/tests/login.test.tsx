// Phase 6A — Login page tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, useAuth } from '../lib/auth'
import { Router, Routes } from '../lib/router'
import { LoginPage } from '../pages/LoginPage'
import { ApiError, configureApi, __resetApiForTests } from '../lib/api'

function mockResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function wrap(node: React.ReactNode) {
  return render(
    <ThemeProvider>
      <Router>
        <Routes routes={[{ path: '/login', element: node }]} />
      </Router>
    </ThemeProvider>,
  )
}

const authInfoEnabled = {
  auth_enabled: true,
  issuer: 'i',
  audience: 'a',
  algorithm: 'HS256',
  access_token_minutes: 60,
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState({}, '', '/login')
  __resetApiForTests()
})
afterEach(() => {
  vi.restoreAllMocks()
})

// Helper: render with the AuthProvider in place.
function renderWithAuth(extra: React.ReactNode = null) {
  return render(
    <ThemeProvider>
      <Router>
        <AuthProvider>
          <Routes
            routes={[
              { path: '/login', element: <LoginPage /> },
              { path: '/', element: <div data-testid="dashboard">dashboard</div> },
            ]}
          />
          {extra}
        </AuthProvider>
      </Router>
    </ThemeProvider>,
  )
}

describe('LoginPage', () => {
  it('renders email + password inputs and a sign-in button', async () => {
    const transport = vi.fn().mockResolvedValue(mockResponse(200, authInfoEnabled))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
    expect(screen.getByTestId('login-submit')).toBeInTheDocument()
  })

  it('shows a generic Invalid email or password message on 401', async () => {
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
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/email/i), 'a@b.com')
    await user.type(screen.getByLabelText(/password/i), 'wrong')
    await user.click(screen.getByTestId('login-submit'))
    await waitFor(() => screen.getByTestId('login-error'))
    expect(screen.getByTestId('login-error').textContent).toMatch(/invalid email or password/i)
  })

  it('shows a backend-unavailable message on network failure', async () => {
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') throw new TypeError('network')
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/email/i), 'a@b.com')
    await user.type(screen.getByLabelText(/password/i), 'whatever')
    await user.click(screen.getByTestId('login-submit'))
    await waitFor(() => screen.getByTestId('login-error'))
    expect(screen.getByTestId('login-error').textContent).toMatch(/backend unavailable/i)
  })

  it('disables the submit button while in flight', async () => {
    let resolveLogin!: (r: Response) => void
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') {
        return new Promise<Response>((res) => (resolveLogin = res))
      }
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/email/i), 'a@b.com')
    await user.type(screen.getByLabelText(/password/i), 'pw')
    await user.click(screen.getByTestId('login-submit'))
    await waitFor(() => expect(screen.getByTestId('login-submit')).toBeDisabled())
    await act(async () => {
      resolveLogin(
        mockResponse(200, {
          access_token: 't',
          token_type: 'bearer',
          expires_in: 60,
          user: {
            id: 1,
            email: 'a@b.com',
            display_name: 'A',
            role: 'ADMIN',
            is_active: true,
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T00:00:00Z',
            last_login_at: null,
          },
        }),
      )
    })
    await waitFor(() => expect(screen.getByTestId('dashboard')).toBeInTheDocument())
  })

  it('renders the theme toggle in the corner of the login card', async () => {
    const transport = vi.fn().mockResolvedValue(mockResponse(200, authInfoEnabled))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    expect(screen.getByTestId('theme-toggle')).toBeInTheDocument()
  })

  it('does not log the password (no console.error / warn path)', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const spyError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const spyWarn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
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
    renderWithAuth()
    await waitFor(() => screen.getByTestId('login-card'))
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/email/i), 'a@b.com')
    await user.type(screen.getByLabelText(/password/i), 'MY-SECRET-PW')
    await user.click(screen.getByTestId('login-submit'))
    await waitFor(() => screen.getByTestId('login-error'))
    for (const s of [spy, spyError, spyWarn]) {
      s.mock.calls.forEach((call) => {
        expect(JSON.stringify(call)).not.toContain('MY-SECRET-PW')
      })
    }
    spy.mockRestore()
    spyError.mockRestore()
    spyWarn.mockRestore()
  })
})

// Smoke test that the AuthProvider also redirects /login → / when
// the user is already signed in.
describe('LoginPage — auth-disabled redirect', () => {
  it('redirects to / when the backend reports AUTH_ENABLED=false', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockResponse(200, { ...authInfoEnabled, auth_enabled: false }),
    )
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    renderWithAuth()
    // The LoginPage effect fires a navigate('/') once the AuthProvider
    // resolves the disabled state.  The / route is registered with a
    // testid, so we wait for it.
    await waitFor(() => expect(window.location.pathname).toBe('/'))
  })
})

// Token-never-logged sanity check via the auth provider's own hook.
describe('AuthProvider — never logs password', () => {
  it('does not log the password in any error path', async () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const spyError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const spyWarn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const transport = vi.fn().mockImplementation(async (path: string) => {
      if (path === '/api/auth/info') return mockResponse(200, authInfoEnabled)
      if (path === '/api/auth/login') {
        return mockResponse(500, { message: 'internal' })
      }
      return mockResponse(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    function Probe() {
      const api = useAuth()
      return (
        <button
          data-testid="go"
          onClick={async () => {
            try {
              await api.login('a@b.com', 'PASSWORD-DO-NOT-LOG')
            } catch (e) {
              expect(String((e as Error).message)).not.toContain('PASSWORD-DO-NOT-LOG')
            }
          }}
        >
          go
        </button>
      )
    }
    render(
      <ThemeProvider>
        <Router>
          <AuthProvider>
            <Routes routes={[{ path: '*', element: <Probe /> }]} />
          </AuthProvider>
        </Router>
      </ThemeProvider>,
    )
    await waitFor(() => screen.getByTestId('go'))
    await userEvent.setup().click(screen.getByTestId('go'))
    await waitFor(() => {
      for (const s of [spy, spyError, spyWarn]) {
        s.mock.calls.forEach((call) => {
          expect(JSON.stringify(call)).not.toContain('PASSWORD-DO-NOT-LOG')
        })
      }
    })
    spy.mockRestore()
    spyError.mockRestore()
    spyWarn.mockRestore()
  })
})
