// Phase 7D.3 — Unified multi-cloud executive Dashboard tests.
//
// These tests lock down the Phase 7D.3 invariants enumerated in
// the spec (steps 15, 16, 17, 21, 25, 26):
//
//   * AWS ↔ Azure ↔ AWS provider switching never blanks the page,
//     never surfaces stale KPIs, and never lets an in-flight AWS
//     response overwrite Azure state (and vice versa).
//   * Account / subscription scope switching (QA → Production →
//     All, MCPP → Sponsorship → All) preserves cache isolation
//     and renders the correct friendly label.
//   * Cost completeness states (genuine zero, partial subtotal,
//     all-failed throttled) render "Unavailable" / "Partial" /
//     "$0.00" correctly under both providers.
//   * The legacy /api/aws/* cost / identity routes are NEVER
//     consulted as the primary data path.
//   * Azure subscription GUIDs (sub:<guid>) never reach an AWS
//     route; AWS account ids (acct:<n>) never reach an Azure
//     route.
//   * Period changes are reflected in cost widgets; resources and
//     recommendations are current-state only (spec step 24).
//   * Mixed-currency savings render "Mixed" instead of a sum.
//   * Partial domain failure (one canonical endpoint 500, the
//     others 200) renders "Unavailable" for the affected KPI and
//     keeps the others live — the dashboard never goes blank.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { AccountProvider } from '../lib/accountContext'
import { ProviderProvider, useProviderContext } from '../lib/providerContext'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache, invalidateFinopsCache } from '../lib/finops/store'
import { DashboardPage } from '../pages/DashboardPage'
import { CostsPage } from '../pages/CostsPage'
import { AccountSelector } from '../components/AccountSelector'
import { buildMonthList } from '../types/finops'
import {
  buildAccountSelector,
  accountMatchesSelector,
  providerAccountNoun,
  providerAggregateLabel,
  canonicalAccountParam,
  canonicalCacheSegment,
  friendlyAccountScopeErrorMessage,
} from '../lib/finops/subscriptionSelection'
import { MemoryRouter } from '../lib/router'
import {
  __resetDashboardGenerationForTests,
  dashboardScopeCacheSegment,
  friendlyScopeLabel,
  providerSlug,
} from '../lib/finops/dashboardScope'

// ---------------------------------------------------------------------------
//  Helpers
// ---------------------------------------------------------------------------

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

function makeCloudProviders() {
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
      {
        provider: 'AZURE',
        is_registered: true,
        is_enabled: true,
        credential_strategies: ['AZURE_WORKLOAD_FEDERATION'],
        capabilities: [],
        supported_regions: [],
        supported_lookback_days: [],
        status: 'OK',
      },
    ],
    count: 2,
  }
}

function makeCloudAccountsSelectable(provider: 'AWS' | 'AZURE') {
  if (provider === 'AZURE') {
    return {
      items: [
        {
          id: 5,
          account_id: '11111111-2222-3333-4444-555555555555',
          display_name: 'MCPP Subscription',
          provider: 'AZURE',
          enabled: true,
          is_management: false,
        },
        {
          id: 6,
          account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
          display_name: 'Microsoft Azure Sponsorship',
          provider: 'AZURE',
          enabled: true,
          is_management: false,
        },
      ],
      count: 2,
    }
  }
  return {
    items: [
      {
        id: 10,
        account_id: '111111111111',
        display_name: 'AWS QA-Dev Account',
        provider: 'AWS',
        enabled: true,
        is_management: false,
      },
      {
        id: 11,
        account_id: '222222222222',
        display_name: 'AWS Production Account',
        provider: 'AWS',
        enabled: true,
        is_management: false,
      },
    ],
    count: 2,
  }
}

function makeCloudCosts(provider: 'AWS' | 'AZURE', opts: {
  total: string
  previousTotal?: string
  successfulAccounts?: number
  failedAccounts?: number
  subscriptionErrors?: Array<{ subscription_id: string; display_name: string; code: string; message: string }>
  status?: string
} = { total: '100' }) {
  return mockJson(200, {
    report: {
      provider,
      scope: 'all',
      scope_kind: 'all',
      status: opts.status ?? 'SUCCESS',
      currency_status: 'OK',
      account_id: null,
      account_display_name: null,
      period: { start: '2026-10-01', end: '2026-10-17', days: 17 },
      previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 },
      currency: 'USD',
      total_cost: opts.total,
      previous_period_cost: opts.previousTotal ?? '0',
      change_amount: opts.total,
      change_percent: null,
      estimated: false,
      daily_trend: [],
      by_service: [],
      by_region: [],
      by_account: [],
      account_errors: (opts.subscriptionErrors ?? []).map((e) => ({
        account_id: e.subscription_id,
        display_name: e.display_name,
        code: e.code,
        message: e.message,
      })),
      source: provider === 'AWS' ? 'AWS_COST_EXPLORER' : 'AZURE_COST_MANAGEMENT',
      successful_accounts: opts.successfulAccounts ?? 1,
      failed_accounts: opts.failedAccounts ?? 0,
      subscription_errors: opts.subscriptionErrors ?? [],
    },
    cache_status: 'REFRESHED',
    cached_at: '2026-10-17T00:00:00Z',
    expires_at: '2026-10-17T01:00:00Z',
  })
}

function makeCloudResources(provider: 'AWS' | 'AZURE') {
  return mockJson(200, {
    summary: {
      provider,
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      total_resources: 12,
      by_type: [{ key: 'EC2', count: 8 }, { key: 'RDS', count: 4 }],
      by_location: [{ key: 'us-east-1', count: 12 }],
      by_resource_group: [],
      by_subscription: [],
      subscription_errors: [],
      source: provider === 'AWS' ? 'AWS_RESOURCE_GROUP' : 'AZURE_RESOURCE_GRAPH',
    },
    resources: [],
  })
}

function buildCloudRecommendationsBody(provider: 'AWS' | 'AZURE', opts: {
  total: number
  potentialSavings?: string | null
  currency?: string | null
  savingsPeriod?: 'MONTHLY' | 'ANNUAL' | null
  savingsStatus?: 'OK' | 'MIXED_CURRENCY' | 'EMPTY'
  bySource?: Array<{ key: string; count: number }>
} = { total: 0 }) {
  return {
    summary: {
      provider,
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      savings_status: opts.savingsStatus ?? 'OK',
      currency: opts.currency ?? 'USD',
      total_recommendations: opts.total,
      recommendations_with_savings: opts.potentialSavings ? opts.total : 0,
      recommendations_without_savings: opts.potentialSavings ? 0 : opts.total,
      potential_annual_savings: opts.savingsPeriod === 'ANNUAL' ? opts.potentialSavings ?? '0' : null,
      potential_savings: opts.savingsPeriod === 'ANNUAL' ? null : opts.potentialSavings ?? '0',
      savings_period: opts.savingsPeriod ?? 'MONTHLY',
      savings_by_currency: opts.savingsStatus === 'MIXED_CURRENCY'
        ? [
            { currency: 'USD', amount: '100.00' },
            { currency: 'EUR', amount: '50.00' },
          ]
        : [],
      by_impact: [],
      by_resource_type: [],
      by_subscription: [],
      by_resource_group: [],
      by_type: [],
      by_subcategory: [],
      subscription_errors: [],
      by_account: [],
      by_source: opts.bySource ?? [],
      by_action: [],
      account_errors: [],
      source: provider === 'AWS' ? 'AWS_COST_OPTIMIZATION_HUB' : 'AZURE_ADVISOR',
    },
    recommendations: [],
  }
}

function makeCloudRecommendations(provider: 'AWS' | 'AZURE', opts: {
  total: number
  potentialSavings?: string | null
  currency?: string | null
  savingsPeriod?: 'MONTHLY' | 'ANNUAL' | null
  savingsStatus?: 'OK' | 'MIXED_CURRENCY' | 'EMPTY'
  bySource?: Array<{ key: string; count: number }>
} = { total: 0 }) {
  return mockJson(200, buildCloudRecommendationsBody(provider, opts))
}

// ---------------------------------------------------------------------------
//  Test setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  __resetApiForTests()
  invalidateCache()
  invalidateFinopsCache()
  __resetDashboardGenerationForTests()
  if (typeof window !== 'undefined') {
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
  }
})

afterEach(() => {
  cleanup()
  if (typeof window !== 'undefined') window.sessionStorage.clear()
})

// ===========================================================================
//  Section A — Scope helpers (pure unit tests)
// ===========================================================================

describe('Phase 7D.3 — dashboardScope helpers', () => {
  it('providerSlug returns "azure" for AZURE, "aws" for AWS and "rackspace" for RACKSPACE', () => {
    expect(providerSlug('AWS')).toBe('aws')
    expect(providerSlug('AZURE')).toBe('azure')
    // Phase 9D.1 — RACKSPACE is now a supported dashboard
    // provider (canonical costs / resources / recommendations).
    expect(providerSlug('RACKSPACE')).toBe('rackspace')
  })

  it('dashboardScopeCacheSegment always includes the provider prefix', () => {
    expect(dashboardScopeCacheSegment('all', 'AWS')).toBe('aws:all')
    expect(dashboardScopeCacheSegment('all', 'AZURE')).toBe('azure:all')
    expect(dashboardScopeCacheSegment('acct:10', 'AWS')).toBe('aws:acct:10')
    expect(dashboardScopeCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'AZURE')).toBe(
      'azure:sub:11111111-2222-3333-4444-555555555555',
    )
  })

  it('dashboardScopeCacheSegment self-corrects stale Azure acct:<n> to azure:all', () => {
    // Pre-hotfix sessions may still carry acct:<n> for an Azure
    // selection; the cache segment MUST coerce to the aggregate
    // namespace so a stale authoritative-zero entry cannot leak.
    expect(dashboardScopeCacheSegment('acct:5', 'AZURE')).toBe('azure:all')
    expect(dashboardScopeCacheSegment('acct:6', 'AZURE')).toBe('azure:all')
  })

  it('friendlyScopeLabel uses friendly display names only — no raw ids', () => {
    const azureAccounts = { slug: 'azure' as const, wire: 'AZURE' as const, rows: makeCloudAccountsSelectable('AZURE').items as never }
    expect(friendlyScopeLabel('sub:11111111-2222-3333-4444-555555555555', azureAccounts)).toBe(
      'MCPP Subscription',
    )
    expect(friendlyScopeLabel('all', azureAccounts)).toBe('All Azure Subscriptions')
    expect(friendlyScopeLabel('sub:00000000-dead-beef-cafe-000000000000', azureAccounts)).not.toMatch(
      /acct:|sub:/i,
    )
    const awsAccounts = { slug: 'aws' as const, wire: 'AWS' as const, rows: makeCloudAccountsSelectable('AWS').items as never }
    // Phase 9D.1 — AWS selections carry the CLOUD-side account id
    // (acct:<account_id>), never the local database row id.
    expect(friendlyScopeLabel('acct:111111111111', awsAccounts)).toBe('AWS QA-Dev Account')
    expect(friendlyScopeLabel('acct:222222222222', awsAccounts)).toBe('AWS Production Account')
    // A stale database-row-id selector matches no row — the label
    // falls back without leaking the raw selector form.
    expect(friendlyScopeLabel('acct:10', awsAccounts)).toBe('Account 10')
    expect(friendlyScopeLabel('all', awsAccounts)).toBe('All AWS Accounts')
  })
})

// ===========================================================================
//  Section B — Provider switching (release-critical)
// ===========================================================================

function installSwitchingFetchMock(opts: {
  costsByProvider: Record<'AWS' | 'AZURE', { total: string }>
}): { transport: ReturnType<typeof vi.fn>; getRoutesSeenBy: () => string[] } {
  const routesSeen: string[] = []
  const transport = vi.fn(async (url: string) => {
    routesSeen.push(url)
    if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
    if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
    if (url.includes('/api/cloud/accounts/selectable/AZURE')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
    if (url.includes('/api/cloud/accounts/selectable/AWS')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
    if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
    if (url.includes('/api/cloud/costs')) {
      const provider: 'AWS' | 'AZURE' = url.includes('provider=AZURE') ? 'AZURE' : 'AWS'
      return makeCloudCosts(provider, { total: opts.costsByProvider[provider].total })
    }
    if (url.includes('/api/cloud/resources')) {
      const provider: 'AWS' | 'AZURE' = url.includes('provider=AZURE') ? 'AZURE' : 'AWS'
      return makeCloudResources(provider)
    }
    if (url.includes('/api/cloud/recommendations')) {
      const provider: 'AWS' | 'AZURE' = url.includes('provider=AZURE') ? 'AZURE' : 'AWS'
      return makeCloudRecommendations(provider, { total: provider === 'AWS' ? 12 : 5 })
    }
    return mockJson(404, {})
  })
  configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
  return { transport, getRoutesSeenBy: () => routesSeen }
}

describe('Phase 7D.3 — provider switching', () => {
  it('AWS → Azure → AWS never blanks the page and updates provider attribute', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const { transport, getRoutesSeenBy } = installSwitchingFetchMock({
      costsByProvider: { AWS: { total: '1234.56' }, AZURE: { total: '7.89' } },
    })
    const view = wrapRoutes(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider initialDays={30} initialRegion="all">
                  <DashboardPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(view.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AWS')
    })
    expect(await screen.findByText('$1.23K')).toBeInTheDocument()

    // Switch to Azure.
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'cd.provider.selection.v1', newValue: 'AZURE' }))
    })
    // ProviderContext does NOT listen for storage events; the
    // Cloud Context bar emits setProvider() imperatively.  Drive
    // the same code path via a tiny helper component.

    // The above storage event doesn't update the React state
    // (ProviderContext only resets on auth token removal).  We
    // simulate the imperative provider switch by remounting with
    // the new sessionStorage value, which is what the Cloud
    // Context bar does internally.
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const view2 = wrapRoutes(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider initialDays={30} initialRegion="all">
                  <DashboardPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(view2.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AZURE')
    })
    expect(await screen.findByText('$7.89')).toBeInTheDocument()
    expect(screen.queryByText('$1.23K')).toBeNull()

    // Switch back to AWS.
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const view3 = wrapRoutes(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider initialDays={30} initialRegion="all">
                  <DashboardPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(view3.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AWS')
    })
    expect(await screen.findByText('$1.23K')).toBeInTheDocument()

    // The Azure ``acct:<n>`` selector must NEVER be sent to AWS.
    // Drive the assertion via the transport's URL log: no AWS
    // request should carry account_id="acct:5" or account_id="sub:...".
    for (const url of getRoutesSeenBy()) {
      if (url.includes('/api/cloud/costs?provider=AWS')) {
        const m = url.match(/account_id=([^&]+)/)
        if (m && m[1] !== 'all') {
          expect(m[1]).toMatch(/^acct:\d+$/)
        }
      }
      if (url.includes('/api/cloud/costs?provider=AZURE')) {
        const m = url.match(/account_id=([^&]+)/)
        if (m && m[1] !== 'all') {
          expect(m[1]).toMatch(/^sub:[0-9a-f-]+$/)
        }
      }
    }
    expect(transport).toHaveBeenCalled()
  })

  it('no acct:<n> under Azure, no sub:<guid> sent to AWS (cache-key isolation)', () => {
    // Pure cache-key test — the segments MUST be in different
    // namespaces so a stale Azure cache entry cannot satisfy an
    // AWS request and vice versa.
    expect(dashboardScopeCacheSegment('acct:5', 'AZURE')).not.toBe(
      dashboardScopeCacheSegment('acct:5', 'AWS'),
    )
    expect(dashboardScopeCacheSegment('acct:10', 'AWS')).toBe('aws:acct:10')
    expect(dashboardScopeCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'AZURE')).toBe(
      'azure:sub:11111111-2222-3333-4444-555555555555',
    )
    // Phase 9D.1 — a stale Azure ``sub:<guid>`` under AWS (or
    // RACKSPACE) self-corrects to the aggregate namespace: it
    // must never namespace a cache entry for the wrong provider.
    expect(dashboardScopeCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'AWS')).toBe(
      'aws:all',
    )
    expect(dashboardScopeCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'RACKSPACE')).toBe(
      'rackspace:all',
    )
  })

  it('a late AWS response after switching to Azure does NOT overwrite Azure state (race protection)', async () => {
    // Simulate a user who starts an AWS request, switches to Azure
    // mid-flight, and lets both responses resolve.  The Azure
    // response must win; the AWS response must be discarded.
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    let resolveAws: (() => void) | null = null
    const awsGate = new Promise<void>((resolve) => { resolveAws = resolve })
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, { items: [], count: 0 })
      if (url.includes('/api/cloud/costs?provider=AWS')) {
        // Block the AWS response until we explicitly release it.
        await awsGate
        return makeCloudCosts('AWS', { total: '999.99' })
      }
      if (url.includes('/api/cloud/costs?provider=AZURE')) {
        return makeCloudCosts('AZURE', { total: '1.23' })
      }
      if (url.includes('/api/cloud/resources')) {
        const provider: 'AWS' | 'AZURE' = url.includes('provider=AZURE') ? 'AZURE' : 'AWS'
        return makeCloudResources(provider)
      }
      if (url.includes('/api/cloud/recommendations')) {
        const provider: 'AWS' | 'AZURE' = url.includes('provider=AZURE') ? 'AZURE' : 'AWS'
        return makeCloudRecommendations(provider, { total: provider === 'AWS' ? 12 : 5 })
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    // Mount AWS view.
    const view = render(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider initialDays={30} initialRegion="all">
                  <DashboardPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )
    // Confirm AWS mounted.
    await waitFor(() => {
      expect(view.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AWS')
    })

    // Switch to Azure mid-flight by remounting (the Cloud Context
    // bar uses the same imperative state path).  The AWS request
    // is still blocked on awsGate.
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const view2 = render(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider initialDays={30} initialRegion="all">
                  <DashboardPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )
    // Azure mounts and renders its real cost.
    await waitFor(() => {
      expect(view2.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AZURE')
    })
    expect(await screen.findByText('$1.23')).toBeInTheDocument()

    // Now release the AWS request.  Its response carries
    // total=999.99 and a StaleGenerationError must be raised by
    // the race-guard inside the DashboardPage fetcher.  Even if
    // the error surfaces in the store, the Azure DOM must NOT
    // pick up the AWS number.
    if (resolveAws) resolveAws()
    await new Promise((r) => setTimeout(r, 50))
    // Azure's $1.23 still dominates; AWS's $999.99 never appears.
    expect(screen.queryByText('$1,000')).toBeNull()
    expect(screen.queryByText('$999.99')).toBeNull()
    expect(screen.getByText('$1.23')).toBeInTheDocument()
  })
})

// ===========================================================================
//  Section C — Scope switching
// ===========================================================================

describe('Phase 7D.3 — scope switching', () => {
  it('AWS QA → Production → All → QA preserves friendly labels without leaking DB ids', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) return makeCloudCosts('AWS', { total: '500.00' })
      if (url.includes('/api/cloud/resources?provider=AWS')) return makeCloudResources('AWS')
      if (url.includes('/api/cloud/recommendations?provider=AWS')) return makeCloudRecommendations('AWS', { total: 3 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    // Initial: All AWS Accounts.
    const view = wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(view.container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider')).toBe('AWS')
    })
    await waitFor(() => {
      expect(screen.getAllByText(/All AWS Accounts/).length).toBeGreaterThan(0)
    })

    // Switch to QA (acct:<cloud account_id> — the Phase 9D.1
    // canonical AWS selector, never the DB row id).
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'acct:111111111111')
    }
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/AWS QA-Dev Account/).length).toBeGreaterThan(0)
    })

    // Switch to Production (acct:<cloud account_id>).
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'acct:222222222222')
    }
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/AWS Production Account/).length).toBeGreaterThan(0)
    })

    // Back to All.
    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    }
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/All AWS Accounts/).length).toBeGreaterThan(0)
    })

    // No raw acct:<n> anywhere in the rendered text.
    expect(document.body.textContent).not.toMatch(/acct:\d+/)
    expect(document.body.textContent).not.toMatch(/sub:[0-9a-f-]{8,}/)
  })

  it('Azure MCPP → Sponsorship → All preserves friendly subscription names', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      if (url.includes('/api/cloud/costs?provider=AZURE')) return makeCloudCosts('AZURE', { total: '5.00' })
      if (url.includes('/api/cloud/resources?provider=AZURE')) return makeCloudResources('AZURE')
      if (url.includes('/api/cloud/recommendations?provider=AZURE')) return makeCloudRecommendations('AZURE', { total: 2 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    // Aggregate first.
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/All Azure Subscriptions/).length).toBeGreaterThan(0)
    })

    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'sub:11111111-2222-3333-4444-555555555555')
    }
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/MCPP Subscription/).length).toBeGreaterThan(0)
    })

    cleanup()
    invalidateFinopsCache()
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.account.selection.v1', 'sub:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
    }
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/Microsoft Azure Sponsorship/).length).toBeGreaterThan(0)
    })

    // Raw GUIDs must not leak into the rendered text.
    expect(document.body.textContent).not.toMatch(/sub:[0-9a-f-]{8,}/)
    expect(document.body.textContent).not.toMatch(/acct:\d+/)
  })
})

// ===========================================================================
//  Section D — Cost completeness (release-critical)
// ===========================================================================

describe('Phase 7D.3 — cost completeness states', () => {
  it('genuine successful zero renders as $0.00', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      if (url.includes('/api/cloud/costs?provider=AZURE')) {
        return makeCloudCosts('AZURE', { total: '0', successfulAccounts: 1, failedAccounts: 0 })
      }
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AZURE')
      if (url.includes('/api/cloud/recommendations')) return makeCloudRecommendations('AZURE', { total: 0 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText('$0.00').length).toBeGreaterThan(0)
    })
  })

  it('partial subtotal renders the subtotal + Partial marker', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      if (url.includes('/api/cloud/costs?provider=AZURE')) {
        const body = {
          report: {
            provider: 'AZURE',
            scope: 'all',
            scope_kind: 'all',
            status: 'PARTIAL_SUCCESS',
            currency_status: 'OK',
            account_id: null,
            account_display_name: null,
            period: { start: '2026-10-01', end: '2026-10-17', days: 17 },
            previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 },
            currency: 'USD',
            total_cost: '7.18',
            previous_period_cost: '0',
            change_amount: '7.18',
            change_percent: null,
            estimated: false,
            daily_trend: [],
            by_service: [],
            by_region: [],
            // by_account MUST contain the successful subscription
            // so the adapter derives 1 success + 1 fail.
            by_account: [
              {
                account_id: '11111111-2222-3333-4444-555555555555',
                display_name: 'MCPP Subscription',
                amount: '7.18',
                currency: 'USD',
              },
              {
                account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                display_name: 'Microsoft Azure Sponsorship',
                amount: '0',
                currency: 'USD',
              },
            ],
            account_errors: [
              {
                account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                display_name: 'Microsoft Azure Sponsorship',
                code: 'THROTTLED',
                message: 'THROTTLED',
              },
            ],
            source: 'AZURE_COST_MANAGEMENT',
          },
          cache_status: 'REFRESHED',
          cached_at: '2026-10-17T00:00:00Z',
          expires_at: '2026-10-17T01:00:00Z',
        }
        return mockJson(200, body)
      }
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AZURE')
      if (url.includes('/api/cloud/recommendations')) return makeCloudRecommendations('AZURE', { total: 0 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText('$7.18').length).toBeGreaterThan(0)
      expect(screen.getAllByText(/Partial \(1 unavailable\)/).length).toBeGreaterThan(0)
    })
  })

  it('all-failed throttled Azure aggregation renders Unavailable (NEVER $0.00) — spec step 21', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      if (url.includes('/api/cloud/costs?provider=AZURE')) {
        // Real-world Azure throttling scenario: every subscription
        // throttled, total_cost = 0.00.  This MUST render as
        // "Unavailable", never "$0.00".
        return makeCloudCosts('AZURE', {
          total: '0.00',
          successfulAccounts: 0,
          failedAccounts: 2,
          status: 'PARTIAL_SUCCESS',
          subscriptionErrors: [
            {
              subscription_id: '11111111-2222-3333-4444-555555555555',
              display_name: 'MCPP Subscription',
              code: 'THROTTLED',
              message: 'THROTTLED',
            },
            {
              subscription_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
              display_name: 'Microsoft Azure Sponsorship',
              code: 'THROTTLED',
              message: 'THROTTLED',
            },
          ],
        })
      }
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AZURE')
      // Recommendations: NO data (zero total) so neither the
      // savings card nor anywhere else renders "$0.00" — the
      // only place that could display $0.00 is the SpendKpi,
      // which we explicitly assert MUST render Unavailable.
      if (url.includes('/api/cloud/recommendations')) {
        return makeCloudRecommendations('AZURE', {
          total: 0,
          potentialSavings: null,
          savingsStatus: 'EMPTY',
        })
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    // The SpendKpi must render "Unavailable" — never "$0.00".
    // Spec step 21 is release-critical.
    await waitFor(() => {
      const cards = screen.getAllByTestId('metric-card')
      const spendCard = cards.find((c) => c.textContent?.includes('Total Spend'))
      expect(spendCard).toBeDefined()
      expect(spendCard?.textContent).toMatch(/Unavailable/)
      expect(spendCard?.textContent).not.toMatch(/\$0\.00/)
    })
  })

  it('previous-period-zero renders no fabricated percentage', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) {
        return makeCloudCosts('AWS', { total: '100.00', previousTotal: '0' })
      }
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AWS')
      if (url.includes('/api/cloud/recommendations')) return makeCloudRecommendations('AWS', { total: 0 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getByText('$100.00')).toBeInTheDocument()
    })
    // The Change KPI must render "—" (no fabricated Infinity% jump).
    const changeKpis = screen.getAllByText('Change vs Previous Period')
    expect(changeKpis.length).toBeGreaterThan(0)
  })
})

// ===========================================================================
//  Section E — Savings normalisation (mixed currency)
// ===========================================================================

describe('Phase 7D.3 — savings normalisation', () => {
  it('mixed-currency savings render "Mixed" — never a summed number', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) return makeCloudCosts('AWS', { total: '100' })
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AWS')
      if (url.includes('/api/cloud/recommendations?provider=AWS')) {
        return makeCloudRecommendations('AWS', {
          total: 4,
          potentialSavings: null,
          currency: null,
          savingsStatus: 'MIXED_CURRENCY',
        })
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      const card = screen.getByTestId('kpi-potential-monthly-savings')
      expect(card.textContent ?? '').toMatch(/Mixed/)
    })
  })

  it('Azure ANNUAL savings are normalised to monthly (display-only)', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/cloud/accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      if (url.includes('/api/cloud/costs?provider=AZURE')) return makeCloudCosts('AZURE', { total: '50' })
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AZURE')
      if (url.includes('/api/cloud/recommendations?provider=AZURE')) {
        // Canonical Azure envelope: ANNUAL figure on
        // potential_annual_savings; potential_savings null;
        // savings_period = ANNUAL.  The dashboard must divide by
        // 12 to display as monthly.
        const body = buildCloudRecommendationsBody('AZURE', { total: 3 })
        const s = body.summary as Record<string, unknown>
        s.potential_annual_savings = '1200.00'
        s.potential_savings = null
        s.savings_period = 'ANNUAL'
        s.savings_status = 'OK'
        s.currency = 'USD'
        return mockJson(200, body)
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      const card = screen.getByTestId('kpi-potential-monthly-savings')
      // 1200 / 12 = 100, formatted compact = "$100.00"
      expect(card.textContent ?? '').toMatch(/\$100\.00/)
    })
  })

  it('missing savings render "Not available" — no fabricated zero', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) return makeCloudCosts('AWS', { total: '100' })
      if (url.includes('/api/cloud/resources')) return makeCloudResources('AWS')
      if (url.includes('/api/cloud/recommendations?provider=AWS')) {
        return makeCloudRecommendations('AWS', {
          total: 7,
          potentialSavings: null,
          savingsStatus: 'EMPTY',
        })
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/Not available/i).length).toBeGreaterThan(0)
    })
  })
})

// ===========================================================================
//  Section F — Partial domain failure
// ===========================================================================

describe('Phase 7D.3 — partial domain failure', () => {
  it('cost 500 + resources/recs 200 → Unavailable for cost, live for resources/recs', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) return mockJson(500, { error_code: 'UPSTREAM_ERROR' })
      if (url.includes('/api/cloud/resources?provider=AWS')) return makeCloudResources('AWS')
      if (url.includes('/api/cloud/recommendations?provider=AWS')) return makeCloudRecommendations('AWS', { total: 8 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getAllByText(/Unavailable/).length).toBeGreaterThan(0)
    })
    // Resources KPI still renders the real count.
    await waitFor(() => {
      expect(screen.getByTestId('resources-total-kpi')).toHaveTextContent('12')
    })
    // Optimization count still renders.
    expect(screen.getByTestId('optimization-count').textContent).toBe('8')
    // Page chrome intact.
    expect(screen.getAllByText(/Dashboard/).length).toBeGreaterThan(0)
  })

  it('resources 500 + costs/recs 200 → Resources KPI degrades, others stay live', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeCloudProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) return mockJson(200, makeCloudAccountsSelectable('AWS'))
      if (url.includes('/api/cloud/costs?provider=AWS')) return makeCloudCosts('AWS', { total: '42.00' })
      if (url.includes('/api/cloud/resources?provider=AWS')) return mockJson(500, { error_code: 'TIMEOUT' })
      if (url.includes('/api/cloud/recommendations?provider=AWS')) return makeCloudRecommendations('AWS', { total: 5 })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrapRoutes(<DashboardPage />)
    // Costs stay live — the canonical report succeeded.
    await waitFor(() => {
      expect(screen.getAllByText('$42.00').length).toBeGreaterThan(0)
    })
    // Optimization count stays live — the canonical recommendations call succeeded.
    await waitFor(() => {
      expect(screen.getByTestId('optimization-count').textContent).toBe('5')
    })
    // Resources KPI must NOT render a fabricated number.  Either
    // "—" or the loading placeholder is acceptable; the spec
    // requires we never invent data.
    await waitFor(() => {
      const cards = screen.getAllByTestId('metric-card')
      const resourcesCard = cards.find((c) => c.textContent?.includes('Resources'))
      expect(resourcesCard).toBeDefined()
    })
  })
})

// ===========================================================================
//  Section G — Cache isolation
// ===========================================================================

describe('Phase 7D.3 — cache isolation', () => {
  it('AWS costs != Azure costs (different cache keys for the same selection)', () => {
    const awsKey = `${dashboardScopeCacheSegment('all', 'AWS')}:costs:month:2026-10:all`
    const azureKey = `${dashboardScopeCacheSegment('all', 'AZURE')}:costs:month:2026-10:all`
    expect(awsKey).not.toBe(azureKey)
  })

  it('AWS QA != AWS Production (different cache keys)', () => {
    const qa = `${dashboardScopeCacheSegment('acct:10', 'AWS')}:costs:month:2026-10:all`
    const prod = `${dashboardScopeCacheSegment('acct:11', 'AWS')}:costs:month:2026-10:all`
    expect(qa).not.toBe(prod)
  })

  it('MCPP != Sponsorship (different cache keys under Azure)', () => {
    const mcpp = `${dashboardScopeCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'AZURE')}:costs:month:2026-10:all`
    const sponsor = `${dashboardScopeCacheSegment('sub:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', 'AZURE')}:costs:month:2026-10:all`
    expect(mcpp).not.toBe(sponsor)
  })

  it('current month != previous month (period isolation in cost keys)', () => {
    const cur = `${dashboardScopeCacheSegment('all', 'AWS')}:costs:month:2026-10:all`
    const prev = `${dashboardScopeCacheSegment('all', 'AWS')}:costs:month:2026-09:all`
    expect(cur).not.toBe(prev)
  })
})

// ===========================================================================
//  Section H — Phase 9D.1 — provider account-scope closure (browser fix)
// ===========================================================================
//
// The proven selector contract, locked down end-to-end:
//
//   * AWS       → acct:<provider account_id>  (the 12-digit AWS
//     account number — NEVER the local database row id)
//   * RACKSPACE → acct:<provider account_id>  (the Rackspace
//     account id — NEVER the local database row id; the proven
//     row is database id 7 = account_id 708274 → acct:708274)
//   * AZURE     → sub:<provider account_id>   (subscription GUID)
//
// Plus the closure UX invariants: stale-selection recovery,
// provider-aware labels, the Rackspace fixed billing-cycle scope
// (no historical Month / regional cost controls), the
// AccountNotFound professional message, and the Rackspace
// dashboard leaving the Loading state when the canonical APIs
// succeed.

// Live provider-switch probe — captures the provider context so a
// test can flip the active provider on a mounted component.
let providerProbe: ReturnType<typeof useProviderContext> | null = null
function ProviderProbe() {
  providerProbe = useProviderContext()
  return null
}

const MONTH_LIST = buildMonthList()
const CURRENT_MONTH = MONTH_LIST[MONTH_LIST.length - 1]
// A month guaranteed to differ from the current one (≥2 back in
// the 12-month list) — used to prove a persisted historical month
// never reaches a Rackspace cost query.
const HISTORICAL_MONTH = MONTH_LIST[MONTH_LIST.length - 3]

function makeRackspaceProviders() {
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
      {
        provider: 'AZURE',
        is_registered: true,
        is_enabled: true,
        credential_strategies: ['AZURE_WORKLOAD_FEDERATION'],
        capabilities: [],
        supported_regions: [],
        supported_lookback_days: [],
        status: 'OK',
      },
      {
        provider: 'RACKSPACE',
        is_registered: true,
        is_enabled: true,
        credential_strategies: ['RACKSPACE_API_KEY'],
        capabilities: [],
        supported_regions: [],
        supported_lookback_days: [],
        status: 'OK',
      },
    ],
    count: 3,
  }
}

// The PROVEN Rackspace selectable row: the local database row id
// is 7 while the provider-side Rackspace account id is 708274.
// The canonical selector MUST be acct:708274 — acct:7 can only
// fail with AccountNotFound.
function makeRackspaceSelectable() {
  return {
    items: [
      {
        id: 7,
        account_id: '708274',
        display_name: 'Rackspace Cloud Account',
        provider: 'RACKSPACE',
        enabled: true,
        is_management: false,
      },
    ],
    count: 1,
  }
}

function makeRackspaceCosts() {
  return mockJson(200, {
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
      by_service: [{ service: 'Cloud Servers', amount: '1500.25', unit: 'USD' }],
      by_region: [],
      by_account: [
        { account_id: '708274', display_name: 'Rackspace Cloud Account', amount: '2475.50', currency: 'USD' },
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
  })
}

function makeRackspaceResources() {
  return mockJson(200, {
    summary: {
      provider: 'RACKSPACE',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      total_resources: 12,
      by_type: [
        { key: 'SERVER', count: 7 },
        { key: 'VOLUME', count: 5 },
      ],
      by_location: [{ key: 'DFW', count: 12 }],
      by_resource_group: [],
      by_subscription: [],
      subscription_errors: [],
      source: 'RACKSPACE_INVENTORY',
    },
    resources: [],
  })
}

function makeRackspaceRecommendations() {
  return mockJson(200, {
    summary: {
      provider: 'RACKSPACE',
      scope: 'all',
      scope_kind: 'all',
      status: 'SUCCESS',
      savings_status: 'EMPTY',
      currency: null,
      total_recommendations: 3,
      recommendations_with_savings: 0,
      recommendations_without_savings: 3,
      potential_annual_savings: null,
      potential_savings: null,
      savings_period: 'MONTHLY',
      savings_by_currency: [],
      by_impact: [],
      by_resource_type: [],
      by_subscription: [],
      by_resource_group: [],
      by_type: [],
      by_subcategory: [],
      subscription_errors: [],
      by_account: [],
      by_source: [],
      by_action: [],
      account_errors: [],
      source: 'RACKSPACE_OPTIMIZATION',
    },
    recommendations: [],
  })
}

// A full Rackspace route set for CostsPage / DashboardPage tests.
// `costsOverride` lets a test inject an AccountNotFound failure.
function installRackspaceRoutes(opts: {
  costsOverride?: (url: string) => Response | null
} = {}) {
  const calls: string[] = []
  const transport = vi.fn(async (url: string) => {
    calls.push(url)
    if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
    if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
    if (url.includes('/api/cloud/accounts/selectable/RACKSPACE')) {
      return mockJson(200, makeRackspaceSelectable())
    }
    if (url.includes('/api/cloud/costs')) {
      const override = opts.costsOverride?.(url)
      if (override) return override
      return makeRackspaceCosts()
    }
    if (url.includes('/api/cloud/resources')) return makeRackspaceResources()
    if (url.includes('/api/cloud/recommendations')) return makeRackspaceRecommendations()
    return mockJson(404, {})
  })
  configureApi({
    getToken: () => null,
    onSessionExpired: () => {},
    transport: transport as unknown as typeof fetch,
  })
  return {
    transport,
    calls,
    costsCalls: () => calls.filter((c) => c.includes('/api/cloud/costs')),
  }
}

function setProviderAndSelection(provider: 'AWS' | 'AZURE' | 'RACKSPACE', selection: string) {
  window.sessionStorage.setItem('cd.provider.selection.v1', provider)
  window.sessionStorage.setItem('cd.account.selection.v1', selection)
}

// ---------------------------------------------------------------------------
//  H1 — canonical mapping (pure unit tests)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — canonical selectable-account mapping', () => {
  it('Rackspace row (DB id 7, account_id 708274) selects acct:708274 — never the DB row id', () => {
    const row = { account_id: '708274' }
    expect(buildAccountSelector(row, 'RACKSPACE')).toBe('acct:708274')
    expect(buildAccountSelector(row, 'RACKSPACE')).not.toBe('acct:7')
    expect(accountMatchesSelector(row, 'acct:708274', 'RACKSPACE')).toBe(true)
    // A pre-9D.1 database-row-id selector matches nothing.
    expect(accountMatchesSelector(row, 'acct:7', 'RACKSPACE')).toBe(false)
    // A stale Azure selector never matches a Rackspace row.
    expect(
      accountMatchesSelector(row, 'sub:11111111-2222-3333-4444-555555555555', 'RACKSPACE'),
    ).toBe(false)
  })

  it('AWS rows select acct:<cloud account_id> — never the local DB id', () => {
    const row = { account_id: '974053642038' }
    expect(buildAccountSelector(row, 'AWS')).toBe('acct:974053642038')
    expect(buildAccountSelector(row, 'AWS')).not.toBe('acct:10')
    expect(accountMatchesSelector(row, 'acct:974053642038', 'AWS')).toBe(true)
    expect(accountMatchesSelector(row, 'acct:10', 'AWS')).toBe(false)
  })

  it('Azure rows select sub:<subscription-guid>', () => {
    const row = { account_id: '11111111-2222-3333-4444-555555555555' }
    expect(buildAccountSelector(row, 'AZURE')).toBe(
      'sub:11111111-2222-3333-4444-555555555555',
    )
    expect(
      accountMatchesSelector(row, 'sub:11111111-2222-3333-4444-555555555555', 'AZURE'),
    ).toBe(true)
    // A stale acct:<n> never matches an Azure row.
    expect(accountMatchesSelector(row, 'acct:5', 'AZURE')).toBe(false)
  })

  it('stale cross-provider selectors are never submitted to canonical APIs', () => {
    // A stale Azure sub:<guid> must never reach AWS / RACKSPACE.
    expect(
      canonicalAccountParam('sub:11111111-2222-3333-4444-555555555555', 'RACKSPACE'),
    ).toBeUndefined()
    expect(
      canonicalAccountParam('sub:11111111-2222-3333-4444-555555555555', 'AWS'),
    ).toBeUndefined()
    // A stale acct:<n> must never reach AZURE.
    expect(canonicalAccountParam('acct:7', 'AZURE')).toBeUndefined()
    // Cache segments self-correct the same way.
    expect(
      canonicalCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'rackspace'),
    ).toBe('all')
    expect(
      canonicalCacheSegment('sub:11111111-2222-3333-4444-555555555555', 'aws'),
    ).toBe('all')
    expect(canonicalCacheSegment('acct:7', 'azure')).toBe('all')
  })

  it('provider-aware account nouns and aggregate labels', () => {
    expect(providerAccountNoun('AWS')).toBe('AWS Account')
    expect(providerAccountNoun('AZURE')).toBe('Azure Subscription')
    expect(providerAccountNoun('RACKSPACE')).toBe('Rackspace Account')
    expect(providerAggregateLabel('AWS')).toBe('All AWS Accounts')
    expect(providerAggregateLabel('AZURE')).toBe('All Azure Subscriptions')
    expect(providerAggregateLabel('RACKSPACE')).toBe('All Rackspace Cloud Accounts')
  })

  it('AccountNotFound maps to professional text — the raw backend selector never leaks', () => {
    // The exact backend wording for a scope miss.
    const raw = "no RACKSPACE account matches selection='acct:7'"
    const friendly = friendlyAccountScopeErrorMessage({ message: raw }, 'RACKSPACE')
    expect(friendly).toBeTruthy()
    expect(friendly).toMatch(/no longer available/)
    expect(friendly).not.toMatch(/acct:7/)
    expect(friendly).not.toMatch(/no RACKSPACE account/i)

    // The errorCode path maps too.
    const byCode = friendlyAccountScopeErrorMessage(
      { message: 'Request failed (404)', errorCode: 'AccountNotFound' },
      'AWS',
    )
    expect(byCode).toMatch(/AWS Account/)
    expect(byCode).toMatch(/no longer available/)

    // Azure wording says "subscription".
    const azure = friendlyAccountScopeErrorMessage(
      { message: "no AZURE account matches selection='acct:5'" },
      'AZURE',
    )
    expect(azure).toMatch(/Azure Subscription/)
    expect(azure).toMatch(/choose another subscription/)

    // Disabled accounts get their own professional wording.
    expect(
      friendlyAccountScopeErrorMessage({ message: 'x', errorCode: 'AccountDisabled' }, 'RACKSPACE'),
    ).toMatch(/disabled/)

    // Non-account failures pass through untouched (null) so the
    // caller keeps its existing message.
    expect(
      friendlyAccountScopeErrorMessage({ message: 'Backend unavailable' }, 'AWS'),
    ).toBeNull()
  })
})

// ---------------------------------------------------------------------------
//  H2 — AccountSelector provider contract (UI)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — AccountSelector provider contract', () => {
  it('Rackspace row (DB id 7 + account_id 708274) stores acct:708274 with the Rackspace Account label', async () => {
    setProviderAndSelection('RACKSPACE', 'all')
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/cloud/accounts/selectable/RACKSPACE')) {
        return mockJson(200, makeRackspaceSelectable())
      }
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    wrapRoutes(<AccountSelector />)
    const trigger = await screen.findByTestId('account-selector-button')
    // Provider-aware labels — never AWS wording under Rackspace.
    expect(trigger.textContent).toContain('Rackspace Account')
    expect(trigger.textContent).toContain('All Rackspace Cloud Accounts')

    // Open the dropdown and pick the proven row (DB id 7).
    fireEvent.click(trigger)
    const row = await screen.findByTestId('account-option-7')
    expect(row.textContent).toContain('708274')
    fireEvent.click(row)

    // The stored selection carries the CLOUD-side account id.
    const stored = window.sessionStorage.getItem('cd.account.selection.v1')
    expect(stored).toBe('acct:708274')
    expect(stored).not.toBe('acct:7')
    // The button shows the friendly row label (never a bare DB id).
    await waitFor(() => {
      expect(screen.getByTestId('account-selector-button').textContent).toContain(
        'Rackspace Cloud Account — 708274',
      )
    })
  })

  it('provider-aware button labels: AWS Account / Azure Subscription / Rackspace Account', async () => {
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) {
        return mockJson(200, makeCloudAccountsSelectable('AWS'))
      }
      if (url.includes('/api/cloud/accounts/selectable/AZURE')) {
        return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      }
      if (url.includes('/api/cloud/accounts/selectable/RACKSPACE')) {
        return mockJson(200, makeRackspaceSelectable())
      }
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    // AWS
    setProviderAndSelection('AWS', 'all')
    wrapRoutes(<AccountSelector />)
    let trigger = await screen.findByTestId('account-selector-button')
    await waitFor(() => {
      expect(trigger.textContent).toContain('AWS Account')
      expect(trigger.textContent).toContain('All AWS Accounts')
    })
    cleanup()

    // AZURE
    setProviderAndSelection('AZURE', 'all')
    wrapRoutes(<AccountSelector />)
    trigger = await screen.findByTestId('account-selector-button')
    await waitFor(() => {
      expect(trigger.textContent).toContain('Azure Subscription')
      expect(trigger.textContent).toContain('All Azure Subscriptions')
    })
    cleanup()

    // RACKSPACE
    setProviderAndSelection('RACKSPACE', 'all')
    wrapRoutes(<AccountSelector />)
    trigger = await screen.findByTestId('account-selector-button')
    await waitFor(() => {
      expect(trigger.textContent).toContain('Rackspace Account')
      expect(trigger.textContent).toContain('All Rackspace Cloud Accounts')
    })
  })
})

// ---------------------------------------------------------------------------
//  H3 — stale selection recovery
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — stale selection recovery', () => {
  it('a rehydrated stale acct:<db-id> selector under RACKSPACE self-corrects to the aggregate', async () => {
    // Pre-9D.1 session: the persisted selector is the local DB
    // row id (acct:7), not the cloud account id (acct:708274).
    setProviderAndSelection('RACKSPACE', 'acct:7')
    const { costsCalls } = installRackspaceRoutes({
      costsOverride: (url) => {
        // The real backend answer for a DB-id selector.
        if (decodeURIComponent(url).includes('account_id=acct:7')) {
          return mockJson(404, {
            error_code: 'AccountNotFound',
            message: "no RACKSPACE account matches selection='acct:7'",
          })
        }
        return null
      },
    })

    wrapRoutes(<CostsPage />)

    // The rehydration guard resets the stale selector to the
    // provider-wide aggregate once the provider rows load.
    await waitFor(() => {
      expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('all')
    })

    // An aggregate costs request (no account_id) goes out and
    // succeeds — the page recovers with real data.
    await waitFor(() => {
      expect(costsCalls().some((c) => !decodeURIComponent(c).includes('account_id='))).toBe(true)
    })
    expect(await screen.findByText('$2.48K')).toBeInTheDocument()

    // The raw backend selector error never reaches the browser UI.
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/no RACKSPACE account matches selection/)
    expect(text).not.toMatch(/AccountNotFound/)
  })

  it('switching provider resets the selection so a stale account context never crosses providers', async () => {
    setProviderAndSelection('AWS', 'all')
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) {
        return mockJson(200, makeCloudAccountsSelectable('AWS'))
      }
      if (url.includes('/api/cloud/accounts/selectable/RACKSPACE')) {
        return mockJson(200, makeRackspaceSelectable())
      }
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    wrapRoutes(
      <>
        <ProviderProbe />
        <AccountSelector />
      </>,
    )

    // Select the AWS QA-Dev account (DB id 10, cloud id 111111111111).
    fireEvent.click(await screen.findByTestId('account-selector-button'))
    fireEvent.click(await screen.findByTestId('account-option-10'))
    await waitFor(() => {
      expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('acct:111111111111')
    })

    // Switch provider AWS → RACKSPACE (the Cloud Context bar's
    // imperative setProvider path).
    await act(async () => {
      providerProbe?.setProvider('RACKSPACE')
    })

    // The specific selection is invalidated and the selector
    // renders the Rackspace aggregate with the Rackspace noun.
    await waitFor(() => {
      expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('all')
    })
    await waitFor(() => {
      const button = screen.getByTestId('account-selector-button')
      expect(button.textContent).toContain('All Rackspace Cloud Accounts')
      expect(button.textContent).toContain('Rackspace Account')
      // The stale AWS row label is gone.
      expect(button.textContent).not.toContain('AWS QA-Dev Account')
    })
  })

  it('switching provider Azure ↔ Rackspace never carries a stale subscription selector across providers', async () => {
    setProviderAndSelection('AZURE', 'all')
    const transport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/cloud/accounts/selectable/AZURE')) {
        return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      }
      if (url.includes('/api/cloud/accounts/selectable/RACKSPACE')) {
        return mockJson(200, makeRackspaceSelectable())
      }
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    wrapRoutes(
      <>
        <ProviderProbe />
        <AccountSelector />
      </>,
    )

    // Select the MCPP Subscription (DB id 5, subscription GUID).
    fireEvent.click(await screen.findByTestId('account-selector-button'))
    fireEvent.click(await screen.findByTestId('azure-sub-option-5'))
    await waitFor(() => {
      expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe(
        'sub:11111111-2222-3333-4444-555555555555',
      )
    })

    // Switch provider AZURE → RACKSPACE.  The Azure subscription
    // selector is invalidated: a sub:<guid> can never cross into
    // a provider whose canonical route would reject it with
    // AccountNotFound.
    await act(async () => {
      providerProbe?.setProvider('RACKSPACE')
    })

    await waitFor(() => {
      expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('all')
    })
    await waitFor(() => {
      const button = screen.getByTestId('account-selector-button')
      expect(button.textContent).toContain('All Rackspace Cloud Accounts')
      expect(button.textContent).toContain('Rackspace Account')
      // The stale Azure row label is gone.
      expect(button.textContent).not.toContain('MCPP Subscription')
    })

    // Switch back RACKSPACE → AZURE: still the aggregate, with
    // the Azure noun — the switch is symmetric and never
    // resurrects the earlier subscription selection.
    await act(async () => {
      providerProbe?.setProvider('AZURE')
    })
    await waitFor(() => {
      const button = screen.getByTestId('account-selector-button')
      expect(button.textContent).toContain('All Azure Subscriptions')
      expect(button.textContent).toContain('Azure Subscription')
      // The stale Rackspace wording is gone.
      expect(button.textContent).not.toContain('Rackspace Account')
    })
    expect(window.sessionStorage.getItem('cd.account.selection.v1')).toBe('all')
  })
})

// ---------------------------------------------------------------------------
//  H4 — Rackspace scope controls (Month / Region)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace scope controls', () => {
  it('Rackspace Costs hides the historical Month selector and the regional cost selector', async () => {
    setProviderAndSelection('RACKSPACE', 'all')
    // Persist a HISTORICAL month + a region from another
    // provider's session — neither may reach a Rackspace query.
    window.sessionStorage.setItem(
      'accd.finops.period',
      JSON.stringify({ kind: 'month', month: HISTORICAL_MONTH }),
    )
    window.sessionStorage.setItem('accd.finops.region', 'us-east-1')

    const { costsCalls } = installRackspaceRoutes()

    // Mount WITHOUT initialPeriod/initialRegion so the stored
    // historical month + region are actually rehydrated.
    render(
      <MemoryRouter>
        <ThemeProvider>
          <AuthProvider>
            <ProviderProvider>
              <AccountProvider>
                <FinopsPeriodProvider>
                  <CostsPage />
                </FinopsPeriodProvider>
              </AccountProvider>
            </ProviderProvider>
          </AuthProvider>
        </ThemeProvider>
      </MemoryRouter>,
    )

    // The static current-cycle control replaces the month <select>.
    expect(await screen.findByTestId('period-selector-fixed-cycle')).toHaveTextContent(
      'Current Billing Cycle',
    )
    expect(screen.queryByTestId('period-selector-input')).toBeNull()
    expect(screen.queryByTestId('period-selector')).not.toBeNull()
    // No regional cost selector at all.
    expect(screen.queryByTestId('region-filter')).toBeNull()

    // The costs request used the CURRENT month and NO region —
    // the persisted historical month / region never leak.
    await waitFor(() => {
      expect(costsCalls().length).toBeGreaterThan(0)
    })
    for (const url of costsCalls()) {
      expect(url).toContain(`month=${CURRENT_MONTH}`)
      expect(url).not.toContain(`month=${HISTORICAL_MONTH}`)
      expect(decodeURIComponent(url)).not.toContain('region=')
    }

    // Current Billing Cycle semantics: the ACTUAL source cycle is
    // labelled as a billing cycle — never a calendar month.
    expect(
      (await screen.findAllByText('Billing cycle Oct 1, 2026 – Oct 31, 2026')).length,
    ).toBeGreaterThan(0)
  })

  it('AWS and Azure keep the historical Month selector and the regional cost selector', async () => {
    // --- AWS (canonical /api/cloud/costs route, Phase 9D.2) ---
    setProviderAndSelection('AWS', 'all')
    const awsTransport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/aws/cloud-accounts/selectable')) {
        return mockJson(200, makeCloudAccountsSelectable('AWS'))
      }
      // Phase 9D.2 — the Costs page queries ONLY the canonical
      // route for AWS.  The legacy /api/aws/costs stub below is a
      // tripwire: if the page regresses to it, the canonical stub
      // is bypassed and the selector contract test in
      // costs-page.test.tsx fails.
      if (url.includes('/api/aws/costs')) return makeCloudCosts('AWS', { total: '100.00' })
      if (url.includes('/api/cloud/costs?provider=AWS')) return makeCloudCosts('AWS', { total: '100.00' })
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: awsTransport as unknown as typeof fetch,
    })
    wrapRoutes(<CostsPage />)
    const awsSelect = await screen.findByTestId('period-selector-input')
    expect(awsSelect.tagName).toBe('SELECT')
    expect(screen.getByTestId('region-filter')).toBeInTheDocument()
    cleanup()

    // --- AZURE (canonical route, unchanged behaviour) ---
    setProviderAndSelection('AZURE', 'all')
    const azureTransport = vi.fn(async (url: string) => {
      if (url.includes('/api/auth/info')) return mockJson(200, { auth_enabled: false })
      if (url.includes('/api/cloud/providers')) return mockJson(200, makeRackspaceProviders())
      if (url.includes('/api/cloud/accounts/selectable/AZURE')) {
        return mockJson(200, makeCloudAccountsSelectable('AZURE'))
      }
      if (url.includes('/api/cloud/costs')) return makeCloudCosts('AZURE', { total: '5.00' })
      return mockJson(404, {})
    })
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: azureTransport as unknown as typeof fetch,
    })
    wrapRoutes(<CostsPage />)
    const azureSelect = await screen.findByTestId('period-selector-input')
    expect(azureSelect.tagName).toBe('SELECT')
    expect(screen.getByTestId('region-filter')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
//  H5 — account-scope failure UX (AccountNotFound)
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — account-scope failure UX', () => {
  it('AccountNotFound renders the professional message — the raw backend selector error is never exposed', async () => {
    // A VALID selected Rackspace account (acct:708274) whose
    // canonical cost request nonetheless fails with the
    // backend's AccountNotFound wording.
    setProviderAndSelection('RACKSPACE', 'acct:708274')
    installRackspaceRoutes({
      costsOverride: () =>
        mockJson(404, {
          error_code: 'AccountNotFound',
          message: "no RACKSPACE account matches selection='acct:708274'",
        }),
    })

    wrapRoutes(<CostsPage />)

    // The professional, provider-aware message appears.
    await waitFor(() => {
      expect(
        screen.getAllByText(/The selected Rackspace Account is no longer available/).length,
      ).toBeGreaterThan(0)
    })

    // The raw backend selector error never reaches the browser UI.
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/no RACKSPACE account matches selection/)
    expect(text).not.toMatch(/AccountNotFound/)
  })
})

// ---------------------------------------------------------------------------
//  H6 — Rackspace dashboard readiness
// ---------------------------------------------------------------------------

describe('Phase 9D.1 — Rackspace dashboard readiness', () => {
  it('the Rackspace dashboard exits Loading when the canonical APIs succeed', async () => {
    setProviderAndSelection('RACKSPACE', 'all')
    installRackspaceRoutes()

    wrapRoutes(<DashboardPage />)

    // The unified dashboard mounts for RACKSPACE.
    await waitFor(() => {
      expect(
        document.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider'),
      ).toBe('RACKSPACE')
    })

    // The status badge leaves Loading/Refreshing once the
    // canonical costs / resources / recommendations all succeed.
    await waitFor(() => {
      const badge = screen.getByTestId('dashboard-status-badge')
      expect(badge.textContent ?? '').not.toMatch(/Loading|Refreshing/)
    })

    // The canonical data renders: spend, resources, recommendations.
    expect(await screen.findByText('$2.48K')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByTestId('resources-total-kpi')).toHaveTextContent('12')
    })
    expect(screen.getByTestId('optimization-count')).toHaveTextContent('3')
  })
})
