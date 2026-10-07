// Phase 6A — API client tests.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiFetch, configureApi, __resetApiForTests } from '../lib/api'

function mockJsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

describe('apiFetch', () => {
  beforeEach(() => {
    __resetApiForTests()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uses relative paths only', async () => {
    const transport = vi.fn().mockResolvedValue(mockJsonResponse(200, { ok: true }))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/auth/info')).resolves.toEqual({ ok: true })
    expect(transport).toHaveBeenCalledWith(
      '/api/auth/info',
      expect.objectContaining({ headers: expect.any(Headers) }),
    )
    const url = transport.mock.calls[0][0] as string
    expect(url.startsWith('/')).toBe(true)
    // Hard guard — no host / IP / localhost in any browser call.
    expect(url).not.toMatch(/localhost|127\.0\.0\.1|10\.|192\.168\.|ec2/)
  })

  it('rejects non-relative paths', async () => {
    await expect(apiFetch('https://example.com/api')).rejects.toBeInstanceOf(ApiError)
  })

  it('attaches Authorization header when a token is present', async () => {
    let captured: RequestInit | undefined
    const transport = vi.fn().mockImplementation((_path: string, init: RequestInit) => {
      captured = init
      return Promise.resolve(mockJsonResponse(200, { ok: true }))
    })
    configureApi({
      getToken: () => 't0k3n',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await apiFetch('/api/auth/me')
    const headers = captured?.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer t0k3n')
  })

  it('serialises JSON body when json option is passed', async () => {
    let captured: RequestInit | undefined
    const transport = vi.fn().mockImplementation((_path: string, init: RequestInit) => {
      captured = init
      return Promise.resolve(mockJsonResponse(200, { ok: true }))
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await apiFetch('/api/auth/login', {
      method: 'POST',
      json: { email: 'a@b.com', password: 'x' },
    })
    expect(captured?.body).toBe(JSON.stringify({ email: 'a@b.com', password: 'x' }))
  })

  it('normalises 401 to ApiError(Unauthorized)', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockJsonResponse(401, { status: 'error', error_code: 'InvalidCredentials', message: 'invalid credentials' }),
    )
    configureApi({
      getToken: () => 't0k3n',
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    try {
      await apiFetch('/api/auth/me')
      throw new Error('should have thrown')
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError)
      const e = err as ApiError
      expect(e.code).toBe('Unauthorized')
      expect(e.status).toBe(401)
      expect(e.errorCode).toBe('InvalidCredentials')
      expect(e.message).toBe('invalid credentials')
    }
  })

  it('normalises 403 to ApiError(Forbidden)', async () => {
    const transport = vi.fn().mockResolvedValue(
      mockJsonResponse(403, { error_code: 'Forbidden', message: 'forbidden' }),
    )
    configureApi({
      getToken: () => 't0k3n',
      onSessionExpired: () => {},
      onForbidden: vi.fn(),
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/admin/users')).rejects.toMatchObject({
      code: 'Forbidden',
      status: 403,
    })
  })

  it('normalises 5xx to ApiError(Unavailable)', async () => {
    const transport = vi.fn().mockResolvedValue(mockJsonResponse(503, { message: 'down' }))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/health/ready')).rejects.toMatchObject({
      code: 'Unavailable',
      status: 503,
    })
  })

  it('normalises network failure to ApiError(Unavailable)', async () => {
    const transport = vi.fn().mockRejectedValue(new TypeError('network down'))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/health/ready')).rejects.toMatchObject({
      code: 'Unavailable',
      status: 0,
    })
  })

  it('fires onSessionExpired when a 401 arrives with an active session', async () => {
    const onSessionExpired = vi.fn()
    const transport = vi.fn().mockResolvedValue(
      mockJsonResponse(401, { message: 'token expired' }),
    )
    configureApi({
      getToken: () => 't0k3n',
      onSessionExpired,
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/auth/me')).rejects.toBeInstanceOf(ApiError)
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
  })

  it('does NOT fire onSessionExpired on a 401 from /api/auth/login', async () => {
    const onSessionExpired = vi.fn()
    const transport = vi.fn().mockResolvedValue(
      mockJsonResponse(401, { message: 'invalid' }),
    )
    configureApi({
      getToken: () => null,
      onSessionExpired,
      transport: transport as unknown as typeof fetch,
    })
    await expect(
      apiFetch('/api/auth/login', {
        method: 'POST',
        json: { email: 'x@y.com', password: 'bad' },
        skipAuthRedirect: true,
      }),
    ).rejects.toBeInstanceOf(ApiError)
    expect(onSessionExpired).not.toHaveBeenCalled()
  })

  it('returns undefined body for 204 No Content', async () => {
    const transport = vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    await expect(apiFetch('/api/x')).resolves.toBeUndefined()
  })
})
