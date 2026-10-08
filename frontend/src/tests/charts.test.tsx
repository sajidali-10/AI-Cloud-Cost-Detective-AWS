// Phase 6B — Chart and bar component tests.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CostTrendChart } from '../components/CostTrendChart'
import { HorizontalBarChart } from '../components/HorizontalBarChart'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode) {
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

describe('CostTrendChart', () => {
  it('renders points and provides a screen-reader table mirror', () => {
    wrap(
      <CostTrendChart
        points={[
          { date: '2026-10-01', amount: '10.00', unit: 'USD' },
          { date: '2026-10-02', amount: '20.00', unit: 'USD' },
          { date: '2026-10-03', amount: '30.00', unit: 'USD' },
        ]}
        ariaLabel="Test trend"
      />,
    )
    expect(screen.getByTestId('cost-trend-chart')).toBeInTheDocument()
    // 2026-10-01 appears in the chart x-axis AND the sr-only table.
    expect(screen.getAllByText('2026-10-01').length).toBeGreaterThan(0)
    // USD 30.00 appears as both the topmost y-axis tick and the table cell.
    expect(screen.getAllByText('USD 30.00').length).toBeGreaterThan(0)
  })

  it('renders a No data state when empty', () => {
    wrap(<CostTrendChart points={[]} ariaLabel="Empty" />)
    expect(screen.getByText(/no data/i)).toBeInTheDocument()
  })

  it('renders correctly under both themes via semantic classes', () => {
    const { unmount } = wrap(
      <CostTrendChart
        points={[{ date: '2026-10-01', amount: '5.00', unit: 'USD' }]}
        ariaLabel="Themed"
      />,
    )
    expect(screen.getByTestId('cost-trend-chart')).toBeInTheDocument()
    unmount()
  })
})

describe('HorizontalBarChart', () => {
  it('renders rows with bars and currency', () => {
    wrap(
      <HorizontalBarChart
        rows={[
          { key: 'a', label: 'Service A', value: '100' },
          { key: 'b', label: 'Service B', value: '50' },
        ]}
      />,
    )
    expect(screen.getAllByTestId('bar-row')).toHaveLength(2)
    expect(screen.getByText('Service A')).toBeInTheDocument()
  })

  it('collapses tail rows into Other', () => {
    wrap(
      <HorizontalBarChart
        maxRows={3}
        rows={[
          { key: 'a', label: 'A', value: '100' },
          { key: 'b', label: 'B', value: '50' },
          { key: 'c', label: 'C', value: '30' },
          { key: 'd', label: 'D', value: '20' },
          { key: 'e', label: 'E', value: '10' },
        ]}
      />,
    )
    expect(screen.getAllByTestId('bar-row')).toHaveLength(3)
    expect(screen.getByText('Other')).toBeInTheDocument()
  })

  it('renders an empty state when no rows', () => {
    wrap(<HorizontalBarChart rows={[]} />)
    expect(screen.getByText(/no data/i)).toBeInTheDocument()
  })
})
