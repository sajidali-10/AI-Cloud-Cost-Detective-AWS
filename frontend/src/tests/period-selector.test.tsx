// Phase 6B — Period selector and region filter tests.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider } from '../lib/theme'
import { AuthProvider } from '../lib/auth'
import { FinopsPeriodProvider, useFinopsPeriod } from '../lib/finops/period'
import { configureApi, __resetApiForTests } from '../lib/api'
import { PeriodSelector } from '../components/PeriodSelector'
import { RegionFilter, buildRegionOptions } from '../components/RegionFilter'
import { MemoryRouter } from '../lib/router'

afterEach(cleanup)

beforeEach(() => {
  // Phase 7A: AuthProvider fires /api/auth/info on mount; mock the
  // transport so the bootstrap resolves to auth_enabled=false and
  // lands on the synthetic-anon 'disabled' phase.
  __resetApiForTests()
  configureApi({
    getToken: () => null,
    onSessionExpired: () => {},
    onForbidden: () => {},
    transport: (async (input: RequestInfo | URL) => {
      const path = typeof input === 'string' ? input : input.toString()
      if (path === '/api/auth/info') {
        return new Response(JSON.stringify({ auth_enabled: false }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({}), { status: 404 })
    }) as unknown as typeof fetch,
  })
})

function wrap(node: React.ReactNode, opts: { initialPeriod?: { kind: 'month'; month: string } } = {}) {
  return render(
    <MemoryRouter>
      <ThemeProvider>
        <AuthProvider>
          <FinopsPeriodProvider
            {...(opts.initialPeriod ? { initialPeriod: opts.initialPeriod } : {})}
            initialRegion="all"
          >
            {node}
          </FinopsPeriodProvider>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

function PeriodReader({ onPeriod }: { onPeriod: (p: { kind: 'month'; month: string } | { kind: 'days'; days: number }) => void }) {
  const { period } = useFinopsPeriod()
  onPeriod(period)
  return null
}

describe('PeriodSelector', () => {
  it('renders the calendar-month select with the current month selected', () => {
    let observed: { kind: 'month' | 'days'; [k: string]: unknown } | null = null
    const { container } = wrap(
      <>
        <PeriodSelector />
        <PeriodReader onPeriod={(p) => { observed = p as { kind: 'month' | 'days' } }} />
      </>,
    )
    // The Phase 7A selector renders a native <select>, not a radio
    // group.  The provider always lands on a calendar month by
    // default.
    const select = container.querySelector('select[data-testid="period-selector-input"]')
    expect(select).not.toBeNull()
    const options = select!.querySelectorAll('option')
    expect(options.length).toBeGreaterThanOrEqual(12)
    expect(observed?.kind).toBe('month')
  })

  it('changes the month on selection', async () => {
    let observedMonth = ''
    const { container } = wrap(
      <>
        <PeriodSelector />
        <PeriodReader onPeriod={(p) => { if (p.kind === 'month') observedMonth = p.month }} />
      </>,
    )
    const select = container.querySelector('select[data-testid="period-selector-input"]') as HTMLSelectElement
    const options = Array.from(select.querySelectorAll('option')) as HTMLOptionElement[]
    expect(options.length).toBeGreaterThan(1)
    const target = options[options.length - 2] // second-to-last month
    const user = userEvent.setup()
    await user.selectOptions(select, target.value)
    expect(observedMonth).toBe(target.value)
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
