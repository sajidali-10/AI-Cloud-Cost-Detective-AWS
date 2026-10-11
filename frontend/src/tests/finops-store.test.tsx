// Phase 6B — In-memory FinOps query store tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import {
  invalidateCache,
  useFinopsQuery,
  _storeSize,
} from '../lib/finops/store'
import { configureApi, __resetApiForTests } from '../lib/api'
import { authDisabledElement } from './helpers/auth-bootstrap'

function FetchProbe({ cacheKey, fetcher, onResult }: {
  cacheKey: string
  fetcher: () => Promise<string>
  onResult: (status: string, data: string | null, err: Error | null) => void
}) {
  const sub = useFinopsQuery<string>(cacheKey, fetcher)
  onResult(sub.entry.status, sub.entry.data, sub.entry.error)
  return null
}

describe('useFinopsQuery', () => {
  beforeEach(() => {
    // Phase 7A: useFinopsQuery calls useAuth() and gates fetches on
    // auth readiness.  The bootstrap transport responds to
    // /api/auth/info with `{auth_enabled: false}`, which lands the
    // AuthProvider on the synthetic-anonymous "disabled" phase —
    // the gate opens and fetches proceed as before.
    __resetApiForTests()
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      onForbidden: () => {},
      transport: (async (input: RequestInfo | URL) => {
        const path = typeof input === 'string' ? input : input.toString()
        if (path === '/api/auth/info') {
          return new Response(JSON.stringify({ auth_enabled: false }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          })
        }
        return new Response(JSON.stringify({}), { status: 404 })
      }) as unknown as typeof fetch,
    })
    invalidateCache()
  })
  afterEach(() => {
    invalidateCache()
  })

  // The store notifies React via useSyncExternalStore (React 18)
  // whenever the fetcher resolves.  Each test wraps its assertions
  // in `waitFor` (which keeps every iteration inside an act()
  // boundary) so React does not log a "not wrapped in act" warning.

  it('fetches once per cache key and reuses cached data', async () => {
    let calls = 0
    const fetcher = vi.fn(async () => {
      calls += 1
      return `value-${calls}`
    })

    let firstStatus = ''
    let firstData: string | null = null
    render(authDisabledElement(<FetchProbe cacheKey="probe-1" fetcher={fetcher} onResult={(s, d) => { firstStatus = s; firstData = d }} />))

    await waitFor(() => {
      expect(firstStatus).toBe('success')
      expect(firstData).toBe('value-1')
    })
    expect(calls).toBe(1)
    expect(_storeSize()).toBe(1)
  })

  it('deduplicates concurrent subscribers on the same key', async () => {
    let calls = 0
    const fetcher = vi.fn(async () => {
      calls += 1
      await new Promise((r) => setTimeout(r, 10))
      return 'shared'
    })

    render(
      authDisabledElement(
        <>
          <FetchProbe cacheKey="probe-shared" fetcher={fetcher} onResult={() => undefined} />
          <FetchProbe cacheKey="probe-shared" fetcher={fetcher} onResult={() => undefined} />
        </>,
      ),
    )

    await waitFor(() => {
      expect(calls).toBe(1)
    })
  })

  it('propagates errors to subscribers', async () => {
    let observed: Error | null = null
    render(
      authDisabledElement(
        <FetchProbe
          cacheKey="probe-error"
          fetcher={async () => { throw new Error('boom') }}
          onResult={(_, _d, err) => { observed = err }}
        />,
      ),
    )

    await waitFor(() => {
      expect(observed).toBeInstanceOf(Error)
    })
    expect((observed as Error | null)?.message).toBe('boom')
  })

  it('invalidateCache forces the next subscriber to refetch', async () => {
    let calls = 0
    const fetcher = vi.fn(async () => {
      calls += 1
      return 'ok'
    })

    let observedData: string | null = null
    let refreshFn: () => void = () => undefined
    function Holder() {
      const sub = useFinopsQuery<string>('probe-refresh', fetcher)
      refreshFn = sub.refresh
      observedData = sub.entry.data
      return null
    }
    render(authDisabledElement(<Holder />))
    await waitFor(() => {
      expect(observedData).toBe('ok')
    })
    const before = calls
    refreshFn()
    await waitFor(() => {
      expect(calls).toBe(before + 1)
    })
  })
})
