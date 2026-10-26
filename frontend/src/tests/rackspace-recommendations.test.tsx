// Phase 9D.1 — Rackspace Recommendations UI tests (Batch B).
//
// Focused tests for the canonical Rackspace recommendation surface:
//   * rows render through /api/cloud/recommendations?provider=RACKSPACE
//     (never the Phase 9C.1 engine and never a legacy AWS route)
//   * RS-VOL-001 stays review/advisory — the backend wording is
//     rendered verbatim and never transformed into deletion /
//     waste language
//   * severity (impact) and confidence render from canonical fields
//   * unknown savings render "Not available" — never "$0"
//   * ACTIVE servers never gain frontend-derived rightsizing labels
//   * loading / empty / partial / error states stay distinct
//   * cross-provider stale-data protection (cache keys + live
//     provider switches in all four directions)
//
// No test requires real cloud credentials — every response is a
// local mock of the canonical envelope.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { AccountProvider } from '../lib/accountContext'
import { ProviderProvider, useProviderContext } from '../lib/providerContext'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { MemoryRouter } from '../lib/router'
import { OptimizationPage } from '../pages/OptimizationPage'

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
// Fixtures — canonical Phase 9C.2 envelope shapes
// ---------------------------------------------------------------------------

const TENANT = '10001112'

const RS_VOL_001_RECOMMENDED_ACTION =
  'Review this detached volume. Confirm whether it is still required, whether a snapshot/backup is needed, and remove it only after owner validation if it is no longer needed.'

/** RS-VOL-001 — Detached Block Storage Review, exactly as the
 *  Phase 9C.1/9C.2 backend emits it: action REVIEW, unknown
 *  savings, advisory wording, evidence + limitations in
 *  extensions. */
function makeRsVol001(overrides: Record<string, unknown> = {}) {
  return {
    provider: 'RACKSPACE',
    account_id: TENANT,
    region: 'DFW',
    recommendation_id: `RACKSPACE|${TENANT}|RS-VOL-001|DFW|VOLUME|vol-2222`,
    resource_id: 'vol-2222',
    resource_type: 'VOLUME',
    service: 'Cloud Block Storage',
    action: 'REVIEW',
    title: 'Detached block storage volume requires review',
    category: 'STORAGE_REVIEW',
    impact: 'MEDIUM',
    description:
      'Block storage volume vol-2222 in DFW is currently detached. Owner validation is required before any retirement decision.',
    estimated_savings: null,
    currency: null,
    source: 'RACKSPACE_CLOUD_API',
    extensions: {
      rule_id: 'RS-VOL-001',
      resource_name: 'orphan-vol-02',
      severity: 'MEDIUM',
      confidence: 'MEDIUM',
      recommended_action: RS_VOL_001_RECOMMENDED_ACTION,
      evidence: [
        { kind: 'volume_status', value: 'AVAILABLE' },
        { kind: 'attachment_state', value: 'DETACHED' },
        { kind: 'size_gb', value: 150 },
        { kind: 'volume_type', value: 'SSD' },
        { kind: 'region', value: 'DFW' },
        { kind: 'created_at', value: '2026-08-15T09:30:00+00:00' },
      ],
      savings_basis: 'UNAVAILABLE',
      limitations: [
        'detached-now does not imply detached-for-N-days',
        'resource-level price is unavailable; monthly savings cannot be proven',
        'created_at reflects resource age, not detachment duration',
      ],
      savings_period: 'MONTHLY',
      account_display_name: 'Rackspace Cloud Account',
    },
    ...overrides,
  }
}

function makeRackspaceSummary(overrides: Record<string, unknown> = {}) {
  return {
    provider: 'RACKSPACE',
    scope: `acct:${TENANT}`,
    scope_kind: 'single',
    status: 'SUCCESS',
    savings_status: 'EMPTY',
    currency: null,
    total_recommendations: 1,
    recommendations_with_savings: 0,
    recommendations_without_savings: 1,
    potential_annual_savings: null,
    potential_savings: null,
    savings_period: 'MONTHLY',
    savings_by_currency: [],
    by_impact: [{ key: 'MEDIUM', count: 1 }],
    by_resource_type: [{ key: 'VOLUME', count: 1 }],
    by_subscription: [],
    by_resource_group: [],
    by_type: [],
    by_subcategory: [],
    subscription_errors: [],
    by_account: [{ key: TENANT, count: 1 }],
    by_source: [{ key: 'RACKSPACE_CLOUD_API', count: 1 }],
    by_action: [{ key: 'REVIEW', count: 1 }],
    account_errors: [],
    source: 'RACKSPACE_OPTIMIZATION',
    ...overrides,
  }
}

function makeRackspaceRecsResponse(opts: {
  recommendations?: Array<Record<string, unknown>>
  summary?: Record<string, unknown>
} = {}) {
  return {
    summary: opts.summary ?? makeRackspaceSummary(),
    recommendations: opts.recommendations ?? [makeRsVol001()],
  }
}

const AWS_REC = {
  provider: 'AWS',
  account_id: '974053642038',
  region: 'us-east-1',
  recommendation_id: 'r-aws-1',
  resource_id: 'vol-001',
  resource_type: 'EBS_VOLUME',
  service: 'EC2',
  action: 'REVIEW_DELETE_UNATTACHED_EBS',
  title: 'Unattached EBS volume vol-001',
  category: 'Cost Optimization',
  impact: 'High',
  description: 'Volume has been detached for 30 days.',
  estimated_savings: null,
  currency: 'USD',
  source: 'UNKNOWN',
  extensions: {},
}

function makeAwsResponse() {
  return {
    summary: {
      provider: 'AWS',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      savings_status: 'EMPTY',
      currency: 'USD',
      total_recommendations: 1,
      recommendations_with_savings: 0,
      recommendations_without_savings: 1,
      potential_annual_savings: null,
      potential_savings: null,
      savings_period: 'MONTHLY',
      savings_by_currency: [],
      by_impact: [{ key: 'High', count: 1 }],
      by_resource_type: [{ key: 'EBS_VOLUME', count: 1 }],
      by_subscription: [],
      by_resource_group: [],
      by_type: [],
      by_subcategory: [],
      subscription_errors: [],
      by_account: [{ key: '974053642038', count: 1 }],
      by_source: [{ key: 'UNKNOWN', count: 1 }],
      by_action: [{ key: 'REVIEW_DELETE_UNATTACHED_EBS', count: 1 }],
      account_errors: [],
      source: 'AWS_OPTIMIZATION',
    },
    recommendations: [AWS_REC],
  }
}

const AZURE_REC = {
  provider: 'AZURE',
  account_id: 'a54d076b-2650-4b4f-a28c-2deed2011f83',
  region: 'eastus',
  recommendation_id: 'r-az-1',
  resource_id:
    '/subscriptions/a54d076b/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/azure-vm-01',
  resource_type: 'microsoft.compute/virtualmachines',
  service: 'Microsoft.Compute',
  action: 'Rightsize',
  title: 'Rightsize azure-vm-01',
  category: 'Cost',
  impact: 'Medium',
  description: 'Consider rightsizing this VM.',
  estimated_savings: null,
  currency: null,
  source: 'AZURE_ADVISOR',
  extensions: {},
}

function makeAzureResponse() {
  return {
    summary: {
      provider: 'AZURE',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      savings_status: 'EMPTY',
      currency: null,
      total_recommendations: 1,
      recommendations_with_savings: 0,
      recommendations_without_savings: 1,
      potential_annual_savings: null,
      potential_savings: null,
      savings_period: 'ANNUAL',
      savings_by_currency: [],
      by_impact: [{ key: 'Medium', count: 1 }],
      by_resource_type: [
        { key: 'microsoft.compute/virtualmachines', count: 1 },
      ],
      by_subscription: [],
      by_resource_group: [],
      by_type: [],
      by_subcategory: [],
      subscription_errors: [],
      by_account: [],
      by_source: [{ key: 'AZURE_ADVISOR', count: 1 }],
      by_action: [{ key: 'Rightsize', count: 1 }],
      account_errors: [],
      source: 'AZURE_ADVISOR',
    },
    recommendations: [AZURE_REC],
  }
}

function installTransport(
  handler: (path: string) => Response | Promise<Response>,
) {
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

function routesHandler(
  rackspace: () => Response | Promise<Response>,
  aws?: () => Response | Promise<Response>,
  azure?: () => Response | Promise<Response>,
) {
  return async (path: string): Promise<Response> => {
    if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
    if (path.startsWith('/api/cloud/accounts/selectable/')) {
      return mockJson(200, { items: [], count: 0 })
    }
    if (path.startsWith('/api/cloud/recommendations')) {
      if (path.includes('provider=RACKSPACE')) return rackspace()
      if (path.includes('provider=AWS') && aws) return aws()
      if (path.includes('provider=AZURE') && azure) return azure()
      return mockJson(404, {})
    }
    return mockJson(404, {})
  }
}

function setProviders(provider: 'AWS' | 'AZURE' | 'RACKSPACE') {
  window.sessionStorage.setItem('cd.provider.selection.v1', provider)
  window.sessionStorage.setItem('cd.account.selection.v1', 'all')
}

/** The document must never contain deletion / waste language for
 *  a Rackspace advisory finding. */
function expectNoAdvisoryLanguageDrift() {
  const text = document.body.textContent ?? ''
  expect(text).not.toMatch(/\bdelete (the )?volume\b/i)
  expect(text).not.toMatch(/\bunused volume\b/i)
  expect(text).not.toMatch(/\bsafe to remove\b/i)
  expect(text).not.toMatch(/\bguaranteed savings\b/i)
  expect(text).not.toMatch(/\bwaste(d)?\b/i)
  expect(text).not.toMatch(/\$0(\.00)?/s)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace Recommendations (OptimizationPage)', () => {
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

  it('fetches the canonical /api/cloud/recommendations route with provider=RACKSPACE (never the engine or a legacy route)', async () => {
    const { calls } = installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    expect(
      calls.some(
        (c) =>
          c.startsWith('/api/cloud/recommendations') &&
          c.includes('provider=RACKSPACE'),
      ),
    ).toBe(true)
    expect(calls.some((c) => c.startsWith('/api/aws/optimization'))).toBe(false)
    expect(calls.some((c) => c.includes('/api/cloud/rackspace'))).toBe(false)
  })

  it('renders the RS-VOL-001 review semantics verbatim (advisory, never deletion language)', async () => {
    installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)

    // Title + REVIEW action from the canonical fields.
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    expect(screen.getAllByText('REVIEW').length).toBeGreaterThan(0)
    expectNoAdvisoryLanguageDrift()

    // Open the advisory detail panel by clicking the row.
    const user = userEvent.setup()
    await user.click(screen.getByText('orphan-vol-02').closest('tr')!)
    expect(
      await screen.findByTestId('optimization-detail-panel'),
    ).toBeInTheDocument()
    // The backend recommended_action is rendered VERBATIM —
    // review, confirm requirement, backup, remove only after
    // validation.
    const action = screen.getByTestId('recommendation-recommended-action')
    expect(action).toHaveTextContent(/Review this detached volume/)
    expect(action).toHaveTextContent(/Confirm whether it is still required/)
    expect(action).toHaveTextContent(/snapshot\/backup/)
    expect(action).toHaveTextContent(/only after owner validation/)
    // Read-only advisory notice.
    expect(screen.getByText(/Read-only/)).toBeInTheDocument()
    expectNoAdvisoryLanguageDrift()
  })

  it('renders severity and confidence from the canonical fields', async () => {
    installTransport(
      routesHandler(async () =>
        mockJson(
          200,
          makeRackspaceRecsResponse({
            recommendations: [makeRsVol001({ impact: 'HIGH' })],
            summary: makeRackspaceSummary({
              total_recommendations: 1,
              by_impact: [{ key: 'HIGH', count: 1 }],
            }),
          }),
        ),
      ),
    )
    wrap(<OptimizationPage />)

    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    // Severity (impact) badge.
    expect(screen.getAllByText('High').length).toBeGreaterThan(0)
    // Confidence badge — from extensions.confidence, NOT from
    // impact.  The fixture pins impact=HIGH but confidence=MEDIUM,
    // so the two badges must disagree — proving confidence renders
    // the canonical extension field rather than deriving from
    // severity.
    await userEvent.click(screen.getByText('orphan-vol-02').closest('tr')!)
    const confidence = await screen.findByTestId('recommendation-confidence')
    expect(confidence).toHaveTextContent('Medium')
  })

  it('renders resource and region for Rackspace recommendations', async () => {
    installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)

    // Resource name (extensions.resource_name) in the table…
    expect(await screen.findByText('orphan-vol-02')).toBeInTheDocument()
    // …region from the canonical region field…
    expect(screen.getAllByText('DFW').length).toBeGreaterThan(0)
    // …and the raw resource id in the advisory detail.
    await userEvent.click(screen.getByText('orphan-vol-02').closest('tr')!)
    expect(await screen.findByText('vol-2222')).toBeInTheDocument()
  })

  it('renders evidence and limitations from the canonical extensions', async () => {
    installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)

    await userEvent.click(
      (await screen.findByText('orphan-vol-02')).closest('tr')!,
    )
    // Evidence kinds render as a definition list.
    const evidence = await screen.findByTestId('recommendation-evidence')
    expect(evidence).toHaveTextContent('attachment_state')
    expect(evidence).toHaveTextContent('size_gb')
    expect(evidence).toHaveTextContent('150')
    expect(evidence).toHaveTextContent('volume_type')
    // Limitations render as an explicit advisory list.
    const limitations = await screen.findByTestId('recommendation-limitations')
    expect(limitations).toHaveTextContent(
      'detached-now does not imply detached-for-N-days',
    )
    expect(limitations).toHaveTextContent(
      'resource-level price is unavailable; monthly savings cannot be proven',
    )
    expect(limitations).toHaveTextContent(
      'created_at reflects resource age, not detachment duration',
    )
  })

  it('renders unknown savings as unavailable — never as $0 (row, KPI and detail)', async () => {
    installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)

    // Row-level: savings cell shows "Not available".
    expect(await screen.findByTestId('savings-not-available')).toBeInTheDocument()
    // KPI-level: the Potential Monthly Savings tile shows
    // "Not available" — unknown != zero.
    const kpi = await screen.findByTestId('kpi-potential-monthly-savings')
    expect(kpi).toHaveTextContent('Not available')
    expect(kpi.textContent).not.toContain('$0')
    // Detail-level: same honesty.
    await userEvent.click(screen.getByText('orphan-vol-02').closest('tr')!)
    const savings = await screen.findByTestId('recommendation-savings-detail')
    expect(savings).toHaveTextContent('Not available')
    // Global: no $0 anywhere in the document.
    expectNoAdvisoryLanguageDrift()
  })

  it('does not derive rightsizing labels for ACTIVE servers from RAM / vCPU / flavor / age', async () => {
    // The Phase 9C engine emits NO server findings for ACTIVE
    // servers without metrics.  The canonical response therefore
    // contains only the detached-volume review — and the frontend
    // must not synthesize server recommendations client-side.
    installTransport(
      routesHandler(async () => mockJson(200, makeRackspaceRecsResponse())),
    )
    wrap(<OptimizationPage />)

    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/oversized|downsize|rightsiz/i)
    expect(text).not.toMatch(/\bidle\b/i)
    expect(text).not.toMatch(/wasted/i)
    expect(text).not.toMatch(/RS-SRV-001/)
    // Exactly one recommendation row — the backend-issued one.
    expect(
      (await screen.findByTestId('optimization-table')).querySelectorAll('tbody tr'),
    ).toHaveLength(1)
  })

  it('renders a successful empty state for zero recommendations (NO_DATA, not an error)', async () => {
    installTransport(
      routesHandler(async () =>
        mockJson(
          200,
          makeRackspaceRecsResponse({
            recommendations: [],
            summary: makeRackspaceSummary({
              status: 'NO_DATA',
              total_recommendations: 0,
              by_impact: [],
              by_resource_type: [],
              by_account: [],
              by_source: [],
              by_action: [],
            }),
          }),
        ),
      ),
    )
    wrap(<OptimizationPage />)

    expect(await screen.findByTestId('optimization-no-data')).toBeInTheDocument()
    expect(
      screen.getByText(/No active optimization recommendations were returned/),
    ).toBeInTheDocument()
    // A valid empty result is NOT an error.
    expect(screen.queryByRole('alert')).toBeNull()
    // Unknown savings still show "Not available" — not a zero.
    const kpi = screen.getByTestId('kpi-potential-monthly-savings')
    expect(kpi).toHaveTextContent('Not available')
  })

  it('renders PARTIAL_SUCCESS with valid rows and a partial-data notice naming the failed service', async () => {
    installTransport(
      routesHandler(async () =>
        mockJson(
          200,
          makeRackspaceRecsResponse({
            summary: makeRackspaceSummary({
              status: 'PARTIAL_SUCCESS',
              account_errors: [
                {
                  account_id: TENANT,
                  display_name: 'Cloud Load Balancers',
                  code: 'NETWORK_ERROR',
                  message: 'transport failure',
                },
              ],
            }),
          }),
        ),
      ),
    )
    wrap(<OptimizationPage />)

    // Valid findings still render…
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    // …the partial notice is visible with the failed service named…
    expect(await screen.findByTestId('optimization-partial-warning')).toBeInTheDocument()
    expect(
      (await screen.findAllByTestId('partial-warning')).length,
    ).toBeGreaterThan(0)
    // PartialWarning splits source / code / message into separate
    // spans, so match the failed service's message with a regex —
    // the service name must still be rendered.
    expect(
      screen.getByText(/Cloud Load Balancers: transport failure/),
    ).toBeInTheDocument()
    // …and the status badge reports PARTIAL_SUCCESS (never
    // silently upgraded to SUCCESS).
    expect(screen.getByText('PARTIAL_SUCCESS')).toBeInTheDocument()
  })

  it('renders a professional error state when the surface fails (no fabricated rows)', async () => {
    installTransport(
      routesHandler(async () =>
        mockJson(503, {
          status: 'error',
          error_code: 'NOT_CONFIGURED',
          message: 'no Rackspace accounts are registered',
        }),
      ),
    )
    wrap(<OptimizationPage />)

    expect(
      await screen.findByText('Could not load optimization recommendations'),
    ).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(
      screen.queryByText('Detached block storage volume requires review'),
    ).toBeNull()
  })

  it('shows a loading state and renders no rows while the request is pending', async () => {
    let release: ((value: Response) => void) | null = null
    const pending = new Promise<Response>((resolve) => {
      release = resolve
    })
    installTransport(routesHandler(() => pending))
    const { container } = wrap(<OptimizationPage />)

    expect(await screen.findByTestId('optimization-page')).toBeInTheDocument()
    // Skeleton placeholders render while the request is pending…
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)
    // …and no recommendation row content is present while loading.
    expect(
      screen.queryByText('Detached block storage volume requires review'),
    ).toBeNull()
    expect(screen.queryByText('orphan-vol-02')).toBeNull()

    await act(async () => {
      release!(mockJson(200, makeRackspaceRecsResponse()))
    })
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Cross-provider stale-data protection
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — cross-provider stale-data protection (recommendations)', () => {
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

  it('builds provider-isolated cache keys for recommendations', async () => {
    const { buildCloudRecommendationsCacheKey } = await import(
      '../lib/finops/cloudRecommendations'
    )
    const rackspace = buildCloudRecommendationsCacheKey('acct:1', 'rackspace')
    const aws = buildCloudRecommendationsCacheKey('acct:1', 'aws')
    const azure = buildCloudRecommendationsCacheKey('sub:guid', 'azure')
    expect(rackspace).not.toEqual(aws)
    expect(rackspace).not.toEqual(azure)
    expect(rackspace.startsWith('rackspace:')).toBe(true)
    expect(aws.startsWith('aws:')).toBe(true)
    expect(azure.startsWith('azure:')).toBe(true)
    // Region / days are part of the identity for the same provider.
    const scoped = buildCloudRecommendationsCacheKey('acct:1', 'rackspace', 'DFW', 30)
    expect(scoped).not.toEqual(rackspace)
    expect(scoped.startsWith('rackspace:')).toBe(true)
  })

  it('Rackspace → AWS: old Rackspace cards never render under the new provider; loading state appears first', async () => {
    setProviders('RACKSPACE')
    let releaseAws: ((value: Response) => void) | null = null
    const awsPending = new Promise<Response>((resolve) => {
      releaseAws = resolve
    })
    installTransport(
      routesHandler(
        async () => mockJson(200, makeRackspaceRecsResponse()),
        () => awsPending,
      ),
    )
    const { container } = wrap(
      <>
        <ProviderProbe />
        <OptimizationPage />
      </>,
    )

    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()

    act(() => {
      providerProbe!.setProvider('AWS')
    })
    // While the AWS request is pending: no Rackspace cards, no AWS
    // rows — a loading state, not stale content.
    await waitFor(() => {
      expect(
        screen.queryByText('Detached block storage volume requires review'),
      ).toBeNull()
    })
    expect(screen.queryByText('orphan-vol-02')).toBeNull()
    expect(screen.queryByText('Unattached EBS volume vol-001')).toBeNull()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)

    await act(async () => {
      releaseAws!(mockJson(200, makeAwsResponse()))
    })
    expect(await screen.findByText('Unattached EBS volume vol-001')).toBeInTheDocument()
    expect(
      screen.queryByText('Detached block storage volume requires review'),
    ).toBeNull()
  })

  it('AWS → Rackspace: AWS cards never render under Rackspace; loading state appears first', async () => {
    setProviders('AWS')
    let releaseRackspace: ((value: Response) => void) | null = null
    const rackspacePending = new Promise<Response>((resolve) => {
      releaseRackspace = resolve
    })
    installTransport(
      routesHandler(
        () => rackspacePending,
        async () => mockJson(200, makeAwsResponse()),
      ),
    )
    const { container } = wrap(
      <>
        <ProviderProbe />
        <OptimizationPage />
      </>,
    )

    expect(await screen.findByText('Unattached EBS volume vol-001')).toBeInTheDocument()

    act(() => {
      providerProbe!.setProvider('RACKSPACE')
    })
    await waitFor(() => {
      expect(screen.queryByText('Unattached EBS volume vol-001')).toBeNull()
    })
    expect(
      screen.queryByText('Detached block storage volume requires review'),
    ).toBeNull()
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)

    await act(async () => {
      releaseRackspace!(mockJson(200, makeRackspaceRecsResponse()))
    })
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Unattached EBS volume vol-001')).toBeNull()
  })

  it('Rackspace → Azure: Rackspace cards never render under Azure', async () => {
    setProviders('RACKSPACE')
    let releaseAzure: ((value: Response) => void) | null = null
    const azurePending = new Promise<Response>((resolve) => {
      releaseAzure = resolve
    })
    installTransport(
      routesHandler(
        async () => mockJson(200, makeRackspaceRecsResponse()),
        undefined,
        () => azurePending,
      ),
    )
    const { container } = wrap(
      <>
        <ProviderProbe />
        <OptimizationPage />
      </>,
    )

    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()

    act(() => {
      providerProbe!.setProvider('AZURE')
    })
    await waitFor(() => {
      expect(
        screen.queryByText('Detached block storage volume requires review'),
      ).toBeNull()
    })
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)

    await act(async () => {
      releaseAzure!(mockJson(200, makeAzureResponse()))
    })
    expect(await screen.findByText('Rightsize azure-vm-01')).toBeInTheDocument()
    expect(
      screen.queryByText('Detached block storage volume requires review'),
    ).toBeNull()
  })

  it('Azure → Rackspace: Azure cards never render under Rackspace', async () => {
    setProviders('AZURE')
    let releaseRackspace: ((value: Response) => void) | null = null
    const rackspacePending = new Promise<Response>((resolve) => {
      releaseRackspace = resolve
    })
    installTransport(
      routesHandler(
        () => rackspacePending,
        undefined,
        async () => mockJson(200, makeAzureResponse()),
      ),
    )
    const { container } = wrap(
      <>
        <ProviderProbe />
        <OptimizationPage />
      </>,
    )

    expect(await screen.findByText('Rightsize azure-vm-01')).toBeInTheDocument()

    act(() => {
      providerProbe!.setProvider('RACKSPACE')
    })
    await waitFor(() => {
      expect(screen.queryByText('Rightsize azure-vm-01')).toBeNull()
    })
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0)

    await act(async () => {
      releaseRackspace!(mockJson(200, makeRackspaceRecsResponse()))
    })
    expect(
      await screen.findByText('Detached block storage volume requires review'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Rightsize azure-vm-01')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Source-level contract assertions (mirrors phase7c1 style)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace recommendations view contract', () => {
  it('routes RACKSPACE through the canonical provider-neutral pipeline', () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const fs = require('node:fs') as typeof import('node:fs')
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const path = require('node:path') as typeof import('node:path')
    const read = (rel: string) =>
      fs.readFileSync(path.resolve(__dirname, '..', rel), 'utf-8')

    const page = read('pages/OptimizationPage.tsx')
    // Provider-prefixed cache key from the shared provider context.
    expect(page).toMatch(/cacheProvider\s*=/)
    expect(page).toMatch(/provider === 'RACKSPACE' \? 'rackspace' : 'aws'/)
    expect(page).toMatch(/buildCloudRecommendationsCacheKey\(/)
    expect(page).toMatch(/invalidateFinopsCache\(\)/)
    expect(page).toMatch(/prevProviderRef/)
    // The advisory detail renders canonical extension fields without
    // reinterpreting semantics.
    expect(page).toMatch(/recommendation-recommended-action/)
    expect(page).toMatch(/recommendation-evidence/)
    expect(page).toMatch(/recommendation-limitations/)
    // No direct Rackspace engine / product API usage from the browser.
    expect(page).not.toMatch(/api\.rackspacecloud\.com/)
    expect(page).not.toMatch(/X-Auth-Token/)
    expect(page).not.toMatch(/evaluate_rackspace_optimization/)

    const model = read('lib/finops/unifiedRecommendations.ts')
    // The shared view model already understands Rackspace sources
    // and attribution (Phase 9D.1 Batch A).
    expect(model).toMatch(/RACKSPACE_OPTIMIZATION/)
    expect(model).toMatch(/All Rackspace Cloud Accounts/)
  })
})
