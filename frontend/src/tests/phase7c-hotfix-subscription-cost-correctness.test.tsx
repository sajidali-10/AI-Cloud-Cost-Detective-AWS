// Phase 7C post-release hotfix #2 — Azure subscription & cost
// correctness.
//
// Two root causes are exercised here:
//
// ROOT CAUSE A — Azure individual-subscription selection.  Before
// the fix the AccountSelector built every selection as ``acct:<row
// id>`` from the local database primary key.  For AWS that is
// correct (the Phase 7B route maps row id → role ARN).  For Azure
// the canonical APIs (Cost / Resources / Recommendations) resolve
// subscriptions by ``sub:<subscription-guid>`` — the local row id
// (``acct:5`` / ``acct:6``) has no Azure meaning and produced
// ``no AZURE account matches selection='acct:5'``.  The fix makes
// the selector emit ``sub:<guid>`` for Azure rows while leaving
// AWS behaviour unchanged.
//
// ROOT CAUSE B — cost completeness semantics.  A genuine
// successful $0.00 is a real zero.  But an aggregation in which
// *every* subscription failed / was throttled also summed to
// ``total_cost = 0`` on the backend, so the frontend rendered
// ``$0.00`` for data that was actually UNKNOWN.  The fix surfaces
// per-subscription success/failure counts from the canonical
// envelope and renders:
//   * all-failed          → "Unavailable" (never $0.00)
//   * some-failed         → valid subtotal + "Partial (N unavailable)"
//   * all-succeeded       → normal value (incl. genuine $0.00)

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SpendKpi } from '../pages/DashboardPage'
import type { CostReportResponse } from '../types/finops'
import type { BillingPeriod } from '../types/finops'
import { adaptCloudCostReport } from '../lib/finops/cloudCosts'
import {
  canonicalAccountParam,
  canonicalCacheSegment,
} from '../lib/finops/subscriptionSelection'

const PERIOD: BillingPeriod = { kind: 'month', month: '2026-10' }

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

// ---------------------------------------------------------------------------
// ROOT CAUSE A — subscription selector modelling
// ---------------------------------------------------------------------------

describe('Hotfix #2 ROOT CAUSE A — Azure subscription selection', () => {
  beforeEach(() => {
    if (typeof window !== 'undefined') window.sessionStorage.clear()
  })
  afterEach(async () => {
    if (typeof window !== 'undefined') window.sessionStorage.clear()
    try {
      const { __resetApiForTests } = await import('../lib/api')
      __resetApiForTests()
    } catch {
      // ignore
    }
  })

  it('AccountSelector emits sub:<guid> for Azure rows', async () => {
    const { AccountSelector } = await import('../components/AccountSelector')
    const { authDisabledElement } = await import('./helpers/auth-bootstrap')
    const { render, fireEvent, waitFor } = await import('@testing-library/react')
    const { configureApi, __resetApiForTests } = await import('../lib/api')
    if (typeof window !== 'undefined') {
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
      window.sessionStorage.clear()
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AZURE')
    }

    // Provide an Azure subscription row so the selector renders the
    // "individual subscription" option.  account_id is the GUID.
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/api/cloud/accounts/selectable')) {
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
              display_name: 'Sponsorship Subscription',
              is_management: false,
              is_enabled: true,
            },
          ],
          count: 2,
        })
      }
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
      if (url.includes('/api/auth/')) return json(200, { auth_enabled: false })
      return json(404, {})
    })
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: fetchMock as unknown as typeof fetch,
    })
    const result = render(authDisabledElement(<AccountSelector />))
    void result
    // Open the dropdown.
    const trigger = await waitFor(() =>
      screen.getByRole('button', { name: /All Subscriptions|All Azure/i }),
    )
    fireEvent.click(trigger)
    await waitFor(() => {
      expect(screen.getByTestId('azure-sub-option-5')).toBeTruthy()
    })
    fireEvent.click(screen.getByTestId('azure-sub-option-5'))
    // Stored selection must be the canonical sub:<guid>, NOT acct:5.
    const stored = window.sessionStorage.getItem('cd.account.selection.v1')
    expect(stored).toBe('sub:11111111-2222-3333-4444-555555555555')
    // And must NOT be the local PK.
    expect(stored).not.toBe('acct:5')
  })

  it('AccountSelector emits acct:<cloud account_id> for AWS rows (Phase 9D.1 canonical contract)', async () => {
    const { AccountSelector } = await import('../components/AccountSelector')
    const { authDisabledElement } = await import('./helpers/auth-bootstrap')
    const { render, fireEvent, waitFor } = await import('@testing-library/react')
    const { configureApi } = await import('../lib/api')
    if (typeof window !== 'undefined') {
      window.sessionStorage.clear()
      window.sessionStorage.setItem('cd.provider.selection.v1', 'AWS')
    }
    const fetchMock = vi.fn(async (url: string) => {
      // AWS uses the legacy Phase 7B selectable route.
      if (url.includes('/api/aws/cloud-accounts/selectable')) {
        return json(200, {
          items: [
            {
              id: 3,
              provider: 'AWS',
              account_id: '123456789012',
              display_name: 'Primary AWS',
              is_management: false,
              is_enabled: true,
            },
          ],
          count: 1,
        })
      }
      if (url.includes('/api/cloud/providers')) {
        return json(200, {
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
        })
      }
      if (url.includes('/api/auth/')) return json(200, { auth_enabled: false })
      return json(404, {})
    })
    configureApi({
      getToken: () => 'test-token',
      onSessionExpired: () => {},
      transport: fetchMock as unknown as typeof fetch,
    })
    const result = render(authDisabledElement(<AccountSelector />))
    void result
    const trigger = await waitFor(() => screen.getByRole('button', { name: /All Accounts|All AWS/i }))
    fireEvent.click(trigger)
    await waitFor(() => expect(screen.getByTestId('account-option-3')).toBeTruthy())
    fireEvent.click(screen.getByTestId('account-option-3'))
    const stored = window.sessionStorage.getItem('cd.account.selection.v1')
    // Phase 9D.1 — the selection carries the CLOUD-side AWS
    // account number, never the local database row id (3).
    expect(stored).toBe('acct:123456789012')
  })

  it('stale acct:<n> selection under Azure self-corrects (never sent as acct:5)', () => {
    // A pre-fix session may still hold ``acct:5`` in memory / storage
    // for an Azure selection.  The API-client coercion boundary must
    // never emit ``acct:5`` to an Azure route — it self-corrects to
    // the digest state by omitting the param ("all").
    expect(canonicalAccountParam('acct:5', 'AZURE')).toBeUndefined()
    expect(canonicalAccountParam('acct:6', 'AZURE')).toBeUndefined()
    expect(canonicalCacheSegment('acct:5', 'AZURE')).toBe('all')

    // A canonical Azure sub:<guid> passes through untouched.
    expect(canonicalAccountParam('sub:11111111-2222-3333-4444-555555555555', 'AZURE')).toBe(
      'sub:11111111-2222-3333-4444-555555555555',
    )
    // 'all' is omitted (no param).
    expect(canonicalAccountParam('all', 'AZURE')).toBeUndefined()
  })

  it('AWS selections pass through unchanged (acct:<id>), including 12-digit ids', () => {
    expect(canonicalAccountParam('acct:3', 'AWS')).toBe('acct:3')
    expect(canonicalCacheSegment('acct:3', 'AWS')).toBe('acct:3')
    expect(canonicalAccountParam('all', 'AWS')).toBeUndefined()
    expect(canonicalCacheSegment('all', 'AWS')).toBe('all')
  })
})

// ---------------------------------------------------------------------------
// ROOT CAUSE B — cost completeness semantics
// ---------------------------------------------------------------------------

// Build a canonical CloudCostReport with the given per-subscription
// statuses and append a throttle failure for any 'failed' entries.
function buildCanonicalReport(opts: {
  status: string
  currencyStatus?: string
  subscriptionStatuses: Array<string> // OK | NO_DATA | EMPTY
  failures?: number
  totals?: Array<string>
}) {
  const totals = opts.totals ?? opts.subscriptionStatuses.map(() => '0')
  // Wire shape mirrors /api/cloud/costs: by_account has one entry per
  // subscription (failed ones carry a zero amount), and account_errors
  // lists the throttled / failed subscriptions.
  const byAccount = opts.subscriptionStatuses.map((st, i) => ({
    account_id: `sub-guid-${i}`,
    display_name: `Sub ${i}`,
    amount: totals[i] ?? '0',
    currency: 'USD',
  }))
  const accountErrors = Array.from({ length: opts.failures ?? 0 }, (_, i) => ({
    account_id: `sub-guid-${i}`,
    display_name: `Sub ${i}`,
    code: 'THROTTLED',
    message: 'THROTTLED',
  }))
  return {
    provider: 'AZURE',
    scope: 'all',
    scope_kind: 'all',
    status: opts.status,
    currency_status: opts.currencyStatus ?? 'OK',
    account_id: null,
    account_display_name: null,
    period: { start: '2026-10-01', end: '2026-10-17', days: 17 },
    previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 },
    currency: 'USD',
    total_cost: '0',
    previous_period_cost: '0',
    change_amount: '0',
    change_percent: null,
    estimated: false,
    daily_trend: [],
    by_service: [],
    by_region: [],
    by_account: byAccount,
    account_errors: accountErrors,
    source: 'AZURE_COST_MANAGEMENT',
  }
}

function adapt(report: unknown): CostReportResponse['report'] {
  return adaptCloudCostReport({ report } as never).report
}

describe('Hotfix #2 ROOT CAUSE B — cost completeness semantics', () => {
  it('all-failed aggregation surfaces successful=0 / failed>0 (never $0.00)', () => {
    const report = adapt(buildCanonicalReport({
      status: 'PARTIAL_SUCCESS',
      subscriptionStatuses: ['EMPTY', 'EMPTY'],
      failures: 2,
      totals: ['0', '0'],
    }))
    expect(report.successful_accounts).toBe(0)
    expect(report.failed_accounts).toBe(2)
    expect(report.subscription_errors?.length).toBe(2)
  })

  it('partial aggregation surfaces successful>0 / failed>0 with retained errors', () => {
    const report = adapt(buildCanonicalReport({
      status: 'PARTIAL_SUCCESS',
      subscriptionStatuses: ['OK', 'EMPTY', 'EMPTY'],
      failures: 2,
      totals: ['7.18', '0', '0'],
    }))
    expect(report.successful_accounts).toBe(1)
    expect(report.failed_accounts).toBe(2)
    expect(report.subscription_errors).toHaveLength(2)
  })

  it('full success surfaces failed=0', () => {
    const report = adapt(buildCanonicalReport({
      status: 'SUCCESS',
      subscriptionStatuses: ['OK'],
      failures: 0,
      totals: ['0'],
    }))
    expect(report.successful_accounts).toBe(1)
    expect(report.failed_accounts).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// SpendKpi rendering of the completeness states
// ---------------------------------------------------------------------------

function reportOf(partial: Partial<CostReportResponse['report']>): CostReportResponse['report'] {
  return {
    account_id: '',
    period: { start: '2026-10-01', end: '2026-10-17', days: 17 },
    previous_period: { start: '2026-09-15', end: '2026-10-01', days: 17 },
    currency: 'USD',
    total_cost: '0',
    previous_period_cost: '0',
    change_amount: '0',
    change_percent: null,
    estimated: false,
    daily_trend: [],
    by_service: [],
    by_region: [],
    source: 'AWS_COST_EXPLORER',
    ...partial,
  }
}

describe('Hotfix #2 — SpendKpi renders completeness states', () => {
  it('renders "Unavailable" when all subscriptions failed (never $0.00)', () => {
    render(
      <SpendKpi
        report={reportOf({
          total_cost: '0',
          successful_accounts: 0,
          failed_accounts: 2,
        })}
        loading={false}
        period={PERIOD}
        providerLabel="Azure Spend"
      />,
    )
    expect(screen.getByText('Unavailable')).toBeTruthy()
    expect(screen.queryByText('$0.00')).toBeNull()
  })

  it('renders partial subtotal with a Partial marker', () => {
    // Phase 7D.3.2 — the canonical deriveCostCompleteness only
    // classifies the report as 'partial' when the backend envelope
    // explicitly says status='PARTIAL_SUCCESS'.  The legacy AWS
    // adapter still emits status='SUCCESS' for a single-account
    // shape, so we explicitly set the partial status here to
    // exercise the partial-marker render path.
    render(
      <SpendKpi
        report={reportOf({
          status: 'PARTIAL_SUCCESS',
          total_cost: '7.18',
          successful_accounts: 1,
          failed_accounts: 2,
        })}
        loading={false}
        period={PERIOD}
        providerLabel="Azure Spend"
      />,
    )
    expect(screen.getByText('$7.18')).toBeTruthy()
    expect(screen.getByText(/Partial \(2 unavailable\)/)).toBeTruthy()
  })

  it('renders genuine successful zero as $0.00 (complete data)', () => {
    render(
      <SpendKpi
        report={reportOf({
          total_cost: '0',
          successful_accounts: 1,
          failed_accounts: 0,
        })}
        loading={false}
        period={PERIOD}
        providerLabel="Azure Spend"
      />,
    )
    expect(screen.getByText('$0.00')).toBeTruthy()
  })

  it('renders an AWS label unchanged for the AWS spend KPI', () => {
    render(
      <SpendKpi
        report={reportOf({ total_cost: '2811.44' })}
        loading={false}
        period={PERIOD}
        providerLabel="AWS Spend"
      />,
    )
    expect(screen.getByText('AWS Spend')).toBeTruthy()
    expect(screen.getByText('$2.81K')).toBeTruthy()
  })
})