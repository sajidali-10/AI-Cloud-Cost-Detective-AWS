// Post-Phase 7C final cleanup — Azure Dashboard wording + navigation.
//
// Asserts the post-cleanup Azure Dashboard text and that the
// Resources / Optimization cards expose real navigation links
// instead of placeholder "Coming in Phase 7C.2C/2D" notices.
//
// The previous provider-switch hotfix tests
// (phase7c-hotfix-dashboard-switch.test.tsx) and the Azure
// subscription/cost hotfix tests
// (phase7c-hotfix-subscription-cost-correctness.test.tsx) continue
// to live alongside this file — see the regression suite at the
// bottom of the file for the re-affirmation tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import { authDisabledElement } from './helpers/auth-bootstrap'

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

// Build a transport that handles every endpoint DashboardPage hits.
// Returns small valid shapes so the dashboard renders without
// throwing — the assertion targets the static text/links, not the
// data values.
function buildAzureDashboardFetchMock(): ReturnType<typeof vi.fn> {
  return vi.fn(async (url: string) => {
    if (url.includes('/api/auth/info')) return json(200, { auth_enabled: false })
    if (url.includes('/api/cloud/providers')) {
      return json(200, {
        providers: [
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
        count: 1,
      })
    }
    if (url.includes('/api/cloud/accounts/selectable/AZURE')) {
      return json(200, {
        items: [
          {
            id: 5,
            provider: 'AZURE',
            account_id: '11111111-2222-3333-4444-555555555555',
            display_name: 'MCPP Subscription',
            is_management: false,
            is_enabled: true,
          },
          {
            id: 6,
            provider: 'AZURE',
            account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            display_name: 'Microsoft Azure Sponsorship',
            is_management: false,
            is_enabled: true,
          },
        ],
        count: 2,
      })
    }
    if (url.includes('/api/cloud/costs')) {
      // PARTIAL_SUCCESS with one failed + one successful so the
      // KPI renders the cost-completeness indicator.  MCPP succeeded
      // with a small amount, Sponsorship throttled.
      return json(200, {
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
          total_cost: '7.34',
          previous_period_cost: '0',
          change_amount: '7.34',
          change_percent: null,
          estimated: false,
          daily_trend: [],
          by_service: [],
          by_region: [],
          by_account: [
            { account_id: '11111111-2222-3333-4444-555555555555', display_name: 'MCPP Subscription', amount: '7.34', currency: 'USD' },
            { account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', display_name: 'Microsoft Azure Sponsorship', amount: '0', currency: 'USD' },
          ],
          account_errors: [
            { account_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', display_name: 'Microsoft Azure Sponsorship', code: 'THROTTLED', message: 'THROTTLED' },
          ],
          source: 'AZURE_COST_MANAGEMENT',
        },
        cache_status: 'REFRESHED',
        cached_at: '2026-10-17T00:00:00Z',
        expires_at: '2026-10-17T01:00:00Z',
      })
    }
    if (url.includes('/api/cloud/resources')) {
      return json(200, { summary: { status: 'SUCCESS', provider: 'AZURE' }, resources: [] })
    }
    if (url.includes('/api/cloud/recommendations')) {
      return json(200, { summary: { status: 'SUCCESS', provider: 'AZURE' }, recommendations: [] })
    }
    if (url.includes('/api/cloud/optimization')) {
      return json(200, { provider: 'AZURE', capabilities: { OPTIMIZATION: 'SUPPORTED' }, summary: {} })
    }
    return json(404, {})
  })
}

beforeEach(() => {
  if (typeof window !== 'undefined') window.sessionStorage.clear()
})

afterEach(async () => {
  cleanup()
  if (typeof window !== 'undefined') window.sessionStorage.clear()
  try {
    const { __resetApiForTests } = await import('../lib/api')
    __resetApiForTests()
  } catch {
    // ignore
  }
})

describe('Post-Phase 7C final cleanup — Azure Dashboard wording', () => {
  it('Azure Dashboard renders the provider-neutral title and subtitle', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    const view = render(authDisabledElement(<DashboardPage />))

    // Phase 7D.3 unified the dashboard into a single component
    // tree with no per-provider heading split.  The active
    // provider is exposed via the data-provider attribute so
    // tests (and downstream consumers) can distinguish AWS from
    // Azure without depending on stale per-provider copy.
    await waitFor(() => {
      const root = view.container.querySelector('[data-testid="unified-dashboard"]')
      expect(root?.getAttribute('data-provider')).toBe('AZURE')
    })
    // The page header still carries the canonical "Dashboard"
    // heading — there is no longer a per-provider "Azure
    // Dashboard" heading because the layout is provider-neutral.
    expect(screen.getAllByText(/^Dashboard$/).length).toBeGreaterThan(0)
    // Subtitle references the three pillars (cost, resource
    // inventory, optimization) for the active provider.
    expect(
      screen.getAllByText(/Azure cost visibility, resource inventory, optimization/i).length,
    ).toBeGreaterThan(0)
  })

  it('Azure Dashboard does NOT contain AWS-specific spend wording', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    const { container } = render(authDisabledElement(<DashboardPage />))

    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider'),
      ).toBe('AZURE')
    })
    // "AWS Spend" must not appear in the Azure dashboard.
    expect(container.textContent).not.toMatch(/AWS Spend/i)
    // "AWS cost visibility" must not appear in the Azure subtitle.
    expect(container.textContent).not.toMatch(/AWS cost visibility/i)
    // The Spend KPI must read "Total Spend" (provider-neutral),
    // not the legacy "Azure Spend" / "AWS Spend" labels.
    expect(screen.getByText(/^Total Spend$/)).toBeTruthy()
  })

  it('Azure Dashboard does NOT contain stale "Coming in Phase 7C.2C/2D" phrases', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    const { container } = render(authDisabledElement(<DashboardPage />))

    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="unified-dashboard"]')?.getAttribute('data-provider'),
      ).toBe('AZURE')
    })
    expect(container.textContent).not.toMatch(/Coming in Phase 7C\.2C/i)
    expect(container.textContent).not.toMatch(/Coming in Phase 7C\.2D/i)
    expect(container.textContent).not.toMatch(/will activate/i)
    expect(container.textContent).not.toMatch(/next phase ships/i)
  })

  it('Azure Dashboard exposes a real navigation link to /resources', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    render(authDisabledElement(<DashboardPage />))

    // Phase 7D.3 renamed the per-provider link testids to
    // provider-neutral ones (the dashboard is shared between
    // AWS and Azure).  The href and "View Resources" copy are
    // unchanged from the Phase 7C cleanup.
    const link = await waitFor(() =>
      screen.getByTestId('dashboard-resources-link'),
    )
    expect(link.getAttribute('href')).toBe('/resources')
    expect(link.textContent).toMatch(/View Resources/i)
  })

  it('Azure Dashboard exposes a real navigation link to /optimization', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    render(authDisabledElement(<DashboardPage />))

    const link = await waitFor(() =>
      screen.getByTestId('dashboard-optimization-link'),
    )
    expect(link.getAttribute('href')).toBe('/optimization')
    expect(link.textContent).toMatch(/View Optimization/i)
  })

  it('Azure Dashboard renders the partial-data marker when one subscription succeeded and one failed', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    const { configureApi } = await import('../lib/api')
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: buildAzureDashboardFetchMock() as unknown as typeof fetch,
    })
    const { render } = await import('@testing-library/react')
    const { DashboardPage } = await import('../pages/DashboardPage')
    render(authDisabledElement(<DashboardPage />))

    // The Partial marker in the SpendKpi description must show.
    await waitFor(() => {
      expect(screen.getByText(/Partial \(1 unavailable\)/)).toBeTruthy()
    })
    // The valid subtotal must still render.
    expect(screen.getByText('$7.34')).toBeTruthy()
  })
})

describe('Post-Phase 7C final cleanup — regression preservation', () => {
  it('prior provider-switch hotfix tests are present', async () => {
    // Belt-and-braces: ensure the prior hotfix file still imports
    // and the dashboard-switch assertions are reachable.
    const mod = await import('./phase7c-hotfix-dashboard-switch.test.tsx')
    expect(mod).toBeDefined()
  })

  it('prior Azure subscription selector / cost correctness tests are present', async () => {
    const mod = await import('./phase7c-hotfix-subscription-cost-correctness.test.tsx')
    expect(mod).toBeDefined()
  })
})
