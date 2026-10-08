// Phase 6B — Dashboard page tests with mocked transport.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { ThemeProvider } from '../lib/theme'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { DashboardPage } from '../pages/DashboardPage'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function wrapRoutes(node: React.ReactNode) {
  return render(
    <ThemeProvider>
      <FinopsPeriodProvider initialDays={30} initialRegion="all">{node}</FinopsPeriodProvider>
    </ThemeProvider>,
  )
}

// The Dashboard fans out 5 useFinopsQuery fetches in parallel;
// each one's `finally` block emits a state transition via
// useSyncExternalStore from its own microtask.  Using findBy* /
// waitFor keeps every DOM assertion inside an act() boundary
// (RTL wraps both automatically), and useSyncExternalStore
// integrates natively with React 18's act scheduler.

function makeCostsBody(days = 30) {
  return {
    report: {
      account_id: '974053642038',
      period: { start: '2026-09-08', end: '2026-10-08', days },
      previous_period: { start: '2026-08-09', end: '2026-09-07', days },
      currency: 'USD',
      total_cost: '2438.08',
      previous_period_cost: '2300.00',
      change_amount: '138.08',
      change_percent: '0.06',
      estimated: false,
      daily_trend: [
        { date: '2026-10-01', amount: '80.00', unit: 'USD' },
        { date: '2026-10-02', amount: '90.00', unit: 'USD' },
        { date: '2026-10-03', amount: '100.00', unit: 'USD' },
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
      source: 'AWS_COST_EXPLORER',
    },
    cache_status: 'HIT',
    cached_at: '2026-10-08T14:42:00Z',
    expires_at: '2026-10-09T14:42:00Z',
  }
}

function makeResourcesBody() {
  return {
    region: 'us-east-1',
    services: {
      ec2: { service: 'ec2', status: 'ok', items: [{ instance_id: 'i-1', state: 'running', instance_type: 't3.micro', region: 'us-east-1', tags: { Name: 'web' } }], error_code: null },
      ebs: { service: 'ebs', status: 'ok', items: [], error_code: null },
      eip: { service: 'eip', status: 'ok', items: [], error_code: null },
      nat: { service: 'nat', status: 'ok', items: [], error_code: null },
      elbv2: { service: 'elbv2', status: 'ok', items: [], error_code: null },
      rds: { service: 'rds', status: 'ok', items: [], error_code: null },
      lambda: { service: 'lambda', status: 'ok', items: [], error_code: null },
      s3: { service: 's3', status: 'ok', items: [], error_code: null },
    },
    enrichment: {},
  }
}

function makeCapsBody() {
  return {
    region: 'us-east-1',
    account_id: '974053642038',
    compute_optimizer: { status: 'INACTIVE', detail: null, last_checked_at: null, error_code: null },
    cost_optimization_hub: { status: 'NOT_ENROLLED', detail: null, last_checked_at: null, error_code: null },
    deterministic_engine: { status: 'AVAILABLE', detail: null, last_checked_at: null, error_code: null },
    supported_resource_types: [],
    supported_lookback_days: [7, 30, 60, 90],
    warnings: [],
  }
}

function makeSummaryBody() {
  return {
    region: 'us-east-1',
    account_id: '974053642038',
    days: 30,
    status: 'SUCCESS',
    total_recommendations: 12,
    total_estimated_monthly_savings: '320.50',
    currency: 'USD',
    by_resource_type: [
      { key: 'EBS_VOLUME', count: 9, estimated_monthly_savings: null, currency: 'USD' },
      { key: 'ELASTIC_IP', count: 3, estimated_monthly_savings: null, currency: 'USD' },
    ],
    by_action: [],
    by_source: [{ key: 'UNKNOWN', count: 12, estimated_monthly_savings: null, currency: 'USD' }],
    by_confidence: [{ key: 'HIGH', count: 12, estimated_monthly_savings: null, currency: 'USD' }],
    recommendations_without_savings: 12,
    warnings: [],
  }
}

function makeIdentity() {
  return {
    account: '974053642038',
    arn: 'arn:aws:sts::974053642038:assumed-role/x',
    user_id: 'AROA:abc',
    region: 'us-east-1',
  }
}

function installMockRoutes(opts: { costs?: Response; summary?: any } = {}) {
  const transport = vi.fn(async (path: string) => {
    if (path.startsWith('/api/aws/identity')) return mockJson(200, makeIdentity())
    if (path.startsWith('/api/aws/costs')) return opts.costs ?? mockJson(200, makeCostsBody())
    if (path.startsWith('/api/aws/resources')) return mockJson(200, makeResourcesBody())
    if (path.startsWith('/api/aws/optimization/capabilities')) return mockJson(200, makeCapsBody())
    if (path.startsWith('/api/aws/optimization/summary')) return mockJson(200, opts.summary ?? makeSummaryBody())
    if (path.startsWith('/api/aws/optimization/recommendations')) return mockJson(200, { count: 0, recommendations: [] })
    return mockJson(404, { message: 'not found' })
  })
  configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
  return transport
}

describe('DashboardPage', () => {
  beforeEach(() => {
    __resetApiForTests()
    invalidateCache()
  })
  afterEach(() => {
    invalidateCache()
    cleanup()
  })

  it('renders real cost, change, resource and optimization KPIs', async () => {
    installMockRoutes()
    wrapRoutes(<DashboardPage />)

    await waitFor(() => {
      expect(screen.getAllByText(/9740…2038/).length).toBeGreaterThan(0)
    })
    expect(await screen.findByText('$2.44K')).toBeInTheDocument()
    expect(await screen.findByText('+6.0%')).toBeInTheDocument()
    // Resources KPI: 1 EC2 instance
    await waitFor(() => {
      expect(screen.getByTestId('resources-total-kpi')).toHaveTextContent('1')
    })
    // Optimization KPI: 12 recommendations
    await waitFor(() => {
      expect(screen.getByTestId('optimization-count').textContent).toBe('12')
    })
    await waitFor(() => {
      expect(screen.getAllByText(/open opportunities/i).length).toBeGreaterThan(0)
    })
    expect(await screen.findByTestId('dashboard-capabilities')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getAllByText('Inactive').length).toBeGreaterThan(0)
    })
    await waitFor(() => {
      expect(screen.getAllByText('Not enrolled').length).toBeGreaterThan(0)
    })
  })

  it('renders "Savings not available" when authoritative savings is null', async () => {
    installMockRoutes({ summary: { ...makeSummaryBody(), total_estimated_monthly_savings: null, recommendations_without_savings: 12 } })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      // Two renderings: the Optimize KPI description AND the Optimization Summary "savings not available" line.
      expect(screen.getAllByText(/savings not available/i).length).toBeGreaterThan(0)
    })
  })

  it('shows an error banner when the costs endpoint fails', async () => {
    installMockRoutes({ costs: mockJson(502, { error_code: 'ServerDown' }) })
    wrapRoutes(<DashboardPage />)
    await waitFor(() => {
      expect(screen.getByText(/some data could not be loaded/i)).toBeInTheDocument()
    })
  })
})
