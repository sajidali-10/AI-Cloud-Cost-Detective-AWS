// Phase 6B — Costs page tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { AccountProvider } from '../lib/accountContext'
import { ProviderProvider } from '../lib/providerContext'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { CostsPage } from '../pages/CostsPage'
import { MemoryRouter } from '../lib/router'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function wrap(node: React.ReactNode) {
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

// CostsPage uses useFinopsQuery for the cost report; the store
// notifies React via useSyncExternalStore (React 18) after the
// mocked fetch resolves.  findBy* / waitFor keep every DOM
// assertion inside an act() boundary (RTL wraps both automatically).

// Phase 9D.2 — the Costs page routes EVERY provider (AWS included)
// through the canonical /api/cloud/costs envelope.  The AWS fixtures
// below are canonical CloudCostReport bodies (adapted to the legacy
// CostReportResponse shape by the page) — the legacy /api/aws/costs
// registry-primary-key grammar must never be queried by the UI.
function makeAwsCloudCosts(
  totalCost: string,
  previousCost: string,
  changePercent: string | null = '0.05',
) {
  return {
    report: {
      provider: 'AWS',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      currency_status: 'OK',
      account_id: null,
      account_display_name: null,
      period: { start: '2026-09-08', end: '2026-10-08', days: 30 },
      previous_period: { start: '2026-08-09', end: '2026-09-07', days: 30 },
      currency: 'USD',
      total_cost: totalCost,
      previous_period_cost: previousCost,
      change_amount: '10.00',
      change_percent: changePercent,
      estimated: false,
      daily_trend: [
        { date: '2026-10-01', amount: '80.00', unit: 'USD' },
        { date: '2026-10-02', amount: '90.00', unit: 'USD' },
      ],
      by_service: [
        { service: 'EC2', amount: '1500.00', unit: 'USD' },
        { service: 'RDS', amount: '500.00', unit: 'USD' },
        { service: 'S3', amount: '100.00', unit: 'USD' },
      ],
      by_region: [
        { region: 'us-east-1', amount: '2300.00', unit: 'USD' },
        { region: 'global', amount: '20.00', unit: 'USD' },
      ],
      by_account: [],
      account_errors: [],
      successful_accounts: 1,
      failed_accounts: 0,
      source: 'AWS_COST_EXPLORER',
    },
    cache_status: 'HIT',
    cached_at: '2026-10-08T14:42:00Z',
    expires_at: null,
  }
}

describe('CostsPage', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
  })

  it('renders real period data with KPI tiles', async () => {
    const calls: string[] = []
    const transport = vi.fn(async (path: string) => {
      calls.push(path)
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/costs')) {
        // Phase 9D.2 — AWS individual/aggregate scope must speak the
        // canonical selector contract: provider=AWS on
        // /api/cloud/costs, never the legacy /api/aws/costs route.
        expect(path).toContain('provider=AWS')
        return mockJson(200, makeAwsCloudCosts('1500.00', '1000.00', '0.5'))
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)

    await waitFor(() => {
      expect(screen.getByText('$1.50K')).toBeInTheDocument()
    })
    expect(await screen.findByText('+50.0%')).toBeInTheDocument()
    expect(calls.some((c) => c.startsWith('/api/aws/costs'))).toBe(false)
  })

  it('renders "—" for missing previous cost (no divide by zero)', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/costs')) return mockJson(200, makeAwsCloudCosts('100', '0', null))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)

    await waitFor(() => {
      const dashEls = Array.from(document.querySelectorAll('*')).filter(
        (el) => el.children.length === 0 && el.textContent === '—',
      )
      expect(dashEls.length).toBeGreaterThan(0)
    })
  })

  it('renders the Cost Trend chart with daily points', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/costs')) return mockJson(200, makeAwsCloudCosts('1500.00', '1000.00'))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)
    expect(await screen.findByTestId('cost-trend-chart')).toBeInTheDocument()
  })

  it('period selector changes month and triggers refetch', async () => {
    const calls: string[] = []
    const transport = vi.fn(async (path: string) => {
      calls.push(path)
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/costs')) return mockJson(200, makeAwsCloudCosts('1500.00', '1000.00'))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    const { container } = wrap(<CostsPage />)
    await waitFor(() => {
      // Phase 7A: costs endpoint is hit with ?month=YYYY-MM.
      expect(calls.some((c) => /month=\d{4}-\d{2}/.test(c))).toBe(true)
    })
    const select = container.querySelector('select[data-testid="period-selector-input"]') as HTMLSelectElement
    expect(select).not.toBeNull()
    const options = Array.from(select.querySelectorAll('option')) as HTMLOptionElement[]
    expect(options.length).toBeGreaterThan(1)
    const target = options[options.length - 2].value
    const user = userEvent.setup()
    await user.selectOptions(select, target)
    await waitFor(() => {
      expect(calls.some((c) => c.includes(`month=${target}`))).toBe(true)
    })
  })

  // Phase 9D.1 — Rackspace renders through the canonical
  // /api/cloud/costs route with honest billing-cycle semantics:
  // the ACTUAL source period is labelled as a billing cycle (never
  // a calendar month), and unsupported dimensions (daily trend,
  // regional breakdown, previous-period comparison) render a
  // professional unavailable state instead of fabricated zeros.
  it('renders Rackspace costs with billing-cycle wording and unavailable dimensions', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'RACKSPACE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    const calls: string[] = []
    const rackspaceEnvelope = {
      report: {
        provider: 'RACKSPACE',
        scope: 'all',
        scope_kind: 'all',
        status: 'SUCCESS',
        currency_status: 'OK',
        account_id: null,
        account_display_name: null,
        period: { start: '2026-10-01', end: '2026-11-01', days: 31 },
        previous_period: { start: '2026-09-01', end: '2026-10-01', days: 30 },
        currency: 'USD',
        total_cost: '2475.50',
        previous_period_cost: null,
        change_amount: null,
        change_percent: null,
        estimated: true,
        daily_trend: [],
        by_service: [
          { service: 'Cloud Servers', amount: '1500.25', unit: 'USD' },
          { service: 'Cloud Files', amount: '975.25', unit: 'USD' },
        ],
        by_region: [],
        by_account: [
          { account_id: '123456', display_name: 'Rackspace Cloud Account', amount: '2475.50', currency: 'USD' },
        ],
        account_errors: [],
        successful_accounts: 1,
        failed_accounts: 0,
        source: 'RACKSPACE_BILLING',
        previous_period_unavailable: true,
      },
      cache_status: 'MISS',
      cached_at: '2026-10-26T10:00:00Z',
      expires_at: null,
    }
    const transport = vi.fn(async (path: string) => {
      calls.push(path)
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/accounts/selectable/')) {
        return mockJson(200, { items: [], count: 0 })
      }
      if (path.startsWith('/api/cloud/costs')) {
        // Must be the canonical provider-neutral route with
        // provider=RACKSPACE — never a legacy AWS route.
        expect(path).toContain('provider=RACKSPACE')
        return mockJson(200, rackspaceEnvelope)
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)

    // Current billing-cycle total renders.
    expect(await screen.findByText('$2.48K')).toBeInTheDocument()
    // The page header / KPI label the ACTUAL source cycle — never a
    // calendar month name.
    const cycleLabels = await screen.findAllByText('Billing cycle Oct 1, 2026 – Oct 31, 2026')
    expect(cycleLabels.length).toBeGreaterThan(0)
    // Unsupported dimensions render the professional unavailable
    // state — no fabricated chart, no zero-filled regions.
    expect(screen.getByTestId('rackspace-daily-trend-unavailable')).toBeInTheDocument()
    expect(screen.getByTestId('rackspace-region-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('cost-trend-chart')).not.toBeInTheDocument()
    expect(screen.getByText('Not available for the current Rackspace Billing integration. Rackspace Billing reports estimated charges for the current billing cycle without a daily breakdown.')).toBeInTheDocument()
    // Previous-period comparison is unsupported — an honest
    // "unavailable", not a fabricated 0%.
    expect(screen.getByText('Previous period unavailable')).toBeInTheDocument()
    // The service breakdown (an authoritative dimension) renders.
    expect(screen.getAllByText('Cloud Servers').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Cloud Files').length).toBeGreaterThan(0)
    // Only the canonical route was queried for costs.
    expect(calls.some((c) => c.startsWith('/api/aws/costs'))).toBe(false)
  })
})
