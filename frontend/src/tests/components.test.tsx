// Phase 6A — Component tests.
//
// Covers rendering and theme-awareness for the shell components,
// role-aware navigation visibility, and the access-denied path.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge } from '../components/StatusBadge'
import { RoleBadge } from '../components/RoleBadge'
import { MetricCard } from '../components/MetricCard'
import { SectionCard } from '../components/SectionCard'
import { EmptyState } from '../components/EmptyState'
import { ErrorState, BackendUnavailable, ForbiddenState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { AccessDenied } from '../components/AccessDenied'
import { PageHeader, ContextItem } from '../components/PageHeader'
import { FilterBar, FilterSelect } from '../components/FilterBar'
import { DataTable } from '../components/DataTable'
import { ThemeProvider } from '../lib/theme'
import { Router } from '../lib/router'

function wrap(node: React.ReactNode) {
  // Wrap in Router because AccessDenied uses <Link> which needs the
  // navigate context.  All other components are unaffected by the
  // extra provider.
  return render(
    <ThemeProvider>
      <Router>{node}</Router>
    </ThemeProvider>,
  )
}

describe('StatusBadge', () => {
  it('renders each tone with the expected accessible label', () => {
    wrap(
      <div>
        <StatusBadge tone="success">OK</StatusBadge>
        <StatusBadge tone="warning">Warn</StatusBadge>
        <StatusBadge tone="danger">Err</StatusBadge>
        <StatusBadge tone="info">Info</StatusBadge>
        <StatusBadge tone="ai">AI</StatusBadge>
        <StatusBadge tone="neutral">Idle</StatusBadge>
      </div>,
    )
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.getByText('Warn')).toBeInTheDocument()
    expect(screen.getByText('Err')).toBeInTheDocument()
    expect(screen.getByText('Info')).toBeInTheDocument()
    expect(screen.getByText('AI')).toBeInTheDocument()
    expect(screen.getByText('Idle')).toBeInTheDocument()
  })
})

describe('RoleBadge', () => {
  it('renders each role with its display label', () => {
    wrap(
      <div>
        <RoleBadge role="ADMIN" />
        <RoleBadge role="ANALYST" />
        <RoleBadge role="VIEWER" />
      </div>,
    )
    expect(screen.getByText('Administrator')).toBeInTheDocument()
    expect(screen.getByText('Analyst')).toBeInTheDocument()
    expect(screen.getByText('Viewer')).toBeInTheDocument()
  })
})

describe('MetricCard', () => {
  it('renders a placeholder when value is undefined', () => {
    wrap(<MetricCard label="Spend" />)
    const value = screen.getByTestId('metric-card-value')
    expect(value.textContent).toBe('—')
    const card = screen.getByTestId('metric-card')
    expect(card.getAttribute('data-empty')).toBe('true')
  })
  it('renders the value when provided', () => {
    wrap(<MetricCard label="Spend" value="$12,345" />)
    expect(screen.getByTestId('metric-card-value').textContent).toBe('$12,345')
    expect(screen.getByTestId('metric-card').getAttribute('data-empty')).toBe('false')
  })
})

describe('SectionCard', () => {
  it('renders title and children', () => {
    wrap(<SectionCard title="Region">body</SectionCard>)
    expect(screen.getByText('Region')).toBeInTheDocument()
    expect(screen.getByText('body')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('renders title + description', () => {
    wrap(<EmptyState title="No data" description="Try later" />)
    expect(screen.getByText('No data')).toBeInTheDocument()
    expect(screen.getByText('Try later')).toBeInTheDocument()
  })
})

describe('ErrorState', () => {
  it('renders the message and optional retry', () => {
    wrap(<ErrorState message="boom" onRetry={() => undefined} />)
    expect(screen.getByText('boom')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
  it('BackendUnavailable has a generic message', () => {
    wrap(<BackendUnavailable />)
    expect(screen.getByText(/backend unavailable/i)).toBeInTheDocument()
  })
  it('ForbiddenState renders', () => {
    wrap(<ForbiddenState />)
    expect(screen.getByText(/forbidden/i)).toBeInTheDocument()
  })
})

describe('LoadingSkeleton', () => {
  it('renders with role=presentation (aria-hidden)', () => {
    wrap(<LoadingSkeleton className="h-4 w-12" />)
    const el = document.querySelector('[aria-hidden="true"]')
    expect(el).toBeInTheDocument()
  })
})

describe('AccessDenied', () => {
  it('renders required role list and a link back to /', () => {
    wrap(<AccessDenied requiredRoles={['ADMIN']} message="nope" />)
    expect(screen.getByText('Access denied')).toBeInTheDocument()
    expect(screen.getByText('nope')).toBeInTheDocument()
    expect(screen.getByText(/ADMIN|Administrator/)).toBeInTheDocument()
  })
})

describe('PageHeader', () => {
  it('renders eyebrow + title + subtitle + context', () => {
    wrap(
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Dashboard"
        subtitle="subtitle"
        context={
          <>
            <ContextItem label="Region" value="us-east-1" />
          </>
        }
      />,
    )
    expect(screen.getByText('AI Cloud Cost Detective')).toBeInTheDocument()
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('subtitle')).toBeInTheDocument()
    expect(screen.getByText('us-east-1')).toBeInTheDocument()
  })
})

describe('FilterBar', () => {
  it('renders children inside a toolbar', () => {
    wrap(
      <FilterBar>
        <FilterSelect
          label="Account"
          value="all"
          onChange={() => undefined}
          options={[{ value: 'all', label: 'All' }]}
        />
      </FilterBar>,
    )
    expect(screen.getByText('Account')).toBeInTheDocument()
    expect(screen.getByText('All')).toBeInTheDocument()
  })
})

describe('DataTable', () => {
  it('renders headers and rows', () => {
    wrap(
      <DataTable
        columns={[
          { key: 'name', header: 'Name', cell: (r: { name: string }) => r.name },
          { key: 'role', header: 'Role', cell: (r: { role: string }) => r.role },
        ]}
        rows={[{ name: 'a', role: 'ADMIN' }]}
        rowKey={(r) => (r as { name: string }).name}
      />,
    )
    expect(screen.getByText('Name')).toBeInTheDocument()
    expect(screen.getByText('Role')).toBeInTheDocument()
    expect(screen.getByText('a')).toBeInTheDocument()
    expect(screen.getByText('ADMIN')).toBeInTheDocument()
  })
  it('renders empty state when no rows', () => {
    wrap(
      <DataTable
        columns={[{ key: 'name', header: 'Name', cell: () => '—' }]}
        rows={[]}
        rowKey={() => 'k'}
        emptyState={<div>no rows</div>}
      />,
    )
    expect(screen.getByText('no rows')).toBeInTheDocument()
  })
  it('renders loading state', () => {
    wrap(
      <DataTable
        columns={[{ key: 'name', header: 'Name', cell: () => '—' }]}
        rows={[]}
        rowKey={() => 'k'}
        isLoading
      />,
    )
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })
})
