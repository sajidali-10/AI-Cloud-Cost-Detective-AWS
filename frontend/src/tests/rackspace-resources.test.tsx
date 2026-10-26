// Phase 9D.1 — Rackspace Resources UI tests (Batch B).
//
// Focused tests for the canonical Rackspace inventory view:
//   * summary cards derive from the canonical by_type breakdown
//   * server / volume / load-balancer tables render the Phase 9B.2
//     normalized fields with "—" fallbacks for missing values
//   * ATTACHED / DETACHED distinction (evidence-only, never a
//     deletion prompt)
//   * zero load balancers is a SUCCESSFUL empty state
//   * loading / partial / unavailable / error states stay distinct
//   * cross-provider stale-data protection (cache keys + live
//     provider switch)
//
// No test requires real cloud credentials — every response is a
// local mock of the canonical /api/cloud/resources envelope.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { AccountProvider } from '../lib/accountContext'
import { ProviderProvider, useProviderContext } from '../lib/providerContext'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { MemoryRouter } from '../lib/router'
import { ResourcesPage } from '../pages/ResourcesPage'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function wrap(node: React.ReactNode) {
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <FinopsPeriodProvider initialDays={30} initialRegion="all">
                {node}
              </FinopsPeriodProvider>
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

// Live provider-switch probe — captures the provider context so a
// test can flip the active provider on a mounted page.
let providerProbe: ReturnType<typeof useProviderContext> | null = null
function ProviderProbe() {
  providerProbe = useProviderContext()
  return null
}

// ---------------------------------------------------------------------------
// Fixtures — canonical Phase 9B.2 envelope shapes
// ---------------------------------------------------------------------------

const TENANT = '10001112'

const BASE_SERVER_METADATA = {
  flavor_id: 'general1-2',
  flavor_name: '2 GB General Purpose v1',
  flavor_ram_mb: 2048,
  flavor_vcpus: 2,
  flavor_disk_gb: 80,
  availability_zone: 'nova',
  created: '2026-09-01T12:00:00+00:00',
}

function makeServer(
  id: string,
  name: string,
  region: string,
  metadataOverride: Record<string, unknown> | 'bare' = {},
) {
  const metadata =
    metadataOverride === 'bare'
      ? {}
      : { ...BASE_SERVER_METADATA, ...metadataOverride }
  return {
    provider: 'RACKSPACE',
    account_id: TENANT,
    region,
    resource_type: 'SERVER',
    resource_id: id,
    service: 'Cloud Servers',
    name,
    state: 'ACTIVE',
    composite_id: `RACKSPACE|${TENANT}|${region}|SERVER|${id}`,
    metadata,
  }
}

function makeVolume(
  id: string,
  name: string,
  region: string,
  attachmentState: 'ATTACHED' | 'DETACHED',
) {
  const attached = attachmentState === 'ATTACHED'
  return {
    provider: 'RACKSPACE',
    account_id: TENANT,
    region,
    resource_type: 'VOLUME',
    resource_id: id,
    service: 'Cloud Block Storage',
    name,
    state: attached ? 'IN-USE' : 'AVAILABLE',
    composite_id: `RACKSPACE|${TENANT}|${region}|VOLUME|${id}`,
    metadata: {
      size_gb: attached ? 100 : 150,
      volume_type: 'SSD',
      attachment_state: attachmentState,
      ...(attached ? { attached_server_ids: ['srv-01'] } : {}),
      availability_zone: 'nova',
      created: '2026-08-15T09:30:00+00:00',
    },
  }
}

function makeRackspaceEnvelope(opts: {
  resources: Array<Record<string, unknown>>
  byType: Array<{ key: string; count: number }>
  total: number
  status?: string
  subscriptionErrors?: Array<Record<string, unknown>>
}) {
  return {
    summary: {
      provider: 'RACKSPACE',
      scope: `acct:${TENANT}`,
      scope_kind: 'single',
      status: opts.status ?? 'SUCCESS',
      total_resources: opts.total,
      by_type: opts.byType,
      by_location: [
        { key: 'DFW', count: opts.total },
      ],
      by_resource_group: [],
      by_subscription: [
        {
          subscription_id: TENANT,
          display_name: 'Rackspace Cloud Account',
          count: opts.total,
        },
      ],
      subscription_errors: opts.subscriptionErrors ?? [],
      source: 'RACKSPACE_CLOUD_API',
    },
    resources: opts.resources,
  }
}

/** The canonical live-ish fixture: 3 servers, 2 volumes (one
 *  attached, one detached), zero load balancers. */
function standardRackspaceEnvelope() {
  return makeRackspaceEnvelope({
    resources: [
      makeServer('srv-01', 'web-01', 'DFW'),
      makeServer('srv-02', 'web-02', 'DFW'),
      makeServer('srv-03', 'db-01', 'ORD'),
      makeVolume('vol-1111', 'data-vol-01', 'DFW', 'ATTACHED'),
      makeVolume('vol-2222', 'orphan-vol-02', 'DFW', 'DETACHED'),
    ],
    byType: [
      { key: 'SERVER', count: 3 },
      { key: 'VOLUME', count: 2 },
    ],
    total: 5,
  })
}

const AZURE_ITEM = {
  provider: 'AZURE',
  account_id: 'a54d076b-2650-4b4f-a28c-2deed2011f83',
  region: 'eastus',
  resource_type: 'microsoft.compute/virtualmachines',
  resource_id:
    '/subscriptions/a54d076b/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/azure-vm-01',
  service: 'Microsoft.Compute',
  name: 'azure-vm-01',
  state: 'Running',
  composite_id:
    'AZURE|a54d076b|eastus|microsoft.compute/virtualmachines|azure-vm-01',
  metadata: {},
}

function makeAzureEnvelope() {
  return {
    summary: {
      provider: 'AZURE',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      total_resources: 1,
      by_type: [{ key: 'microsoft.compute/virtualmachines', count: 1 }],
      by_location: [{ key: 'eastus', count: 1 }],
      by_resource_group: [{ key: 'rg', count: 1 }],
      by_subscription: [
        {
          subscription_id: 'a54d076b-2650-4b4f-a28c-2deed2011f83',
          display_name: 'MCPP Subscription',
          count: 1,
        },
      ],
      subscription_errors: [],
      source: 'AZURE_RESOURCE_GRAPH',
    },
    resources: [AZURE_ITEM],
  }
}

function installTransport(handler: (path: string) => Response | Promise<Response>) {
  const calls: string[] = []
  const transport = vi.fn(async (path: string) => {
    calls.push(path)
    return handler(path)
  })
  configureApi({
    getToken: () => null,
    onSessionExpired: () => {},
    transport: transport as unknown as typeof fetch,
  })
  return { transport, calls }
}

function defaultRoutesHandler(
  rackspaceBody: () => Response | Promise<Response>,
  azureBody?: () => Response | Promise<Response>,
) {
  return async (path: string): Promise<Response> => {
    if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
    if (path.startsWith('/api/cloud/accounts/selectable/')) {
      return mockJson(200, { items: [], count: 0 })
    }
    if (path.startsWith('/api/cloud/resources')) {
      if (path.includes('provider=RACKSPACE')) return rackspaceBody()
      if (path.includes('provider=AZURE') && azureBody) return azureBody()
      return mockJson(404, {})
    }
    return mockJson(404, {})
  }
}

function setProviders(provider: 'AWS' | 'AZURE' | 'RACKSPACE') {
  window.sessionStorage.setItem('cd.provider.selection.v1', provider)
  window.sessionStorage.setItem('cd.account.selection.v1', 'all')
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace Resources view', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
    setProviders('RACKSPACE')
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('fetches the canonical /api/cloud/resources route with provider=RACKSPACE (never a legacy AWS route)', async () => {
    const { calls } = installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)
    await screen.findByTestId('rackspace-resources-view')
    // The canonical fetch is issued once auth bootstrap completes —
    // wait for it rather than racing the auth gate.
    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.startsWith('/api/cloud/resources') &&
            c.includes('provider=RACKSPACE'),
        ),
      ).toBe(true)
    })
    expect(calls.some((c) => c.startsWith('/api/aws/resources'))).toBe(false)
  })

  it('renders the provider-level summary from the canonical by_type breakdown', async () => {
    installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)

    // Wait for the inventory to load before asserting derived
    // totals (the fetch fires after auth bootstrap).
    await screen.findByText('web-01')
    // Total derives from summary.total_resources — not recomputed
    // from unrelated data.
    expect(screen.getByTestId('rackspace-resources-total')).toHaveTextContent('5')
    expect(screen.getByTestId('rackspace-card-total')).toHaveTextContent('5')
    expect(screen.getByTestId('rackspace-card-servers')).toHaveTextContent('3')
    expect(screen.getByTestId('rackspace-card-volumes')).toHaveTextContent('2')
    // Zero load balancers is an authoritative zero for a
    // successfully inventoried service.
    expect(screen.getByTestId('rackspace-card-load-balancers')).toHaveTextContent('0')
  })

  it('renders server rows with normalized Phase 9B.2 fields', async () => {
    installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)

    expect(await screen.findByText('web-01')).toBeInTheDocument()
    expect(screen.getByText('web-02')).toBeInTheDocument()
    expect(screen.getByText('db-01')).toBeInTheDocument()
    // Normalized fields from metadata: flavor, vCPU, RAM, disk, created.
    expect(screen.getAllByText('2 GB General Purpose v1').length).toBeGreaterThan(0)
    expect(screen.getAllByText('80 GB').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Sep 1, 2026').length).toBeGreaterThan(0)
    // RAM: 2048 MB converts exactly to 2 GB.
    expect(screen.getAllByText('2 GB').length).toBeGreaterThan(0)
    // ACTIVE is the normal healthy status — rendered as-is, never
    // relabelled as idle/oversized/waste.
    expect(screen.getAllByText('ACTIVE').length).toBeGreaterThan(0)
  })

  it('renders volume rows and visually distinguishes ATTACHED from DETACHED', async () => {
    installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)

    expect(await screen.findByText('data-vol-01')).toBeInTheDocument()
    expect(screen.getByText('orphan-vol-02')).toBeInTheDocument()
    // Both attachment states render with distinct badge testids.
    expect(
      screen.getByTestId('volume-attachment-vol-1111'),
    ).toHaveTextContent('ATTACHED')
    expect(
      screen.getByTestId('volume-attachment-vol-2222'),
    ).toHaveTextContent('DETACHED')
    // Size / type derive from canonical metadata.
    expect(screen.getAllByText('150 GB').length).toBeGreaterThan(0)
    expect(screen.getAllByText('SSD').length).toBeGreaterThan(0)
  })

  it('renders a successful empty state for zero load balancers (not an error)', async () => {
    installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)

    expect(
      await screen.findByText('No Rackspace load balancers discovered.'),
    ).toBeInTheDocument()
    // A valid empty result is not an unavailable service.
    expect(screen.queryByTestId('rackspace-service-unavailable')).toBeNull()
    // And it is not an error.
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('uses the shared "—" fallback for missing optional fields', async () => {
    const envelope = makeRackspaceEnvelope({
      resources: [
        makeServer('srv-99', 'bare-01', 'DFW', 'bare'),
      ],
      byType: [{ key: 'SERVER', count: 1 }],
      total: 1,
    })
    installTransport(defaultRoutesHandler(async () => mockJson(200, envelope)))
    wrap(<ResourcesPage />)

    await screen.findByText('bare-01')
    const view = screen.getByTestId('rackspace-resources-view')
    // Missing flavor / vCPU / RAM / disk / created render the
    // existing fallback placeholder, never fabricated values.
    const dashes = Array.from(view.querySelectorAll('*')).filter(
      (el) => el.children.length === 0 && el.textContent === '—',
    )
    expect(dashes.length).toBeGreaterThan(0)
  })

  it('shows a loading state and renders no rows (and no fabricated totals) while the request is pending', async () => {
    let release: ((value: Response) => void) | null = null
    const pending = new Promise<Response>((resolve) => {
      release = resolve
    })
    const { calls } = installTransport(
      defaultRoutesHandler(() => pending),
    )
    const { container } = wrap(<ResourcesPage />)

    expect(await screen.findByTestId('rackspace-resources-view')).toBeInTheDocument()
    // Wait for the request to be issued (auth gate) before
    // asserting the pending state.
    await waitFor(() => {
      expect(
        calls.some((c) => c.startsWith('/api/cloud/resources')),
      ).toBe(true)
    })
    // Skeleton placeholders render…
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
    // …and no resource row content is present while loading.
    expect(screen.queryByText('web-01')).toBeNull()
    expect(container.querySelector('table')).toBeNull()
    // The totals do not fabricate zero counts while loading — the
    // shared "—" placeholder renders instead (loading is distinct
    // from an authoritative zero).
    expect(screen.getByTestId('rackspace-resources-total')).toHaveTextContent('—')
    const totalCard = screen.getByTestId('rackspace-card-total')
    expect(totalCard).toHaveTextContent('—')
    expect(
      totalCard.querySelector('[data-testid="metric-card"]')?.getAttribute('data-empty'),
    ).toBe('true')
    expect(totalCard).toHaveTextContent('Loading…')

    await act(async () => {
      release!(mockJson(200, standardRackspaceEnvelope()))
    })
    expect(await screen.findByText('web-01')).toBeInTheDocument()
  })

  it('renders PARTIAL_SUCCESS with valid data, a partial notice, and an honest unavailable volume section', async () => {
    const envelope = makeRackspaceEnvelope({
      resources: [
        makeServer('srv-01', 'web-01', 'DFW'),
        makeServer('srv-02', 'web-02', 'DFW'),
      ],
      byType: [{ key: 'SERVER', count: 2 }],
      total: 2,
      status: 'PARTIAL_SUCCESS',
      subscriptionErrors: [
        {
          subscription_id: TENANT,
          display_name: 'Cloud Block Storage',
          code: 'NETWORK_ERROR',
          message: 'transport failure',
        },
      ],
    })
    installTransport(defaultRoutesHandler(async () => mockJson(200, envelope)))
    wrap(<ResourcesPage />)

    // Valid server data still renders…
    expect(await screen.findByText('web-01')).toBeInTheDocument()
    expect(screen.getByText('web-02')).toBeInTheDocument()
    // …the partial-data notice is visible…
    expect(await screen.findByTestId('rackspace-resources-partial-warning')).toBeInTheDocument()
    expect(
      (await screen.findAllByTestId('partial-warning')).length,
    ).toBeGreaterThan(0)
    // …and the failed service is reported as unavailable, never
    // as a fabricated zero.
    expect(screen.getByTestId('rackspace-card-volumes')).toHaveTextContent('—')
    expect(screen.getByTestId('rackspace-card-volumes')).toHaveTextContent('Inventory unavailable')
    expect(screen.getAllByTestId('rackspace-service-unavailable').length).toBeGreaterThan(0)
    expect(screen.queryByText('orphan-vol-02')).toBeNull()
  })

  it('renders a professional error state when the surface fails (no fabricated rows)', async () => {
    installTransport(
      defaultRoutesHandler(async () =>
        mockJson(503, {
          status: 'error',
          error_code: 'NOT_CONFIGURED',
          message: 'no Rackspace accounts are registered',
        }),
      ),
    )
    wrap(<ResourcesPage />)

    expect(
      await screen.findByText('Could not load Rackspace resources'),
    ).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText('web-01')).toBeNull()
    expect(screen.queryByTestId('rackspace-card-servers')).toBeNull()
  })

  it('surfaces no destructive or management actions for Rackspace resources', async () => {
    installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    const { container } = wrap(<ResourcesPage />)
    await screen.findByText('web-01')

    const forbidden =
      /delete|detach|stop|reboot|resize|cleanup|remove now|terminate/i
    const buttons = Array.from(container.querySelectorAll('button'))
    const offending = buttons.filter((b) => forbidden.test(b.textContent ?? ''))
    expect(offending).toEqual([])
    // The advisory note explains the evidence-only semantics.
    expect(
      screen.getByTestId('rackspace-volume-review-note'),
    ).toHaveTextContent(/not\s+flagged as safe to delete/i)
  })
})

// ---------------------------------------------------------------------------
// Cross-provider stale-data protection
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — cross-provider stale-data protection (resources)', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('builds provider-isolated cache keys for resources', async () => {
    const { buildCloudResourcesCacheKey } = await import(
      '../lib/finops/cloudResources'
    )
    const rackspace = buildCloudResourcesCacheKey('acct:1', 'rackspace')
    const aws = buildCloudResourcesCacheKey('acct:1', 'aws')
    const azure = buildCloudResourcesCacheKey('sub:guid', 'azure')
    expect(rackspace).not.toEqual(aws)
    expect(rackspace).not.toEqual(azure)
    expect(rackspace.startsWith('rackspace:')).toBe(true)
    expect(aws.startsWith('aws:')).toBe(true)
    expect(azure.startsWith('azure:')).toBe(true)
    // Scope is part of the key too.
    expect(buildCloudResourcesCacheKey('acct:2', 'rackspace')).not.toEqual(rackspace)
    expect(buildCloudResourcesCacheKey(null, 'rackspace')).toBe(
      'rackspace:cloud:resources:all',
    )
  })

  it('a live Rackspace → Azure switch replaces Rackspace rows with Azure data (no stale rows, loading state in between)', async () => {
    setProviders('RACKSPACE')
    let releaseAzure: ((value: Response) => void) | null = null
    const azurePending = new Promise<Response>((resolve) => {
      releaseAzure = resolve
    })
    installTransport(
      defaultRoutesHandler(
        async () => mockJson(200, standardRackspaceEnvelope()),
        () => azurePending,
      ),
    )
    const { container } = wrap(
      <>
        <ProviderProbe />
        <ResourcesPage />
      </>,
    )

    // Rackspace rows render first.
    expect(await screen.findByText('web-01')).toBeInTheDocument()

    // Switch to Azure — the Rackspace response must not satisfy
    // the Azure request.
    act(() => {
      providerProbe!.setProvider('AZURE')
    })
    // While the Azure request is pending: no Rackspace rows, no
    // Azure rows yet — a loading state, not stale content.
    await waitFor(() => {
      expect(screen.queryByText('web-01')).toBeNull()
    })
    expect(screen.queryByText('azure-vm-01')).toBeNull()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)

    // The Azure request is issued against the canonical route.
    await act(async () => {
      releaseAzure!(mockJson(200, makeAzureEnvelope()))
    })
    expect(await screen.findByText('azure-vm-01')).toBeInTheDocument()
    expect(screen.queryByText('web-01')).toBeNull()
    expect(screen.queryByText('orphan-vol-02')).toBeNull()
  })

  it('a live Azure → Rackspace switch replaces Azure rows with Rackspace data', async () => {
    setProviders('AZURE')
    let releaseRackspace: ((value: Response) => void) | null = null
    const rackspacePending = new Promise<Response>((resolve) => {
      releaseRackspace = resolve
    })
    installTransport(
      defaultRoutesHandler(
        () => rackspacePending,
        async () => mockJson(200, makeAzureEnvelope()),
      ),
    )
    wrap(
      <>
        <ProviderProbe />
        <ResourcesPage />
      </>,
    )

    // Azure rows render first.
    expect(await screen.findByText('azure-vm-01')).toBeInTheDocument()

    act(() => {
      providerProbe!.setProvider('RACKSPACE')
    })
    await waitFor(() => {
      expect(screen.queryByText('azure-vm-01')).toBeNull()
    })
    await act(async () => {
      releaseRackspace!(mockJson(200, standardRackspaceEnvelope()))
    })
    expect(await screen.findByText('web-01')).toBeInTheDocument()
    expect(screen.queryByText('azure-vm-01')).toBeNull()
  })

  it('AWS → Rackspace isolation: the Rackspace view never consumes the AWS legacy resources route', async () => {
    setProviders('RACKSPACE')
    const { calls } = installTransport(
      defaultRoutesHandler(async () => mockJson(200, standardRackspaceEnvelope())),
    )
    wrap(<ResourcesPage />)
    await screen.findByText('web-01')
    // AWS inventory lives behind /api/aws/resources (legacy) — the
    // Rackspace view only ever queries the canonical route.
    expect(calls.some((c) => c.startsWith('/api/aws/resources'))).toBe(false)
    expect(
      calls.filter((c) => c.startsWith('/api/cloud/resources')).every(
        (c) => c.includes('provider=RACKSPACE'),
      ),
    ).toBe(true)
  })

  it('Rackspace → AWS isolation: cache keys cannot collide across providers', async () => {
    const { buildCloudResourcesCacheKey } = await import(
      '../lib/finops/cloudResources'
    )
    // The store keys the AWS (legacy) resources query with the
    // `aws:` prefix and Rackspace with `rackspace:` — a stale AWS
    // entry can never satisfy a Rackspace request and vice versa.
    const rackspaceKey = buildCloudResourcesCacheKey('all', 'rackspace')
    const awsKey = buildCloudResourcesCacheKey('all', 'aws')
    expect(rackspaceKey).not.toEqual(awsKey)
    expect(rackspaceKey.includes('rackspace')).toBe(true)
    expect(awsKey.includes('aws')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Source-level contract assertions (mirrors phase7c1 style)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace resources view contract', () => {
  it('routes RACKSPACE through the canonical view with provider-prefixed cache key + invalidation', () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require('node:fs') as typeof import('node:fs')
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const path = require('node:path') as typeof import('node:path')
    const read = (rel: string) =>
      fs.readFileSync(
        path.resolve(__dirname, '..', rel),
        'utf-8',
      )
    const view = read('pages/RackspaceResourcesView.tsx')
    // Canonical fetch + provider-prefixed cache key.  The canonical
    // call passes BOTH the provider and the account scope:
    //   fetchCloudResources({ provider: 'RACKSPACE', accountId: selection })
    // (whitespace-tolerant match; provider + accountId both pinned).
    expect(view).toMatch(
      /fetchCloudResources\(\{\s*provider:\s*'RACKSPACE',\s*accountId:\s*selection,?\s*\}/,
    )
    expect(view).toMatch(/buildCloudResourcesCacheKey\(selection, cacheProvider\)/)
    // Provider-switch invalidation via the shared store.
    expect(view).toMatch(/invalidateFinopsCache\(\)/)
    expect(view).toMatch(/prevProviderRef/)
    // No direct Rackspace product API usage from the browser.
    expect(view).not.toMatch(/api\.rackspacecloud\.com|identity\.api\.rackspacecloud\.com/)
    expect(view).not.toMatch(/X-Auth-Token/)

    const page = read('pages/ResourcesPage.tsx')
    expect(page).toMatch(/RackspaceResourcesView/)
    // The AWS path is unchanged — no redesign of the legacy view.
    expect(page).toMatch(/AzureResourcesView/)
  })
})
