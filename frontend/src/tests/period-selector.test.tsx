// Phase 6B — Period selector and region filter tests.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { FinopsPeriodProvider, useFinopsPeriod } from '../lib/finops/period'
import { PeriodSelector } from '../components/PeriodSelector'
import { RegionFilter, buildRegionOptions } from '../components/RegionFilter'

afterEach(cleanup)

function wrap(node: React.ReactNode) {
  return render(
    <ThemeProvider>
      <FinopsPeriodProvider initialDays={30} initialRegion="all">
        {node}
      </FinopsPeriodProvider>
    </ThemeProvider>,
  )
}

function PeriodReader({ onDays }: { onDays: (d: number) => void }) {
  const { days } = useFinopsPeriod()
  onDays(days)
  return null
}

describe('PeriodSelector', () => {
  it('renders 7d/30d/60d/90d with 30d default active', () => {
    let days = 0
    wrap(
      <>
        <PeriodSelector />
        <PeriodReader onDays={(d) => { days = d }} />
      </>,
    )
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(4)
    expect(radios.find((r) => r.textContent === '30d')).toHaveAttribute('aria-checked', 'true')
    expect(days).toBe(30)
  })

  it('changes the period on click', async () => {
    let days = 0
    wrap(
      <>
        <PeriodSelector />
        <PeriodReader onDays={(d) => { days = d }} />
      </>,
    )
    await userEvent.setup().click(screen.getByRole('radio', { name: '7d' }))
    expect(days).toBe(7)
  })
})

describe('RegionFilter', () => {
  it('renders options derived from observed regions', () => {
    const opts = buildRegionOptions(['us-east-1', 'eu-west-1', 'global', null])
    expect(opts[0]).toEqual({ value: 'all', label: 'All / account-level' })
    expect(opts.find((o) => o.value === 'global')).toEqual({ value: 'global', label: 'Global / No Region' })
  })

  it('renders the select with options', () => {
    wrap(
      <RegionFilter
        options={[
          { value: 'all', label: 'All / account-level' },
          { value: 'us-east-1', label: 'us-east-1' },
        ]}
      />,
    )
    expect(screen.getByText('Region')).toBeInTheDocument()
    expect(screen.getByText('All / account-level')).toBeInTheDocument()
  })
})
