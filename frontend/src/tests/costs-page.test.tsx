// Phase 6B — Costs page tests.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { FinopsPeriodProvider } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { invalidateCache } from '../lib/finops/store'
import { CostsPage } from '../pages/CostsPage'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function wrap(node: React.ReactNode) {
  return render(
    <ThemeProvider>
      <FinopsPeriodProvider initialDays={30} initialRegion="all">{node}</FinopsPeriodProvider>
    </ThemeProvider>,
  )
}

function makeCosts(totalCost: string, previousCost: string, changePercent: string | null = '0.05') {
  return {
    report: {
      account_id: '974053642038',
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
    const transport = vi.fn(async (path: string) => {
      if (path.startsWith('/api/aws/costs')) return mockJson(200, makeCosts('1500.00', '1000.00', '0.5'))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)

    await waitFor(() => {
      expect(screen.getByText('$1.50K')).toBeInTheDocument()
    })
    expect(screen.getByText('+50.0%')).toBeInTheDocument()
  })

  it('renders "—" for missing previous cost (no divide by zero)', async () => {
    const transport = vi.fn(async (path: string) => {
      if (path.startsWith('/api/aws/costs')) return mockJson(200, makeCosts('100', '0', null))
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
      if (path.startsWith('/api/aws/costs')) return mockJson(200, makeCosts('1500.00', '1000.00'))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)
    await waitFor(() => {
      expect(screen.getByTestId('cost-trend-chart')).toBeInTheDocument()
    })
  })

  it('period selector changes lookback and triggers refetch', async () => {
    const calls: string[] = []
    const transport = vi.fn(async (path: string) => {
      calls.push(path)
      if (path.startsWith('/api/aws/costs')) return mockJson(200, makeCosts('1500.00', '1000.00'))
      return mockJson(404, {})
    })
    configureApi({ getToken: () => null, onSessionExpired: () => {}, transport: transport as unknown as typeof fetch })
    wrap(<CostsPage />)
    await waitFor(() => {
      expect(calls.some((c) => c.includes('days=30'))).toBe(true)
    })
    await userEvent.setup().click(screen.getByRole('radio', { name: '7d' }))
    await waitFor(() => {
      expect(calls.some((c) => c.includes('days=7'))).toBe(true)
    })
  })
})
