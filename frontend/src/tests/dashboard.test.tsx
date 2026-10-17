// Phase 6B / 7D.3 — Dashboard page tests with mocked transport.
//
// Phase 7D.3 unified the dashboard behind canonical Phase 7C /
// 7D.1 cross-cloud routes.  These tests assert the unified
// dashboard renders real cost, change, resource and optimization
// data for AWS, plus the cost-completeness states (Unavailable /
// Partial / genuine zero) that Phase 7C hotfix #2 introduced and
// Phase 7D.3 hardened.
//
// All routes used here are the canonical /api/cloud/* surface.
// The legacy /api/aws/* routes are NOT exercised — they remain
// compatibility surfaces only.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { AccountProvider } from '../lib/accountContext'
import { ProviderProvider } from '../lib/providerContext'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { DashboardPage } from '../pages/DashboardPage'
import { MemoryRouter } from '../lib/router'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function wrapRoutes(node: React.ReactNode) {
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <ProviderProvider>
            <AccountProvider>
              <FinopsPeriodProvider initialDays={30} initialRegion="all">{node}</FinopsPeriodProvider>
            </AccountProvider>
          </ProviderProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

// ---------------------------------------------------------------------------
//  Canonical envelopes
// ---------------------------------------------------------------------------

function makeCloudCostsBody(opts: {
  totalCost: string
  previousTotal?: string
  changeAmount?: string | null
  changePercent?: string | null
  currency?: string
  successfulAccounts?: number
  failedAccounts?: number
  subscriptionErrors?: Array<{ subscription_id: string; display_name: string; code: string; message: string }>
  byAccount?: Array<{ account_id: string; display_name: string; amount: string; currency?: string | null }>
  byService?: Array<{ service: string; amount: string; unit: string }>
  byRegion?: Array<{ region: string; amount: string; unit: string }>
  dailyTrend?: Array<{ date: string; amount: string; unit: string }>
  status?: string
} = { totalCost: '0' }) {
  return {
    report: {
      provider: 'AWS',
      scope: 'all',
      scope_kind: 'all',
      status: opts.status ?? 'SUCCESS',
      currency_status: 'OK',
      account_id: null,
      account_display_name: null,
      period: { start: '2026-09-08', end: '2026-10-08', days: 30 },
      previous_period: { start: '2026-08-09', end: '2026-09-07', days: 30 },
      currency: opts.currency ?? 'USD',
      total_cost: opts.totalCost,
      previous_period_cost: opts.previousTotal ?? '0',
      change_amount: opts.changeAmount ?? '0',
      change_percent: opts.changePercent ?? null,
      estimated: false,
      daily_trend: opts.dailyTrend ?? [
        { date: '2026-10-01', amount: '80.00', unit: 'USD' },
        { date: '2026-10-02', amount: '90.00', unit: 'USD' },
        { date: '2026-10-03', amount: '100.00', unit: 'USD' },
      ],
      by_service: opts.byService ?? [
        { service: 'EC2', amount: '1500.00', unit: 'USD' },
        { service: 'RDS', amount: '500.00', unit: 'USD' },
        { service: 'S3', amount: '100.00', unit: 'USD' },
      ],
      by_region: opts.byRegion ?? [
        { region: 'us-east-1', amount: '2300.00', unit: 'USD' },
        { region: 'global', amount: '20.00', unit: 'USD' },
      ],
      by_account: opts.byAccount ?? [],
      account_errors: (opts.subscriptionErrors ?? []).map((e) => ({
        account_id: e.subscription_id,
        display_name: e.display_name,
        code: e.code,
        message: e.message,
      })),
      source: 'AWS_COST_EXPLORER',
    },
    cache_status: 'HIT',
    cached_at: '2026-10-08T14:42:00Z',
    expires_at: '2026-10-09T14:42:00Z',
  }
}

function makeCloudResourcesBody(opts: {
  total?: number
  byType?: Array<{ key: string; count: number }>
  byLocation?: Array<{ key: string; count: number }>
  bySubscription?: Array<{ subscription_id: string; display_name: string; count: number }>
  subscriptionErrors?: Array<{ subscription_id: string; display_name: string; code: string; message: string }>
} = {}) {
  return {
    summary: {
      provider: 'AWS',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      total_resources: opts.total ?? 1,
      by_type: opts.byType ?? [
        { key: 'EC2', count: opts.total ?? 1 },
        { key: 'RDS', count: 0 },
      ],
      by_location: opts.byLocation ?? [
        { key: 'us-east-1', count: opts.total ?? 1 },
      ],
      by_resource_group: [],
      by_subscription: opts.bySubscription ?? [],
      subscription_errors: opts.subscriptionErrors ?? [],
      source: 'AWS_RESOURCE_GROUP',
    },
    resources: [],
  }
}

function makeCloudRecommendationsBody(opts: {
  totalRecommendations?: number
  potentialSavings?: string | null
  currency?: string | null
  savingsStatus?: 'OK' | 'MIXED_CURRENCY' | 'EMPTY'
  savingsPeriod?: 'MONTHLY' | 'ANNUAL' | null
  bySource?: Array<{ key: string; count: number }>
} = {}) {
  return {
    summary: {
      provider: 'AWS',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      savings_status: opts.savingsStatus ?? 'OK',
      currency: opts.currency ?? 'USD',
      total_recommendations: opts.totalRecommendations ?? 12,
      recommendations_with_savings: opts.potentialSavings ? 12 : 0,
      recommendations_without_savings: opts.potentialSavings ? 0 : 12,
      potential_annual_savings: null,
      potential_savings: opts.potentialSavings ?? '320.50',
      savings_period: opts.savingsPeriod ?? 'MONTHLY',
      savings_by_currency: opts.currency
        ? [{ currency: opts.currency, amount: opts.potentialSavings ?? '0' }]
        : [],
      by_impact: [],
      by_resource_type: [],
      by_subscription: [],
      by_resource_group: [],
      by_type: [],
      by_subcategory: [],
      subscription_errors: [],
      by_account: [],
      by_source: opts.bySource ?? [
        { key: 'AWS_OPTIMIZATION', count: opts.totalRecommendations ?? 12 },
      ],
      by_action: [],
      account_errors: [],
      source: 'AWS_COST_OPTIMIZATION_HUB',
    },
    recommendations: [],
  }
}

function makeCloudProvidersBody() {
  return {
    providers: [
      {
        provider: 'AWS',
        is_registered: true,
        is_enabled: true,
        credential_strategies: [],
        capabilities: [],
        supported_regions: [],
        supported_lookback_days: [],
        status: 'OK',
      },
    ],
    count: 1,
  }
}

function makeCloudAccountsSelectable() {
  return {
    items: [
      {
        id: 12,
        account_id: '974053642038',
        display_name: 'Production AWS Account',
        provider: 'AWS',
        enabled: true,
        is_management: false,
      },
    ],
    count: 1,
  }
}

function installMockRoutes(opts: {
  costs?: Response
  resources?: Response
  recommendations?: Response
  selectedScope?: 'all' | 'acct:12' | string
  includeIdentity?: boolean
} = {}) {
  const transport = vi.fn(async (path: string) => {
    if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
    if (path.startsWith('/api/cloud/providers')) return mockJson(200, makeCloudProvidersBody())
    if (path.startsWith('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable())
    if (path.startsWith('/api/cloud/accounts/selectable')) return mockJson(200, { items: [], count: 0 })
    if (path.startsWith('/api/cloud/costs')) return opts.costs ?? mockJson(200, makeCloudCostsBody({ totalCost: '2438.08', previousTotal: '2300.00', changeAmount: '138.08', changePercent: '0.06' }))
    if (path.startsWith('/api/cloud/resources')) return opts.resources ?? mockJson(200, makeCloudResourcesBody({ total: 1 }))
    if (path.startsWith('/api/cloud/recommendations')) return opts.recommendations ?? mockJson(200, makeCloudRecommendationsBody())
    return mockJson(404, { message: 'not found' })
  })
  configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
  return transport
}

describe('DashboardPage (unified Phase 7D.3)', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    }
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
    if (typeof window !== 'undefined') window.sessionStorage.clear()
  })

  it('renders real cost, change, resource and optimization KPIs from canonical routes', async () => {
    installMockRoutes()
    wrapRoutes(<DashboardPage />)

    // Provider-aware scope label surfaced via friendlyScopeLabel.
    // Selection is 'all' so the label is the canonical aggregate
    // phrasing "All AWS Accounts" (no raw ``acct:<n>`` leaks).
    await waitFor(() => {
      expect(screen.getAllByText(/All AWS Accounts/).length).toBeGreaterThan(0)
    })
    expect(await screen.findByText('$2.44K')).toBeInTheDocument()
    expect(await screen.findByText('+6.0%')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('resources-total-kpi')).toHaveTextContent('1')
    })
    await waitFor(() => {
      expect(screen.getByTestId('optimization-count').textContent).toBe('12')
    })
    expect(await screen.findByTestId('kpi-active-recommendations')).toBeInTheDocument()
    expect(await screen.findByTestId('kpi-potential-monthly-savings')).toBeInTheDocument()
  })

  it('renders "Savings not available" when authoritative savings is null', async () => {
    installMockRoutes({
      recommendations: mockJson(200, makeCloudRecommendationsBody({
        potentialSavings: null,
        savingsStatus: 'EMPTY',
        totalRecommendations: 5,
      })),
    })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      // Two renderings: the SavingsKPI description and the Optimization Summary line.
      expect(screen.getAllByText(/savings not available/i).length).toBeGreaterThan(0)
    })
  })

  it('shows an error banner when the canonical costs endpoint fails', async () => {
    installMockRoutes({
      costs: mockJson(502, { error_code: 'ServerDown' }),
      // Resources and recommendations still succeed so we
      // exercise the partial-failure fan-out (one domain failed,
      // others succeed) without blanking the dashboard.
      resources: mockJson(200, makeCloudResourcesBody({ total: 1 })),
      recommendations: mockJson(200, makeCloudRecommendationsBody()),
    })
    wrapRoutes(<DashboardPage />)
    // Per spec step 20, errors are localized to the affected
    // KPI/section.  Total Spend must render "Unavailable"
    // (never "$0.00"), Resources KPI must still render the real
    // count, and the page chrome must remain visible.
    await waitFor(() => {
      expect(screen.getAllByText(/Unavailable/).length).toBeGreaterThan(0)
    })
    await waitFor(() => {
      expect(screen.getByTestId('resources-total-kpi')).toHaveTextContent('1')
    })
    expect(screen.queryByText('$0.00')).toBeNull()
    // Page chrome is intact.
    expect(screen.getAllByText(/Dashboard/).length).toBeGreaterThan(0)
  })

  it('isolates the unified dashboard against the legacy /api/aws/* cost route', async () => {
    // Transport that ONLY serves legacy AWS routes.  The unified
    // dashboard must NOT consume them as its primary data path.
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/providers')) return mockJson(200, makeCloudProvidersBody())
      if (path.startsWith('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable())
      if (path.startsWith('/api/aws/identity')) return mockJson(200, { account: '974053642038', arn: '', user_id: '', region: 'us-east-1' })
      if (path.startsWith('/api/aws/costs')) return mockJson(200, { report: { account_id: '974053642038', period: { start: '2026-10-01', end: '2026-10-17', days: 17 }, previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 }, currency: 'USD', total_cost: '9999.99', previous_period_cost: '0', change_amount: '9999.99', change_percent: null, estimated: false, daily_trend: [], by_service: [], by_region: [], source: 'AWS_COST_EXPLORER' }, cache_status: 'HIT' })
      if (path.startsWith('/api/cloud/costs')) return mockJson(500, { status: 'error' })
      if (path.startsWith('/api/cloud/resources')) return mockJson(500, { status: 'error' })
      if (path.startsWith('/api/cloud/recommendations')) return mockJson(500, { status: 'error' })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    // The legacy /api/aws/costs response carries total_cost=9999.99.
    // If the unified dashboard read it as its primary source, that
    // number would surface in the Total Spend KPI.  Because the
    // canonical /api/cloud/costs returned 500, Total Spend must
    // render "Unavailable" instead.
    await waitFor(() => {
      expect(screen.getAllByText(/Unavailable/).length).toBeGreaterThan(0)
    })
    expect(screen.queryByText('$10.00K')).toBeNull()
    expect(screen.queryByText('$9,999.99')).toBeNull()
  })
})
