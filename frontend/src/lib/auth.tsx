// Phase 7A closure — Authentication state machine.
//
// Three-state bootstrap:
//   checking        — bootstrap is in flight (calling /api/auth/info
//                     and optionally /api/auth/me).  No FinOps
//                     requests fire while we are in this state.
//   authenticated   — valid token (either freshly issued by /login or
//                     restored from sessionStorage + /auth/me).
//   unauthenticated — no token (initial state with nothing stored, or
//                     the stored token failed validation).
//   disabled        — backend reports auth_enabled=false; synthetic
//                     anonymous user.
//
// Lifecycle:
//   1. On mount, call GET /api/auth/info (anonymous) to learn
//      `auth_enabled` from the backend.
//   2. If `auth_enabled=false` → the app is open (synthetic anon).
//   3. If `auth_enabled=true`:
//      a. If a stored session exists in sessionStorage, validate
//         it with /api/auth/me.  On success → authenticated.  On
//         401 / expired → unauthenticated.
//      b. If no stored session → unauthenticated.
//   4. The sessionStorage['accd.session'] key is the source of truth
//      for the bearer token.  Centralized `getToken` reads it
//      synchronously on every request — no React-state gate — so a
//      successful login() makes the next apiFetch() carry the new
//      token even when the configured getToken callback has not yet
//      been refreshed.
//
// Token storage: sessionStorage under 'accd.session'.  Phase 7A
// moves JWTs out of localStorage so a tab refresh in a different
// browser session does not silently inherit a stale token.  We also
// run a one-time cleanup of any pre-existing localStorage entry.
//
// Security invariants enforced here:
//   * The bearer token is NEVER logged or echoed in errors.
//   * The password is NEVER stored or echoed.
//   * 401 responses automatically drop the session and surface a
//     "session expired" notification.
//   * 401 does NOT cause a redirect loop — at most once per session
//     lifetime, controlled by `sessionExpiredOnceRef`.
//   * Logout clears sessionStorage + FinOps cache + drives the WS
//     hook to close.
//   * No JWT in URL query, path, or fragment.

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
import { invalidateFinopsCache } from './finops/cache'
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

export type AuthPhase =
  | 'checking'
  | 'authenticated'
  | 'unauthenticated'
  | 'disabled'

export type AuthStatus =
  | { phase: 'checking' }
  | { phase: 'disabled' }
  | { phase: 'unauthenticated'; notice: SessionNotice | null }
  | { phase: 'authenticated'; user: AuthUser }

export type SessionNotice =
  | { kind: 'expired'; message: string }
  | { kind: 'loggedOut'; message: string }

interface AuthContextValue {
  status: AuthStatus
  user: AuthUser | null
  /** Resolved when bootstrap is complete (any terminal phase). */
  ready: boolean
  /** Resolved when the user is signed in (or auth is disabled). */
  authReady: boolean
  /** Effective role for route guards; null when not signed in. */
  role: Role | null
  /** Synchronous access to the current bearer token. */
  accessToken: string | null
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

// sessionStorage key — Phase 7A closure.  Replaces the prior
// localStorage key for the bearer token.  We deliberately keep the
// same KEY NAME so a single cleanup pass at startup can remove any
// stale localStorage entry without changing the wire contract.
const SESSION_STORAGE_KEY = 'accd.session'
const SESSION_NOTICE_KEY = 'accd.session.notice'
// Storage keys that must never end up holding auth material.
const FORBIDDEN_LOCALSTORAGE_KEYS = [SESSION_STORAGE_KEY]

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

function safeSessionStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null
    return window.sessionStorage
  } catch {
    return null
  }
}

function safeLocalStorage(): Storage | null {
  try {
    if (typeof window === 'undefined') return null
    return window.localStorage
  } catch {
    return null
  }
}

export function readStoredSession(): StoredSession | null {
  const storage = safeSessionStorage()
  if (!storage) return null
  try {
    const raw = storage.getItem(SESSION_STORAGE_KEY)
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

export function writeStoredSession(session: StoredSession | null): void {
  const storage = safeSessionStorage()
  if (!storage) return
  try {
    if (session === null) {
      storage.removeItem(SESSION_STORAGE_KEY)
    } else {
      storage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session))
    }
  } catch {
    /* storage may be blocked; non-fatal */
  }
}

export function readStoredNotice(): SessionNotice | null {
  const storage = safeSessionStorage()
  if (!storage) return null
  try {
    const raw = storage.getItem(SESSION_NOTICE_KEY)
    if (!raw) return null
    return JSON.parse(raw) as SessionNotice
  } catch {
    return null
  }
}

export function writeStoredNotice(notice: SessionNotice | null): void {
  const storage = safeSessionStorage()
  if (!storage) return
  try {
    if (notice === null) {
      storage.removeItem(SESSION_NOTICE_KEY)
    } else {
      storage.setItem(SESSION_NOTICE_KEY, JSON.stringify(notice))
    }
  } catch {
    /* non-fatal */
  }
}

/**
 * One-time cleanup of any pre-Phase 7A auth material that may have
 * landed in localStorage before the JWT-storage migration.  Removing
 * the legacy key prevents a previous deployment from leaving a stale
 * JWT in the user's browser after we deploy the sessionStorage-only
 * path.
 *
 * Idempotent and safe to run on every bootstrap.
 */
export function cleanupLegacyAuthStorage(): void {
  const ls = safeLocalStorage()
  if (!ls) return
  for (const key of FORBIDDEN_LOCALSTORAGE_KEYS) {
    try {
      ls.removeItem(key)
    } catch {
      /* non-fatal */
    }
  }
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>({ phase: 'checking' })
  const sessionExpiredOnceRef = useRef(false)
  const navigate = useNavigate()

  // Wire the api client to read the current token + react to 401s.
  //
  // CRITICAL: `getToken` reads storage directly (no React-state gate).
  // Storage is the source of truth for the bearer token.  When login()
  // calls `writeStoredSession()` followed by `setStatus({...})`, the
  // very next apiFetch() must return the NEW token — even before the
  // `useEffect` that re-runs `configureApi` with the new `getToken`
  // closure has fired.  Gating on `status.phase === 'authenticated'`
  // re-introduces the race that produced the Phase 7A symptoms
  // (FinOps queries firing before the new token was wired).
  const getToken = useCallback((): string | null => {
    const stored = readStoredSession()
    if (!stored) return null
    if (stored.expires_at <= Date.now()) return null
    return stored.access_token
  }, [])

  const handleSessionExpired = useCallback(() => {
    if (sessionExpiredOnceRef.current) return
    sessionExpiredOnceRef.current = true
    writeStoredSession(null)
    writeStoredNotice({
      kind: 'expired',
      message: 'Your session has expired. Please sign in again.',
    })
    invalidateFinopsCache()
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
    // Defensive: strip any stale JWT that may still live in
    // localStorage from a pre-Phase 7A deployment so a user does
    // not silently inherit a stored credential from a previous
    // origin.
    cleanupLegacyAuthStorage()
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
          if (notice) writeStoredNotice(null)
        } catch (err) {
          if (cancelled) return
          if (err instanceof ApiError && err.status === 401) {
            writeStoredSession(null)
            setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
          } else {
            // Network / backend down: drop into unauthenticated but
            // keep the stored session so the user can retry without
            // re-entering credentials.
            setStatus({ phase: 'unauthenticated', notice: readStoredNotice() })
          }
        }
      } catch {
        if (cancelled) return
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
      // Persist FIRST so the very next apiFetch (from the
      // freshly-mounted Dashboard) returns the new token.  Storage
      // is the source of truth — see the long comment on
      // `getToken` above.
      writeStoredSession(stored)
      sessionExpiredOnceRef.current = false
      writeStoredNotice(null)
      // Invalidate any pre-login probes (a different user may have
      // logged in earlier in this tab).
      invalidateFinopsCache()
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
    // Clear user-scoped FinOps cache so a re-login (or another
    // user on the same browser) never sees the previous account's
    // data flash by.
    invalidateFinopsCache()
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
  const ready = status.phase !== 'checking'
  const authReady = status.phase === 'authenticated' || status.phase === 'disabled'
  const authEnabled = status.phase !== 'disabled'
  const notice: SessionNotice | null =
    status.phase === 'unauthenticated' ? status.notice : null

  // accessToken is the synchronous mirror of the in-memory storage
  // entry.  Useful for components (e.g. AIAnalystPage's WebSocket
  // hook) that need the token without touching storage themselves.
  const accessToken = useMemo(() => {
    if (status.phase === 'authenticated' || status.phase === 'checking') {
      const stored = readStoredSession()
      if (!stored) return null
      if (stored.expires_at <= Date.now()) return null
      return stored.access_token
    }
    return null
  }, [status.phase])

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      ready,
      authReady,
      role,
      accessToken,
      login,
      logout,
      clearNotice,
      authEnabled,
      notice,
    }),
    [status, user, ready, authReady, role, accessToken, login, logout, clearNotice, authEnabled, notice],
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
  FORBIDDEN_LOCALSTORAGE_KEYS,
  readStoredSession,
  writeStoredSession,
  readStoredNotice,
  writeStoredNotice,
  cleanupLegacyAuthStorage,
}
