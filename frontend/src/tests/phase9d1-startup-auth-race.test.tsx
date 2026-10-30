// Phase 9D.1 — Regression: authenticated account discovery on reload.
//
// PRODUCTION DEFECT (headless-browser proven):
//   /api/aws/cloud-accounts/selectable   Authorization: NO  → 401
//   /api/auth/me                         Authorization: YES → 200
// repeated after every hard navigation, leaving the account
// selector at "0 registered" until a provider switch/refresh.
//
// ROOT CAUSE (React mount ordering):
//   api.ts boots with `getToken = () => null` (cold-start default).
//   AuthProvider bound the real storage-reading getter via
//   configureApi() inside a useEffect — but React runs CHILD
//   effects before PARENT effects, so AccountProvider's first-mount
//   selectable fetch fired before AuthProvider's wiring effect ran.
//
// This suite reproduces that exact ordering condition honestly:
//   * the api client is left in its cold-start DEFAULT state
//     (`__resetApiForTests()` only — configureApi is NEVER called
//     from this file; the ONLY code that may wire the token getter
//     is AuthProvider itself), and
//   * the real App provider stack is mounted in production order
//     (Auth → Provider → Account) so child-effect-before-parent-
//     effect ordering is preserved, not mocked away.
//
// The fetch stub replicates the production backend contract: a
// selectable request WITHOUT Authorization is answered 401 (which
// is exactly what turned AccountContext.accounts into [] in
// production).  It records only auth=YES/NO per request — bearer
// token VALUES are never captured, echoed, or logged.
//
// Pre-fix, scenario 1 fails twice: the first selectable request
// carries NO Authorization and the rows never load.  Post-fix,
// AuthProvider wires the getter during render — before any child
// effect — so the first selectable request is authenticated.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from '../lib/router'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider, __authTestInternals, type AuthUser } from '../lib/auth'
import { ProviderProvider, clearStoredProviderForTests } from '../lib/providerContext'
import {
  AccountProvider,
  clearStoredAccountSelectionForTests,
  useAccountContext,
} from '../lib/accountContext'
import { __resetApiForTests } from '../lib/api'
import type { SelectableAccount } from '../lib/accountContext'

// ---------------------------------------------------------------------------
// Fixtures — placeholders only; no real secrets, tokens, or accounts.
// ---------------------------------------------------------------------------

const USER: AuthUser = {
  id: 7,
  email: 'race-tester@example.test',
  display_name: 'Race Tester',
  role: 'ADMIN',
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
}

const PLACEHOLDER_TOKEN = 'test-jwt-placeholder'

const AWS_ROWS: SelectableAccount[] = [
  {
    id: 1,
    account_id: '123456789012',
    display_name: 'AWS QA-Dev',
    provider: 'AWS',
    enabled: true,
    is_management: false,
  },
  {
    id: 2,
    account_id: '210987654321',
    display_name: 'AWS Production',
    provider: 'AWS',
    enabled: true,
    is_management: false,
  },
]

const AZURE_ROWS: SelectableAccount[] = [
  {
    id: 3,
    account_id: '11111111-2222-3333-4444-555555555555',
    display_name: 'Microsoft Azure Sponsorship',
    provider: 'AZURE',
    enabled: true,
    is_management: false,
  },
  {
    id: 4,
    account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    display_name: 'MCPP Subscription',
    provider: 'AZURE',
    enabled: true,
    is_management: false,
  },
]

const RACKSPACE_ROWS: SelectableAccount[] = [
  {
    id: 5,
    account_id: '878783',
    display_name: 'Rackspace Production',
    provider: 'RACKSPACE',
    enabled: true,
    is_management: false,
  },
]

// ---------------------------------------------------------------------------
// Recording fetch stub — auth captured as YES/NO ONLY (never the value).
// ---------------------------------------------------------------------------

interface RecordedCall {
  path: string
  auth: 'YES' | 'NO'
}

function pathOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.toString()
  return input.url
}

function authPresence(init?: RequestInit): 'YES' | 'NO' {
  if (!init || !init.headers) return 'NO'
  try {
    const headers = new Headers(init.headers as HeadersInit)
    return headers.get('Authorization') ? 'YES' : 'NO'
  } catch {
    return 'NO'
  }
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function buildRecordingFetch(calls: RecordedCall[]): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const path = pathOf(input)
    const auth = authPresence(init)
    calls.push({ path, auth })

    if (path === '/api/auth/info') {
      return json(200, {
        auth_enabled: true,
        issuer: 'test-issuer',
        audience: 'test-audience',
        algorithm: 'HS256',
        access_token_minutes: 60,
      })
    }
    if (path === '/api/auth/me') {
      if (auth === 'NO') return json(401, { message: 'unauthorized' })
      return json(200, USER)
    }
    if (path === '/api/aws/cloud-accounts/selectable') {
      // Production contract: no bearer → 401 (this is what emptied
      // the selector on every reload before the fix).
      if (auth === 'NO') return json(401, { message: 'unauthorized' })
      return json(200, { items: AWS_ROWS, count: AWS_ROWS.length })
    }
    if (path === '/api/cloud/accounts/selectable/AZURE') {
      if (auth === 'NO') return json(401, { message: 'unauthorized' })
      return json(200, { items: AZURE_ROWS, count: AZURE_ROWS.length })
    }
    if (path === '/api/cloud/accounts/selectable/RACKSPACE') {
      if (auth === 'NO') return json(401, { message: 'unauthorized' })
      return json(200, { items: RACKSPACE_ROWS, count: RACKSPACE_ROWS.length })
    }
    return json(404, {})
  }) as unknown as typeof fetch
}

// ---------------------------------------------------------------------------
// Account-context probe — captures the latest accounts/loading/error state
// from INSIDE the real provider tree.
// ---------------------------------------------------------------------------

interface ProbeSnapshot {
  count: number
  names: string[]
  accountIds: string[]
  error: string | null
}

function makeProbe(): {
  snapshots: ProbeSnapshot[]
  latest: () => ProbeSnapshot
  Probe: () => null
} {
  const snapshots: ProbeSnapshot[] = []
  function Probe(): null {
    const { accounts, error } = useAccountContext()
    snapshots.push({
      count: accounts.length,
      names: accounts.map((a) => a.display_name),
      accountIds: accounts.map((a) => a.account_id),
      error,
    })
    return null
  }
  return {
    snapshots,
    latest: () => {
      if (snapshots.length === 0) {
        return { count: 0, names: [], accountIds: [], error: null }
      }
      return snapshots[snapshots.length - 1]
    },
    Probe,
  }
}

// ---------------------------------------------------------------------------
// Harness — REAL provider stack in App.tsx order.  The api client is left
// in its cold-start default state on purpose: AuthProvider must wire the
// token getter before AccountProvider's mount effect can fire.
// ---------------------------------------------------------------------------

function seedValidSession(): void {
  __authTestInternals.writeStoredSession({
    access_token: PLACEHOLDER_TOKEN,
    expires_at: Date.now() + 60 * 60 * 1000,
    user: USER,
  })
}

function renderStack(Probe: () => null) {
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <Probe />
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

function selectableCalls(calls: RecordedCall[], path: string): RecordedCall[] {
  return calls.filter((c) => c.path === path)
}

beforeEach(() => {
  // Cold-start DEFAULT api-client state — configureApi is never
  // called from this suite.
  __resetApiForTests()
  window.sessionStorage.clear()
  window.localStorage.clear()
  clearStoredAccountSelectionForTests()
  clearStoredProviderForTests()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  __resetApiForTests()
  window.sessionStorage.clear()
  window.localStorage.clear()
})

describe('Phase 9D.1 — authenticated account discovery on fresh startup', () => {
  it('AWS: the FIRST selectable request after a cold mount carries Authorization and rows load', async () => {
    // 1. valid accd.session exists BEFORE the application mounts.
    seedValidSession()
    // 2. api client stays in its normal cold-start/default state.
    const calls: RecordedCall[] = []
    vi.stubGlobal('fetch', buildRecordingFetch(calls))
    const probe = makeProbe()

    // 3. application/providers mount.
    renderStack(probe.Probe)

    // 4–5. first selectable-account request occurs and MUST be
    // authenticated — no provider switch, no refresh, no remount,
    // no timeout-driven retry (exactly one request).
    await waitFor(() => {
      expect(selectableCalls(calls, '/api/aws/cloud-accounts/selectable').length).toBe(1)
    })
    const first = selectableCalls(calls, '/api/aws/cloud-accounts/selectable')[0]
    expect(first.auth).toBe('YES')

    // 6. selectable rows load on the first pass.
    await waitFor(() => {
      expect(probe.latest().count).toBe(2)
    })
    expect(probe.latest().names).toEqual(
      expect.arrayContaining(['AWS QA-Dev', 'AWS Production']),
    )
    // 12-digit cloud-side account ids (never database row ids).
    for (const id of probe.latest().accountIds) {
      expect(id).toMatch(/^\d{12}$/)
    }
    expect(probe.latest().error).toBeNull()

    // No unauthenticated selectable request ever leaked out.
    expect(
      calls.some(
        (c) => c.path.includes('selectable') && c.auth === 'NO',
      ),
    ).toBe(false)

    // 401 handling intact but untriggered: the session survives.
    expect(__authTestInternals.readStoredSession()).not.toBeNull()
  })

  it('RACKSPACE persisted provider: fresh mount loads selectable rows authenticated', async () => {
    seedValidSession()
    window.sessionStorage.setItem('cd.provider.selection.v1', 'RACKSPACE')
    const calls: RecordedCall[] = []
    vi.stubGlobal('fetch', buildRecordingFetch(calls))
    const probe = makeProbe()

    renderStack(probe.Probe)

    await waitFor(() => {
      expect(selectableCalls(calls, '/api/cloud/accounts/selectable/RACKSPACE').length).toBe(1)
    })
    expect(
      selectableCalls(calls, '/api/cloud/accounts/selectable/RACKSPACE')[0].auth,
    ).toBe('YES')

    await waitFor(() => {
      expect(probe.latest().count).toBe(1)
    })
    expect(probe.latest().names).toEqual(['Rackspace Production'])
    expect(probe.latest().error).toBeNull()
    expect(calls.some((c) => c.path.includes('selectable') && c.auth === 'NO')).toBe(false)
    expect(__authTestInternals.readStoredSession()).not.toBeNull()
  })

  it('AZURE persisted provider: fresh mount loads selectable subscriptions authenticated', async () => {
    seedValidSession()
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const calls: RecordedCall[] = []
    vi.stubGlobal('fetch', buildRecordingFetch(calls))
    const probe = makeProbe()

    renderStack(probe.Probe)

    await waitFor(() => {
      expect(selectableCalls(calls, '/api/cloud/accounts/selectable/AZURE').length).toBe(1)
    })
    expect(
      selectableCalls(calls, '/api/cloud/accounts/selectable/AZURE')[0].auth,
    ).toBe('YES')

    await waitFor(() => {
      expect(probe.latest().count).toBe(2)
    })
    expect(probe.latest().names).toEqual(
      expect.arrayContaining(['Microsoft Azure Sponsorship', 'MCPP Subscription']),
    )
    expect(probe.latest().error).toBeNull()
    expect(calls.some((c) => c.path.includes('selectable') && c.auth === 'NO')).toBe(false)
    expect(__authTestInternals.readStoredSession()).not.toBeNull()
  })
})
