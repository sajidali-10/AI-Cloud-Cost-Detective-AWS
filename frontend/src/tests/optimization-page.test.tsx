// Phase 6B — Optimization page tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { OptimizationPage } from '../pages/OptimizationPage'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function wrapRoutes(node: React.ReactNode) {
  return render(
    <ThemeProvider>
      <FinopsPeriodProvider initialDays={30} initialRegion="all">{node}</FinopsPeriodProvider>
    </ThemeProvider>,
  )
}

const baseCaps = {
  region: 'us-east-1',
  account_id: '974053642038',
  compute_optimizer: { status: 'INACTIVE', detail: null, last_checked_at: null, error_code: null },
  cost_optimization_hub: { status: 'NOT_ENROLLED', detail: null, last_checked_at: null, error_code: null },
  deterministic_engine: { status: 'AVAILABLE', detail: null, last_checked_at: null, error_code: null },
  supported_resource_types: [],
  supported_lookback_days: [7, 30, 60, 90],
  warnings: [],
}

const rec = {
  recommendation_id: 'r-1',
  resource_id: 'vol-001',
  resource_arn: null,
  resource_type: 'EBS_VOLUME',
  region: 'us-east-1',
  account_id: '974053642038',
  action: 'REVIEW_DELETE_UNATTACHED_EBS',
  title: 'Unattached EBS volume vol-001',
  finding: 'Volume has been detached for 30 days.',
  current_configuration: { size_gb: 100, attachments: 0 },
  recommended_configuration: { recommendation: 'REVIEW_DELETE_UNATTACHED_EBS' },
  estimated_monthly_savings: null,
  currency: 'USD',
  savings_percentage: null,
  savings_source: 'UNKNOWN',
  primary_source: 'UNKNOWN',
  sources: ['UNKNOWN'],
  confidence: 'HIGH',
  data_quality: 'high',
  reason_codes: ['EBS_NOT_ATTACHED'],
  restart_needed: null,
  rollback_possible: true,
  evidence: [],
  aws_recommendation_ids: [],
  detected_at: null,
}

function makeSummaryNullSavings() {
  return {
    region: 'us-east-1', account_id: '974053642038', days: 30, status: 'SUCCESS',
    total_recommendations: 1, total_estimated_monthly_savings: null, currency: 'USD',
    by_resource_type: [], by_action: [],
    by_source: [{ key: 'UNKNOWN', count: 1, estimated_monthly_savings: null, currency: 'USD' }],
    by_confidence: [{ key: 'HIGH', count: 1, estimated_monthly_savings: null, currency: 'USD' }],
    recommendations_without_savings: 1, warnings: [],
  }
}

describe('OptimizationPage', () => {
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
      if (path.startsWith('/api/aws/optimization/capabilities')) return mockJson(200, baseCaps)
      if (path.startsWith('/api/aws/optimization/recommendations')) return mockJson(200, { region: 'us-east-1', account_id: '974053642038', days: 30, status: 'SUCCESS', count: 1, recommendations: [rec], warnings: [] })
      if (path.startsWith('/api/aws/optimization/summary')) return mockJson(200, makeSummaryNullSavings())
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    await waitFor(() => {
      expect(screen.getByTestId('savings-not-available')).toBeInTheDocument()
    })
    expect(screen.getByText('vol-001')).toBeInTheDocument()
    expect(screen.getByText('Unattached EBS volume vol-001')).toBeInTheDocument()
    expect(screen.getAllByText('Deterministic').length).toBeGreaterThan(0)
  })

  it('renders an authoritative savings value when present', async () => {
    const awsRec = { ...rec, primary_source: 'AWS_COST_OPTIMIZATION_HUB', sources: ['AWS_COST_OPTIMIZATION_HUB'], savings_source: 'AWS_COST_OPTIMIZATION_HUB', estimated_monthly_savings: '45.00' }
    const summary = {
      region: 'us-east-1', account_id: '974053642038', days: 30, status: 'SUCCESS',
      total_recommendations: 1, total_estimated_monthly_savings: '45.00', currency: 'USD',
      by_resource_type: [{ key: 'EBS_VOLUME', count: 1, estimated_monthly_savings: '45.00', currency: 'USD' }],
      by_action: [],
      by_source: [{ key: 'AWS_COST_OPTIMIZATION_HUB', count: 1, estimated_monthly_savings: '45.00', currency: 'USD' }],
      by_confidence: [{ key: 'HIGH', count: 1, estimated_monthly_savings: '45.00', currency: 'USD' }],
      recommendations_without_savings: 0, warnings: [],
    }
    const transport = vi.fn(async (path: string) => {
      if (path.startsWith('/api/aws/optimization/capabilities')) return mockJson(200, baseCaps)
      if (path.startsWith('/api/aws/optimization/recommendations')) return mockJson(200, { region: 'us-east-1', account_id: '974053642038', days: 30, status: 'SUCCESS', count: 1, recommendations: [awsRec], warnings: [] })
      if (path.startsWith('/api/aws/optimization/summary')) return mockJson(200, summary)
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    await waitFor(() => {
      // USD 45.00 / mo in the table cell
      expect(screen.getByText(/USD\s*45\.00/)).toBeInTheDocument()
    })
    expect(screen.getByText('AWS Cost Optimization Hub')).toBeInTheDocument()
  })

  it('renders partial-success state without blanking the page', async () => {
    const warning = { source: 'compute_optimizer', code: 'AccessDenied', message: 'Access denied', region: 'us-east-1' }
    const transport = vi.fn(async (path: string) => {
      if (path.startsWith('/api/aws/optimization/capabilities')) return mockJson(200, { ...baseCaps, warnings: [warning] })
      if (path.startsWith('/api/aws/optimization/recommendations')) return mockJson(200, { region: 'us-east-1', account_id: '974053642038', days: 30, status: 'PARTIAL_SUCCESS', count: 1, recommendations: [rec], warnings: [warning] })
      if (path.startsWith('/api/aws/optimization/summary')) return mockJson(200, { ...makeSummaryNullSavings(), status: 'PARTIAL_SUCCESS', warnings: [warning] })
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    await waitFor(() => {
      expect(screen.getAllByTestId('partial-warning').length).toBeGreaterThan(0)
    })
    expect(screen.getByText('PARTIAL_SUCCESS')).toBeInTheDocument()
    expect(screen.getByText('vol-001')).toBeInTheDocument()
  })

  it('renders the recommendation detail panel when a row is clicked', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path.startsWith('/api/aws/optimization/capabilities')) return mockJson(200, baseCaps)
      if (path.startsWith('/api/aws/optimization/recommendations')) return mockJson(200, { region: 'us-east-1', account_id: '974053642038', days: 30, status: 'SUCCESS', count: 1, recommendations: [rec], warnings: [] })
      if (path.startsWith('/api/aws/optimization/summary')) return mockJson(200, makeSummaryNullSavings())
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })

    wrapRoutes(<OptimizationPage />)
    await waitFor(() => {
      expect(screen.getByText('vol-001')).toBeInTheDocument()
    })
    await userEvent.setup().click(screen.getByText('vol-001'))
    expect(screen.getByTestId('recommendation-detail')).toBeInTheDocument()
    expect(screen.getByText('Volume has been detached for 30 days.')).toBeInTheDocument()
  })
})
