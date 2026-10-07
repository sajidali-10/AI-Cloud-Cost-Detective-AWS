// Phase 6A — Centralized API client.
//
// Design rules (enforced by Phase 6A spec):
//   * Relative URLs ONLY — never hardcoded host, IP, or localhost.
//     Nginx strips /api/ and forwards to the backend.  In dev, the
//     Vite proxy does the same.  Both routes resolve via `fetch`
//     against the current page origin.
//   * Bearer token injection — read from the auth store; never log.
//   * Typed responses — generic over the expected JSON shape.
//   * Normalised failures — every error becomes an `ApiError` with
//     a stable `code` so the UI can render a sane message without
//     inspecting raw Response objects.
//   * No secret material in thrown errors — error messages contain
//     only what the backend put in the response body.
//
// Session lifecycle is handled here too:
//   * onSessionExpired() is invoked when a 401 is observed AND a
//     session was previously active.  The auth provider wires this
//     to a state reset + redirect to /login?next=...
//
// fetch() is wrapped via a swappable transport for tests; in
// production the default `globalThis.fetch` is used.

export type ApiErrorCode =
  | 'Unauthorized'
  | 'Forbidden'
  | 'NotFound'
  | 'RateLimited'
  | 'Unavailable'
  | 'InvalidResponse'
  | 'Error'

export class ApiError extends Error {
  readonly code: ApiErrorCode
  readonly status: number
  readonly errorCode: string | undefined
  readonly payload: unknown

  constructor(args: {
    code: ApiErrorCode
    status: number
    message: string
    errorCode?: string | undefined
    payload?: unknown
  }) {
    super(args.message)
    this.name = 'ApiError'
    this.code = args.code
    this.status = args.status
    this.errorCode = args.errorCode
    this.payload = args.payload
  }
}

type TokenGetter = () => string | null
type SessionExpiredHandler = () => void
type ForbiddenHandler = () => void

let _getToken: TokenGetter = () => null
let _onSessionExpired: SessionExpiredHandler = () => {}
let _onForbidden: ForbiddenHandler = () => {}
let _transport: typeof fetch = (...args) => globalThis.fetch(...args)

// ---------------------------------------------------------------------------
// Wiring — called once by the auth provider at startup.
// ---------------------------------------------------------------------------

export function configureApi(opts: {
  getToken: TokenGetter
  onSessionExpired: SessionExpiredHandler
  onForbidden?: ForbiddenHandler
  transport?: typeof fetch
}): void {
  _getToken = opts.getToken
  _onSessionExpired = opts.onSessionExpired
  _onForbidden = opts.onForbidden ?? (() => {})
  if (opts.transport) _transport = opts.transport
}

// Test-only reset; not exported via index, only via this module.
export function __resetApiForTests(): void {
  _getToken = () => null
  _onSessionExpired = () => {}
  _onForbidden = () => {}
  _transport = (...args) => globalThis.fetch(...args)
}

// ---------------------------------------------------------------------------
// Error mapping
// ---------------------------------------------------------------------------

function mapStatusToCode(status: number): ApiErrorCode {
  if (status === 401) return 'Unauthorized'
  if (status === 403) return 'Forbidden'
  if (status === 404) return 'NotFound'
  if (status === 429) return 'RateLimited'
  if (status >= 500 && status <= 599) return 'Unavailable'
  return 'Error'
}

function safeMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object') {
    const p = payload as Record<string, unknown>
    if (typeof p.message === 'string' && p.message.trim().length > 0) {
      return p.message
    }
    if (typeof p.detail === 'string' && p.detail.trim().length > 0) {
      return p.detail
    }
  }
  return fallback
}

// ---------------------------------------------------------------------------
// apiFetch — the only public entry point.
// ---------------------------------------------------------------------------

export interface ApiFetchInit extends RequestInit {
  json?: unknown
  /** When true, do NOT auto-redirect on 401 (used by /auth/login). */
  skipAuthRedirect?: boolean
  /** Absolute timeout in ms (default 15s). */
  timeoutMs?: number
}

export async function apiFetch<T = unknown>(
  path: string,
  init: ApiFetchInit = {},
): Promise<T> {
  const { json, skipAuthRedirect, timeoutMs = 15000, headers, ...rest } = init

  // Path must start with "/" — relative to current origin.
  if (!path.startsWith('/')) {
    throw new ApiError({
      code: 'InvalidResponse',
      status: 0,
      message: `apiFetch: path must start with "/" (got ${path})`,
    })
  }

  const finalHeaders = new Headers(headers ?? {})
  if (json !== undefined && !finalHeaders.has('Content-Type')) {
    finalHeaders.set('Content-Type', 'application/json')
  }
  const token = _getToken()
  if (token) {
    finalHeaders.set('Authorization', `Bearer ${token}`)
  }
  finalHeaders.set('Accept', 'application/json')

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)

  let response: Response
  try {
    response = await _transport(path, {
      ...rest,
      headers: finalHeaders,
      body: json === undefined ? rest.body : JSON.stringify(json),
      signal: controller.signal,
    })
  } catch (cause) {
    clearTimeout(timeout)
    if (cause instanceof DOMException && cause.name === 'AbortError') {
      throw new ApiError({
        code: 'Unavailable',
        status: 0,
        message: 'Request timed out',
      })
    }
    throw new ApiError({
      code: 'Unavailable',
      status: 0,
      message: 'Backend unavailable',
    })
  } finally {
    clearTimeout(timeout)
  }

  // 204 No Content
  if (response.status === 204) {
    return undefined as T
  }

  let payload: unknown = undefined
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    try {
      payload = await response.json()
    } catch {
      payload = undefined
    }
  }

  if (response.ok) {
    return payload as T
  }

  const code = mapStatusToCode(response.status)
  const message = safeMessage(
    payload,
    code === 'Unavailable' ? 'Backend unavailable' : `Request failed (${response.status})`,
  )
  const errorCode =
    payload && typeof payload === 'object'
      ? (payload as Record<string, unknown>).error_code as string | undefined
      : undefined

  // Side effects for auth failures.
  if (response.status === 401 && token && !skipAuthRedirect) {
    _onSessionExpired()
  }
  if (response.status === 403 && !skipAuthRedirect) {
    _onForbidden()
  }

  throw new ApiError({
    code,
    status: response.status,
    message,
    errorCode,
    payload,
  })
}
