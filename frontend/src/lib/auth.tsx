// Phase 6A — Auth provider.
//
// Lifecycle:
//   1. On mount, call GET /api/auth/info (anonymous) to learn
//      `auth_enabled` from the backend.  Until this resolves the
//      app is in a "booting" state.
//   2. If `auth_enabled=false` → the app is open.  A synthetic
//      "anonymous" user is exposed so role-aware UI keeps working
//      but is documented as a development-only mode (matches the
//      backend Phase 5A contract).
//   3. If `auth_enabled=true` and no stored session → render the
//      login route.
//   4. If `auth_enabled=true` and a stored session exists → verify
//      it with GET /api/auth/me.  On 401 drop the session and show
//      login.  On success, expose the user via context.
//
// Token storage: localStorage under 'accd.session'.  The bearer
// token is the only credential material; it is short-lived (60 min
// per Phase 5A) and the backend re-validates against the DB row on
// every request, so even if the token leaks from localStorage the
// blast radius is bounded to that single user's permissions until
// expiry.  A hardened deployment should prefer httpOnly Secure
// SameSite cookies set by the backend; that is a backend change
// deferred beyond Phase 6A.
//
// Security invariants enforced here:
//   * The bearer token is NEVER logged or echoed in errors.
//   * The password is NEVER stored or echoed.
//   * 401 responses automatically drop the session and surface a
//     "session expired" notification (not a silent re-login).
//   * 401 does NOT cause a redirect loop — at most once per session
//     lifetime, controlled by `sessionExpiredOnce` ref.
//   * Logout clears local storage and forces a re-bootstrap.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError, apiFetch, configureApi } from './api'
import { useNavigate } from './router'
import type { Role } from './tokens'

// ---------------------------------------------------------------------------
// Types — mirror backend `PublicUserView` / `LoginResponse` /
// `AuthInfoResponse`.
// ---------------------------------------------------------------------------

export interface AuthUser {
  id: number
  email: string
  display_name: string
  role: Role
  is_active: boolean
  created_at: string
  updated_at: string
  last_login_at: string | null
}

interface AuthInfo {
  auth_enabled: boolean
  issuer: string
  audience: string
  algorithm: string
  access_token_minutes: number
}

interface LoginResponsePayload {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  user: AuthUser
}

interface StoredSession {
  access_token: string
  expires_at: number // epoch ms
  user: AuthUser
}

type AuthStatus =
  | { phase: 'booting' }
  | { phase: 'disabled' } // auth_enabled=false → synthetic anon user
  | { phase: 'unauthenticated'; notice: SessionNotice | null }
  | { phase: 'authenticated'; user: AuthUser }

export type SessionNotice =
  | { kind: 'expired'; message: string }
  | { kind: 'loggedOut'; message: string }

interface AuthContextValue {
  status: AuthStatus
  user: AuthUser | null
  /** Resolved when the user is signed in (or auth is disabled). */
  ready: boolean
  /** Effective role for route guards; null when not signed in. */
  role: Role | null
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  /** Clears any active session notice after the UI has displayed it. */
  clearNotice: () => void
  /** True when backend reports auth_enabled=true. */
  authEnabled: boolean
  /** Non-fatal status messages about the session. */
  notice: SessionNotice | null
}

const AuthContext = createContext<AuthContextValue | null>(null)

const SESSION_STORAGE_KEY = 'accd.session'
const SESSION_NOTICE_KEY = 'accd.session.notice'

// ---------------------------------------------------------------------------
// Session storage helpers
// ---------------------------------------------------------------------------

function readStoredSession(): StoredSession | null {
  try {
    const raw = window.localStorage.getItem(SESSION_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<StoredSession>
    if (
      typeof parsed.access_token !== 'string' ||
      typeof parsed.expires_at !== 'number' ||
      !parsed.user ||
      typeof parsed.user.id !== 'number'
    ) {
      return null
    }
    return parsed as StoredSession
  } catch {
    return null
  }
}

function writeStoredSession(session: StoredSession | null): void {
  try {
    if (session === null) {
      window.localStorage.removeItem(SESSION_STORAGE_KEY)
    } else {
      window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
    }
  } catch {
    /* localStorage may be blocked; non-fatal */
  }
}

function readStoredNotice(): SessionNotice | null {
  try {
    const raw = window.sessionStorage.getItem(SESSION_NOTICE_KEY)
    if (!raw) return null
    return JSON.parse(raw) as SessionNotice
  } catch {
    return null
  }
}

function writeStoredNotice(notice: SessionNotice | null): void {
  try {
    if (notice === null) {
      window.sessionStorage.removeItem(SESSION_NOTICE_KEY)
    } else {
      window.sessionStorage.setItem(SESSION_NOTICE_KEY, JSON.stringify(notice))
    }
  } catch {
    /* non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>({ phase: 'booting' })
  const sessionExpiredOnceRef = useRef(false)
  const navigate = useNavigate()

  // Wire the api client to read the current token + react to 401s.
  const getToken = useCallback(() => {
    if (status.phase === 'authenticated') {
      const stored = readStoredSession()
      if (!stored) return null
      if (stored.expires_at <= Date.now()) return null
      return stored.access_token
    }
    return null
  }, [status.phase])

  const handleSessionExpired = useCallback(() => {
    if (sessionExpiredOnceRef.current) return
    sessionExpiredOnceRef.current = true
    writeStoredSession(null)
    writeStoredNotice({
      kind: 'expired',
      message: 'Your session has expired. Please sign in again.',
    })
    setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
  }, [])

  const handleForbidden = useCallback(() => {
    // We don't drop the session on 403 — the user is still
    // authenticated, just lacking permission.  Components decide
    // how to render (e.g. AccessDenied).
  }, [])

  useEffect(() => {
    configureApi({
      getToken,
      onSessionExpired: handleSessionExpired,
      onForbidden: handleForbidden,
    })
  }, [getToken, handleSessionExpired, handleForbidden])

  // ----- bootstrap -----
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const info = await apiFetch<AuthInfo>('/api/auth/info', {
          skipAuthRedirect: true,
          timeoutMs: 8000,
        })
        if (cancelled) return
        if (!info.auth_enabled) {
          // Development / Phase 0-4 compat: synthetic anon user.
          setStatus({ phase: 'disabled' })
          return
        }
        const stored = readStoredSession()
        const notice = readStoredNotice()
        if (!stored || stored.expires_at <= Date.now()) {
          if (stored) writeStoredSession(null)
          setStatus({ phase: 'unauthenticated', notice })
          return
        }
        // Verify the stored token against the backend.
        try {
          const user = await apiFetch<AuthUser>('/api/auth/me', {
            skipAuthRedirect: true,
          })
          if (cancelled) return
          setStatus({ phase: 'authenticated', user })
          // If we had a notice, clear it once the user is back.
          if (notice) writeStoredNotice(null)
        } catch (err) {
          if (cancelled) return
          if (err instanceof ApiError && err.status === 401) {
            writeStoredSession(null)
            setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
          } else {
            // Network / backend down.  Fall back to unauthenticated
            // but keep the session; the login page will surface the
            // error.  Do NOT mark `sessionExpiredOnce`.
            setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
          }
        }
      } catch {
        if (cancelled) return
        // Backend unreachable.  Treat as auth_enabled=true and
        // unauthenticated; the login page will surface the error
        // when the user attempts to submit credentials.
        setStatus({ phase: 'unauthenticated', notice: null })
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // ----- actions -----

  const login = useCallback(
    async (email: string, password: string): Promise<void> => {
      const payload = await apiFetch<LoginResponsePayload>('/api/auth/login', {
        method: 'POST',
        json: { email, password },
        skipAuthRedirect: true,
        timeoutMs: 12000,
      })
      const stored: StoredSession = {
        access_token: payload.access_token,
        expires_at: Date.now() + payload.expires_in * 1000,
        user: payload.user,
      }
      writeStoredSession(stored)
      sessionExpiredOnceRef.current = false
      writeStoredNotice(null)
      setStatus({ phase: 'authenticated', user: payload.user })
    },
    [],
  )

  const logout = useCallback(async (): Promise<void> => {
    writeStoredSession(null)
    writeStoredNotice({
      kind: 'loggedOut',
      message: 'You have been signed out.',
    })
    sessionExpiredOnceRef.current = false
    setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
    navigate('/login', { replace: true })
  }, [navigate])

  const clearNotice = useCallback(() => {
    writeStoredNotice(null)
    setStatus((prev) =>
      prev.phase === 'unauthenticated' ? { phase: 'unauthenticated', notice: null } : prev,
    )
  }, [])

  // ----- derived values -----

  const user = status.phase === 'authenticated' ? status.user : null
  const role: Role | null = user ? user.role : status.phase === 'disabled' ? 'ADMIN' : null
  const ready = status.phase !== 'booting'
  const authEnabled = status.phase !== 'disabled'
  const notice: SessionNotice | null =
    status.phase === 'unauthenticated' ? status.notice : null

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      ready,
      role,
      login,
      logout,
      clearNotice,
      authEnabled,
      notice,
    }),
    [status, user, ready, role, login, logout, clearNotice, authEnabled, notice],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}

// Test-only internals.
export const __authTestInternals = {
  SESSION_STORAGE_KEY,
  SESSION_NOTICE_KEY,
  readStoredSession,
  writeStoredSession,
  readStoredNotice,
  writeStoredNotice,
}
