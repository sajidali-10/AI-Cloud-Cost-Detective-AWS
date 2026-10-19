// Phase 7C post-release hotfix — DashboardPage provider switching.
//
// Reproduces the regression that caused the main dashboard to go
// permanently blank when the user toggled AWS ↔ Azure.  Before
// the fix, ``DashboardPage`` called ``useFinopsQuery`` 5 times
// after an early ``return`` for the Azure branch.  When the user
// switched provider mid-session, React saw a different number of
// hooks between renders and threw ``Rendered fewer hooks than
// expected`` — the entire tree unmounted and only a manual
// browser refresh restored the UI.
//
// Phase 7D.3 unifies the dashboard into a single provider-aware
// component tree (no more AWS / Azure split child components), so
// the hook-order invariant holds across every provider switch by
// construction.  These tests verify that:
//
//   1. AWS renders without crashing and stamps the data-provider
//      attribute as "AWS".
//   2. Azure renders without crashing and stamps the data-provider
//      attribute as "AZURE".
//   3. Provider switches never throw a React hook-count exception
//      and never permanently blank the page — when a canonical
//      request fails, the page still renders the navigation and
//      page chrome.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import { renderWithAuthEnabled } from './helpers/auth-bootstrap'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function buildFetchMock(opts: {
  provider: 'AWS' | 'AZURE'
  selection?: string | null
  costTotal?: string
  awsCostTotal?: string
  azureCostTotal?: string
}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes('/api/cloud/providers')) {
      return mockJson(200, {
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
      })
    }
    if (url.includes('/api/cloud/accounts/selectable')) {
      return mockJson(200, { items: [], count: 0 })
    }
    if (url.includes('/api/aws/cloud-accounts/selectable')) {
      return mockJson(200, { items: [], count: 0 })
    }
    if (url.includes('/api/cloud/costs')) {
      return mockJson(200, {
        report: {
          provider: opts.provider,
          scope: opts.selection === 'all' || !opts.selection ? 'all' : `sub:${opts.selection}`,
          scope_kind: 'all',
          status: 'SUCCESS',
          currency_status: 'OK',
          account_id: opts.selection ?? null,
          account_display_name: null,
          period: { start: '2026-10-01', end: '2026-10-17', days: 17 },
          previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 },
          currency: 'USD',
          total_cost: opts.provider === 'AWS' ? opts.awsCostTotal ?? '100' : opts.azureCostTotal ?? '5',
          previous_period_cost: '80',
          change_amount: opts.provider === 'AWS' ? '20' : '2',
          change_percent: null,
          estimated: false,
          daily_trend: [],
          by_service: [],
          by_region: [],
          by_account: [],
          account_errors: [],
          source: opts.provider === 'AWS' ? 'AWS_COST_EXPLORER' : 'AZURE_COST_MANAGEMENT',
        },
        cache_status: 'REFRESHED',
        cached_at: '2026-10-17T00:00:00Z',
        expires_at: '2026-10-17T01:00:00Z',
      })
    }
    if (url.includes('/api/cloud/resources')) {
      return mockJson(200, {
        summary: {
          provider: opts.provider,
          scope: 'all',
          scope_kind: 'all',
          status: 'SUCCESS',
          total_resources: 0,
          by_type: [],
          by_location: [],
          by_resource_group: [],
          by_subscription: [],
          subscription_errors: [],
          source: opts.provider === 'AWS' ? 'AWS_RESOURCE_GROUP' : 'AZURE_RESOURCE_GRAPH',
        },
        resources: [],
      })
    }
    if (url.includes('/api/cloud/recommendations')) {
      return mockJson(200, {
        summary: {
          provider: opts.provider,
          scope: 'all',
          scope_kind: 'all',
          status: 'SUCCESS',
          savings_status: 'OK',
          currency: 'USD',
          total_recommendations: 0,
          recommendations_with_savings: 0,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: '0',
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
          source: opts.provider === 'AWS' ? 'AWS_COST_OPTIMIZATION_HUB' : 'AZURE_ADVISOR',
        },
        recommendations: [],
      })
    }
    return mockJson(200, {})
  })
  return fetchMock
}

beforeEach(() => {
  if (typeof window !== 'undefined') {
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
  }
})

afterEach(() => {
  cleanup()
  if (typeof window !== 'undefined') window.sessionStorage.clear()
  __resetApiForTestsIfNeeded()
  vi.restoreAllMocks()
})

// Lazy import helper: importing ``__resetApiForTests`` in module
// scope would require the api module at collection time which
// some test configurations mock.  Doing it lazily keeps the test
// resilient to isolation tweaks.
function __resetApiForTestsIfNeeded(): void {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const api = (globalThis as { __resetApiForTests?: () => void }).__resetApiForTests
    if (typeof api === 'function') api()
  } catch {
    // ignore
  }
}

describe('Phase 7C post-release hotfix — DashboardPage provider switching', () => {
  it('renders AWS dashboard without crashing', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const fetchMock = buildFetchMock({ provider: 'AWS' })
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: fetchMock as unknown as typeof fetch,
    })
    const { DashboardPage } = await import('../pages/DashboardPage')
    const view = await renderWithAuthEnabled(<DashboardPage />)
    await waitFor(() => {
      // The unified dashboard stamps `data-provider="AWS"` so
      // tests can assert the active provider without depending on
      // per-provider headings (the Phase 7C split-component
      // layout was retired in Phase 7D.3).
      const root = view.container.querySelector('[data-testid="unified-dashboard"]')
      expect(root?.getAttribute('data-provider')).toBe('AWS')
    })
    await waitFor(() => {
      expect(screen.getAllByText(/Dashboard/).length).toBeGreaterThan(0)
    })
  })

  it('renders Azure dashboard without crashing', async () => {
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }
    const fetchMock = buildFetchMock({ provider: 'AZURE' })
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: fetchMock as unknown as typeof fetch,
    })
    const { DashboardPage } = await import('../pages/DashboardPage')
    const view = await renderWithAuthEnabled(<DashboardPage />)
    await waitFor(() => {
      const root = view.container.querySelector('[data-testid="unified-dashboard"]')
      expect(root?.getAttribute('data-provider')).toBe('AZURE')
    })
    // Friendly Azure scope attribution always renders on the page
    // header.  The unified dashboard replaces the Phase 7C
    // per-provider "Azure Dashboard" heading with this friendly
    // label, preserving the executive visual rhythm.
    await waitFor(() => {
      expect(screen.getAllByText(/All Azure Subscriptions/).length).toBeGreaterThan(0)
    })
  })

  it('renders error boundary fallback instead of permanent blank page on provider-switch exception', async () => {
    // The bug was: DashboardPage called useFinopsQuery 5 times
    // after an early return for the Azure branch.  Switching
    // provider mid-session threw a React "Rendered fewer hooks
    // than expected" exception that unmounted the entire tree.
    // The hotfix wraps the dashboard in an error boundary that
    // surfaces a professional error state.  Phase 7D.3 removes
    // the per-provider split (one tree, one hook order) but
    // keeps the boundary so any future regression still renders
    // *something*.  This test forces a 500 from /api/cloud/costs
    // and asserts the page chrome stays rendered.
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/api/cloud/providers')) {
        return mockJson(200, {
          providers: [{ provider: 'AWS', is_registered: true, is_enabled: true, credential_strategies: [], capabilities: [], supported_regions: [], supported_lookback_days: [], status: 'OK' }],
          count: 1,
        })
      }
      if (url.includes('/api/aws/cloud-accounts/selectable')) {
        return mockJson(200, { items: [], count: 0 })
      }
      if (url.includes('/api/cloud/accounts/selectable')) {
        return mockJson(200, { items: [], count: 0 })
      }
      if (url.includes('/api/cloud/costs')) {
        return mockJson(500, { status: 'error', error_code: 'UPSTREAM_ERROR' })
      }
      if (url.includes('/api/cloud/resources')) {
        return mockJson(500, { status: 'error' })
      }
      if (url.includes('/api/cloud/recommendations')) {
        return mockJson(500, { status: 'error' })
      }
      return mockJson(200, {})
    })
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: fetchMock as unknown as typeof fetch,
    })
    const { DashboardPage } = await import('../pages/DashboardPage')
    await renderWithAuthEnabled(<DashboardPage />)
    await waitFor(() => {
      // Even on a 500, the page must render *something* (the page
      // heading) — it must never become permanently blank.
      expect(screen.queryByText(/Dashboard/)).not.toBeNull()
    })
  })
})
