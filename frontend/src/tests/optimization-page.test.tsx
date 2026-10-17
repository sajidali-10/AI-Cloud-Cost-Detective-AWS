// Phase 7D.2 — Unified Optimization page tests.
//
// The Optimization page now consumes the canonical
// `/api/cloud/recommendations` endpoint for BOTH AWS and Azure,
// so the mocks below target that single route.  The page-level
// behavioural assertions (savings rendering, partial success,
// detail panel, savings-not-available) are preserved.

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
import { OptimizationPage } from '../pages/OptimizationPage'
import { MemoryRouter } from '../lib/router'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
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

const canonicalCloudRec = {
  provider: 'AWS',
  account_id: '974053642038',
  region: 'us-east-1',
  recommendation_id: 'r-1',
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

function makeSummaryNullSavings() {
  return {
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
    by_type: [{ key: 'REVIEW_DELETE_UNATTACHED_EBS', count: 1 }],
    by_subcategory: [],
    subscription_errors: [],
    by_account: [{ key: '974053642038', count: 1 }],
    by_source: [{ key: 'UNKNOWN', count: 1 }],
    by_action: [{ key: 'REVIEW_DELETE_UNATTACHED_EBS', count: 1 }],
    account_errors: [],
    source: 'AWS_OPTIMIZATION',
  }
}

function makeCanonicalRecsResponse(opts: {
  recommendations: Array<Record<string, unknown>>
  summary: Record<string, unknown>
}) {
  return {
    summary: opts.summary,
    recommendations: opts.recommendations,
  }
}

describe('OptimizationPage (Phase 7D.2 unified)', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
  })

  it('renders recommendation rows with null savings as "Not available"', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/recommendations')) {
        return mockJson(200, makeCanonicalRecsResponse({
          recommendations: [canonicalCloudRec],
          summary: makeSummaryNullSavings(),
        }))
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    expect(await screen.findByTestId('savings-not-available')).toBeInTheDocument()
    // Resource id appears as the mono-font cell content; the
    // friendly resource_name also surfaces — assert at least one.
    expect((await screen.findAllByText('vol-001')).length).toBeGreaterThan(0)
    expect(await screen.findByText('Unattached EBS volume vol-001')).toBeInTheDocument()
  })

  it('renders an authoritative monthly savings value when present', async () => {
    const awsRec = {
      ...canonicalCloudRec,
      source: 'AWS_COST_OPTIMIZATION_HUB',
      estimated_savings: '45.00',
    }
    const summary = {
      ...makeSummaryNullSavings(),
      status: 'SUCCESS',
      savings_status: 'OK',
      total_recommendations: 1,
      recommendations_with_savings: 1,
      recommendations_without_savings: 0,
      potential_savings: '45.00',
      by_source: [{ key: 'AWS_COST_OPTIMIZATION_HUB', count: 1 }],
    }
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/recommendations')) {
        return mockJson(200, makeCanonicalRecsResponse({ recommendations: [awsRec], summary }))
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    // The KPI and the row both carry the savings figure — assert
    // at least one matching element lands in the document.
    expect((await screen.findAllByText(/USD\s*45\.00/)).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('AWS Cost Optimization Hub')).length).toBeGreaterThan(0)
  })

  it('renders partial-success state without blanking the page', async () => {
    const partialSummary = {
      ...makeSummaryNullSavings(),
      status: 'PARTIAL_SUCCESS',
      account_errors: [
        {
          account_id: 'secondary',
          display_name: 'Secondary',
          code: 'AccessDenied',
          message: 'Access denied',
        },
      ],
    }
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/recommendations')) {
        return mockJson(200, makeCanonicalRecsResponse({
          recommendations: [canonicalCloudRec],
          summary: partialSummary,
        }))
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    expect(await screen.findByText('PARTIAL_SUCCESS')).toBeInTheDocument()
    expect((await screen.findAllByTestId('partial-warning')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('vol-001')).length).toBeGreaterThan(0)
  })

  it('hides the detail panel when multiple rows match the filter', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path === '/api/auth/info') return mockJson(200, { auth_enabled: false })
      if (path.startsWith('/api/cloud/recommendations')) {
        return mockJson(200, makeCanonicalRecsResponse({
          recommendations: [
            canonicalCloudRec,
            { ...canonicalCloudRec, recommendation_id: 'r-2', resource_id: 'vol-002' },
          ],
          summary: { ...makeSummaryNullSavings(), total_recommendations: 2 },
        }))
      }
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    expect((await screen.findAllByText('vol-001')).length).toBeGreaterThan(0)
    expect((await screen.findAllByText('vol-002')).length).toBeGreaterThan(0)
    // Detail panel only renders when filter narrows to a single row.
    expect(screen.queryByTestId('optimization-detail-panel')).toBeNull()
  })
})
