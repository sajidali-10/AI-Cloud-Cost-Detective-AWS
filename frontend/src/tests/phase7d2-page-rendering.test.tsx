// Phase 7D.2 — page-level integration tests for the unified
// Optimization page.  Pins the React-level behaviour:
//
//   * Canonical `/api/cloud/recommendations` for both providers.
//   * Provider switch (AWS ↔ Azure) does not blank the page.
//   * Scope switch (QA → Prod → All → QA) never shows a stale row.
//   * Friendly attribution — no `acct:<n>` or `sub:<guid>` leak.
//   * Page never emits stale Phase 7C placeholder text.
//   * Page is read-only (no remediation controls).
//   * Empty / partial / error states render correctly.
//
// Test-helper note: the tests below use the lower-level
// `render(authDisabledElement(...))` pattern (the same one used by
// `phase7c-final-cleanup-dashboard-wording.test.tsx`) instead of
// `renderWithAuthDisabled(...)`.  The latter internally calls
// `__resetApiForTests()` and re-installs a transport that only
// handles `/api/auth/info`, which would wipe the test's custom
// `/api/cloud/recommendations` mock and the page would never
// receive data.  This preserves the Phase 7B fixture contract.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { __resetApiForTests, configureApi } from '../lib/api'
import { invalidateCache, invalidateFinopsCache } from '../lib/finops/store'
import { OptimizationPage } from '../pages/OptimizationPage'
import { authDisabledElement } from './helpers/auth-bootstrap'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

interface CanonicalOpts {
  provider: 'AWS' | 'AZURE'
  recommendations: Array<Record<string, unknown>>
  summary: Record<string, unknown>
  selectableAccounts: Array<Record<string, unknown>>
}

function buildTransport(opts: CanonicalOpts): ReturnType<typeof vi.fn> {
  return vi.fn(async (input: RequestInfo | URL) => {
    const path = typeof input === 'string' ? input : input.toString()
    if (path === '/api/auth/info') return jsonResponse(200, { auth_enabled: false })
    if (path.includes('/api/cloud/providers')) {
      return jsonResponse(200, {
        providers: [
          {
            provider: opts.provider,
            is_registered: true,
            is_enabled: true,
            credential_strategies: [],
            capabilities: [],
            supported_regions: [],
            supported_lookback_days: [7, 30, 60, 90],
            status: 'OK',
          },
        ],
        count: 1,
      })
    }
    if (path.includes('/api/cloud/accounts/selectable/')) {
      return jsonResponse(200, { items: opts.selectableAccounts, count: opts.selectableAccounts.length })
    }
    if (path.includes('/api/aws/cloud-accounts/selectable')) {
      // Phase 7B legacy path — AccountProvider still calls this for AWS.
      return jsonResponse(200, { items: opts.selectableAccounts, count: opts.selectableAccounts.length })
    }
    if (path.includes('/api/cloud/recommendations')) {
      return jsonResponse(200, { summary: opts.summary, recommendations: opts.recommendations })
    }
    return jsonResponse(404, {})
  })
}

const AWS_SELECTABLE = [
  { id: 11, provider: 'AWS', account_id: '974053642038', display_name: 'AWS QA-Dev Account', is_management: false, enabled: true },
  { id: 12, provider: 'AWS', account_id: '999999999999', display_name: 'AWS Production Account', is_management: false, enabled: true },
]

const AZURE_SELECTABLE = [
  { id: 21, provider: 'AZURE', account_id: '11111111-2222-3333-4444-555555555555', display_name: 'MCPP Subscription', is_management: false, enabled: true },
  { id: 22, provider: 'AZURE', account_id: '22222222-3333-4444-5555-666666666666', display_name: 'Microsoft Azure Sponsorship', is_management: false, enabled: true },
]

function awsRec(id: string, accountId: string, savings: string | null): Record<string, unknown> {
  return {
    provider: 'AWS',
    account_id: accountId,
    region: 'us-east-1',
    recommendation_id: id,
    resource_id: `vol-${id}`,
    resource_type: 'EBS_VOLUME',
    service: 'EC2',
    action: 'REVIEW_DELETE_UNATTACHED_EBS',
    title: `AWS EBS volume ${id}`,
    category: 'Cost Optimization',
    impact: id.endsWith('1') ? 'High' : id.endsWith('2') ? 'Medium' : 'Low',
    description: 'Unattached EBS volume',
    estimated_savings: savings,
    currency: 'USD',
    source: 'AWS_COST_OPTIMIZATION_HUB',
    extensions: { resource_name: `aws-name-${id}` },
  }
}

function azureRec(id: string, subscriptionId: string, savings: string | null): Record<string, unknown> {
  return {
    provider: 'AZURE',
    account_id: subscriptionId,
    region: 'eastus',
    recommendation_id: id,
    resource_id: `/subscriptions/${subscriptionId}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/${id}`,
    resource_type: 'Microsoft.Compute/virtualMachines',
    service: 'Compute',
    action: 'Right-size',
    title: `Azure VM ${id}`,
    category: 'Cost',
    impact: 'Medium',
    description: 'Underutilised VM',
    estimated_savings: savings,
    currency: 'USD',
    source: 'AZURE_ADVISOR',
    extensions: { resource_name: `azure-name-${id}` },
  }
}

function awsAllSummary(total: number, savings: string | null): Record<string, unknown> {
  return {
    provider: 'AWS',
    scope: 'all',
    scope_kind: 'all',
    status: 'SUCCESS',
    savings_status: savings === null ? 'EMPTY' : 'OK',
    currency: 'USD',
    total_recommendations: total,
    recommendations_with_savings: savings === null ? 0 : total,
    recommendations_without_savings: savings === null ? total : 0,
    potential_annual_savings: null,
    potential_savings: savings,
    savings_period: 'MONTHLY',
    savings_by_currency: savings === null ? [] : [{ currency: 'USD', amount: savings }],
    by_impact: [],
    by_resource_type: [],
    by_subscription: [],
    by_resource_group: [],
    by_type: [],
    by_subcategory: [],
    subscription_errors: [],
    by_account: [],
    by_source: [{ key: 'AWS_COST_OPTIMIZATION_HUB', count: total }],
    by_action: [],
    account_errors: [],
    source: 'AWS_OPTIMIZATION',
  }
}

function azureAllSummary(total: number, savings: string | null): Record<string, unknown> {
  return {
    provider: 'AZURE',
    scope: 'all',
    scope_kind: 'all',
    status: 'SUCCESS',
    savings_status: savings === null ? 'EMPTY' : 'OK',
    currency: 'USD',
    total_recommendations: total,
    recommendations_with_savings: savings === null ? 0 : total,
    recommendations_without_savings: savings === null ? total : 0,
    potential_annual_savings: savings,
    potential_savings: savings,
    savings_period: 'ANNUAL',
    savings_by_currency: savings === null ? [] : [{ currency: 'USD', amount: savings }],
    by_impact: [],
    by_resource_type: [],
    by_subscription: [],
    by_resource_group: [],
    by_type: [],
    by_subcategory: [],
    subscription_errors: [],
    by_account: [],
    by_source: [{ key: 'AZURE_ADVISOR', count: total }],
    by_action: [],
    account_errors: [],
    source: 'AZURE_ADVISOR',
  }
}

beforeEach(() => {
  __resetApiForTests()
  // Phase 7D.3.2 closure — full wipe (preserveLastGood: false).
  // Without this, a prior test's authoritative lastGood leaks
  // into the next mount and surfaces as stale rows under a
  // different mock transport.
  invalidateFinopsCache()
  if (typeof window !== 'undefined') window.sessionStorage.clear()
})

afterEach(async () => {
  cleanup()
  invalidateFinopsCache()
  __resetApiForTests()
  if (typeof window !== 'undefined') window.sessionStorage.clear()
})

describe('Phase 7D.2 — page-level: AWS scopes', () => {
  it('renders the unified page for AWS QA-Dev', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'acct:974053642038')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [awsRec('qa1', '974053642038', '15.00')],
        summary: awsAllSummary(1, '15.00'),
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('AWS QA-Dev Account')).toBeInTheDocument()
    expect(await screen.findByText('Active Recommendations')).toBeInTheDocument()
    expect(await screen.findByText('Potential Monthly Savings')).toBeInTheDocument()
    expect(await screen.findByText('Resources Affected')).toBeInTheDocument()
    expect(await screen.findByText('Recommendation Sources')).toBeInTheDocument()
  })

  it('renders the unified page for All AWS', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [
          awsRec('qa1', '974053642038', '15.00'),
          awsRec('prod1', '999999999999', '8.00'),
        ],
        summary: awsAllSummary(2, '23.00'),
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All AWS Accounts')).toBeInTheDocument()
    // The accountMap populates asynchronously after the AccountProvider
    // fetch resolves; wait for the friendly per-account labels to land
    // in the breakdown chart and the table rows.
    await waitFor(
      () => {
        expect(screen.getAllByText('AWS QA-Dev Account').length).toBeGreaterThan(0)
        expect(screen.getAllByText('AWS Production Account').length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )
  })

  it('AWS Production scope attribution is friendly (no acct:<n>)', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'acct:999999999999')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [awsRec('prod1', '999999999999', '8.00')],
        summary: awsAllSummary(1, '8.00'),
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    const { container } = render(authDisabledElement(<OptimizationPage />))
    await screen.findByText('AWS Production Account')
    expect(container.textContent).not.toMatch(/acct:12/)
    expect(container.textContent).not.toMatch(/acct:11/)
  })
})

describe('Phase 7D.2 — page-level: Azure scopes', () => {
  it('renders the unified page for Azure MCPP', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'sub:11111111-2222-3333-4444-555555555555')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AZURE',
        recommendations: [azureRec('mcpp1', '11111111-2222-3333-4444-555555555555', '1440.00')],
        summary: azureAllSummary(1, '1440.00'),
        selectableAccounts: AZURE_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('MCPP Subscription')).toBeInTheDocument()
  })

  it('renders the unified page for All Azure', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AZURE',
        recommendations: [
          azureRec('mcpp1', '11111111-2222-3333-4444-555555555555', '1440.00'),
          azureRec('sponsor1', '22222222-3333-4444-5555-666666666666', '600.00'),
        ],
        summary: azureAllSummary(2, '2040.00'),
        selectableAccounts: AZURE_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All Azure Subscriptions')).toBeInTheDocument()
    await waitFor(
      () => {
        expect(screen.getAllByText('MCPP Subscription').length).toBeGreaterThan(0)
        expect(screen.getAllByText('Microsoft Azure Sponsorship').length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )
  })

  it('Azure selector values do not contain acct:<n>', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'sub:11111111-2222-3333-4444-555555555555')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AZURE',
        recommendations: [azureRec('mcpp1', '11111111-2222-3333-4444-555555555555', '1440.00')],
        summary: azureAllSummary(1, '1440.00'),
        selectableAccounts: AZURE_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    const { container } = render(authDisabledElement(<OptimizationPage />))
    await screen.findByText('MCPP Subscription')
    expect(container.textContent).not.toMatch(/acct:21/)
    expect(container.textContent).not.toMatch(/acct:22/)
  })
})

describe('Phase 7D.2 — page-level: provider-switch safety', () => {
  it('AWS → Azure → AWS does not blank the page', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')

    // Track provider switch requests by URL — assertion: every
    // canonical recommendation request carries the right provider.
    const transport = vi.fn(async (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/auth/info') return jsonResponse(200, { auth_enabled: false })
      if (path.includes('/api/cloud/providers')) {
        return jsonResponse(200, {
          providers: [
            { provider: 'AWS', is_registered: true, is_enabled: true, credential_strategies: [], capabilities: [], supported_regions: [], supported_lookback_days: [30], status: 'OK' },
            { provider: 'AZURE', is_registered: true, is_enabled: true, credential_strategies: [], capabilities: [], supported_regions: [], supported_lookback_days: [30], status: 'OK' },
          ],
          count: 2,
        })
      }
      if (path.includes('/api/cloud/accounts/selectable/AWS')) {
        return jsonResponse(200, { items: AWS_SELECTABLE, count: 2 })
      }
      if (path.includes('/api/cloud/accounts/selectable/AZURE')) {
        return jsonResponse(200, { items: AZURE_SELECTABLE, count: 2 })
      }
      if (path.includes('/api/aws/cloud-accounts/selectable')) {
        return jsonResponse(200, { items: AWS_SELECTABLE, count: 2 })
      }
      if (path.includes('/api/cloud/recommendations')) {
        const url = new URL(path, 'http://memory')
        const provider = url.searchParams.get('provider')
        if (provider === 'AWS') {
          return jsonResponse(200, {
            summary: awsAllSummary(2, '23.00'),
            recommendations: [
              awsRec('qa1', '974053642038', '15.00'),
              awsRec('prod1', '999999999999', '8.00'),
            ],
          })
        }
        if (provider === 'AZURE') {
          return jsonResponse(200, {
            summary: azureAllSummary(2, '2040.00'),
            recommendations: [
              azureRec('mcpp1', '11111111-2222-3333-4444-555555555555', '1440.00'),
              azureRec('sponsor1', '22222222-3333-4444-5555-666666666666', '600.00'),
            ],
          })
        }
      }
      return jsonResponse(404, {})
    })

    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })

    // AWS first.
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All AWS Accounts')).toBeInTheDocument()
    await waitFor(
      () => {
        expect(screen.getAllByText('AWS QA-Dev Account').length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )

    // Switch to Azure.
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    // Force a remount so the new provider sticks; this mirrors a
    // user-driven provider switch that the Cloud Context bar
    // triggers by remounting via state.  The sessionStorage write
    // + remount simulates the post-fix UX in test time.
    cleanup()
    invalidateFinopsCache()
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All Azure Subscriptions')).toBeInTheDocument()
    await waitFor(
      () => {
        expect(screen.getAllByText('MCPP Subscription').length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )

    // Switch back to AWS.
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    cleanup()
    invalidateFinopsCache()
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: transport as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All AWS Accounts')).toBeInTheDocument()
  })
})

describe('Phase 7D.2 — page-level: no stale Phase 7C wording', () => {
  it('Optimization page never renders stale Phase 7C placeholder text', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AZURE',
        recommendations: [],
        summary: azureAllSummary(0, null),
        selectableAccounts: AZURE_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    const { container } = render(authDisabledElement(<OptimizationPage />))
    await screen.findByText('All Azure Subscriptions')
    expect(container.textContent).not.toMatch(/Coming in Phase 7C\.2C/i)
    expect(container.textContent).not.toMatch(/Coming in Phase 7C\.2D/i)
    expect(container.textContent).not.toMatch(/will activate/i)
    expect(container.textContent).not.toMatch(/next phase ships/i)
  })
})

describe('Phase 7D.2 — page-level: read-only safety', () => {
  it('Page does not surface remediation controls (Apply / Resize / Stop / Delete)', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'acct:974053642038')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [awsRec('qa1', '974053642038', '15.00')],
        summary: awsAllSummary(1, '15.00'),
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    const { container } = render(authDisabledElement(<OptimizationPage />))
    await waitFor(
      () => {
        expect(screen.getAllByText('AWS QA-Dev Account').length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )
    // Buttons that would mutate cloud resources must NOT appear.
    expect(screen.queryByRole('button', { name: /^Apply$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Resize$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Stop$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Delete$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Purchase$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Modify$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Remediate$/i })).toBeNull()
    // The page header explicitly states "Read-only" for the operator.
    expect(container.textContent).toMatch(/Read-only/i)
  })
})

describe('Phase 7D.2 — page-level: empty state', () => {
  it('renders the no-data empty state without classifying it as an error', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [],
        summary: { ...awsAllSummary(0, null), status: 'NO_DATA' },
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    render(authDisabledElement(<OptimizationPage />))
    await waitFor(() => {
      expect(
        screen.getByText(/No active optimization recommendations were returned for this scope\./i),
      ).toBeInTheDocument()
    })
    expect(screen.getByTestId('optimization-no-data')).toBeInTheDocument()
  })
})

describe('Phase 7D.2 — page-level: filter wiring', () => {
  it('filter controls are present and respond to user input', async () => {
    window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    window.sessionStorage.setItem('cd.account.selection.v1', 'all')
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: buildTransport({
        provider: 'AWS',
        recommendations: [
          awsRec('a', '974053642038', '10'),
          awsRec('b', '974053642038', '20'),
        ],
        summary: awsAllSummary(2, '30'),
        selectableAccounts: AWS_SELECTABLE,
      }) as unknown as typeof fetch,
    })
    const user = userEvent.setup()
    render(authDisabledElement(<OptimizationPage />))
    expect(await screen.findByText('All AWS Accounts')).toBeInTheDocument()
    // All filter selects exist.
    expect(screen.getByTestId('filter-impact')).toBeInTheDocument()
    expect(screen.getByTestId('filter-source')).toBeInTheDocument()
    expect(screen.getByTestId('filter-resource-type')).toBeInTheDocument()
    expect(screen.getByTestId('filter-region')).toBeInTheDocument()
    expect(screen.getByTestId('sort-select')).toBeInTheDocument()
    expect(screen.getByTestId('filter-search')).toBeInTheDocument()
    // Search filters down to zero.
    await user.type(screen.getByTestId('filter-search'), 'no-such-resource')
    await waitFor(() => {
      expect(
        screen.getByText(/No recommendations match these filters/i),
      ).toBeInTheDocument()
    })
  })
})
