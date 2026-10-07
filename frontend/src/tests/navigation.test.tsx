// Phase 6A — Role-aware navigation tests.
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { navItemsForRole, isRoleAllowed, roleDisplayLabel } from '../lib/tokens'

describe('navItemsForRole', () => {
  it('returns nothing for null / undefined role', () => {
    expect(navItemsForRole(null)).toEqual([])
    expect(navItemsForRole(undefined)).toEqual([])
  })
  it('returns base items for VIEWER', () => {
    const items = navItemsForRole('VIEWER')
    const labels = items.map((i) => i.label)
    expect(labels).toEqual(['Dashboard', 'Costs', 'Resources', 'Optimization'])
  })
  it('adds AI Analyst and Conversations for ANALYST', () => {
    const items = navItemsForRole('ANALYST')
    const labels = items.map((i) => i.label)
    expect(labels).toContain('AI Cost Analyst')
    expect(labels).toContain('Conversations')
    expect(labels).not.toContain('Users')
    expect(labels).not.toContain('Security')
  })
  it('adds Users and Security for ADMIN', () => {
    const items = navItemsForRole('ADMIN')
    const labels = items.map((i) => i.label)
    expect(labels).toContain('Users')
    expect(labels).toContain('Security')
    expect(labels).toContain('AI Cost Analyst')
    expect(labels).toContain('Conversations')
  })
  it('exposes ordered nav (Dashboard first)', () => {
    const items = navItemsForRole('ADMIN')
    expect(items[0].label).toBe('Dashboard')
  })
})

describe('isRoleAllowed', () => {
  it('returns false when role is null/undefined', () => {
    expect(isRoleAllowed(null, ['ADMIN'])).toBe(false)
    expect(isRoleAllowed(undefined, ['ADMIN'])).toBe(false)
  })
  it('returns true when role is in the allowed list', () => {
    expect(isRoleAllowed('ADMIN', ['ADMIN', 'ANALYST'])).toBe(true)
    expect(isRoleAllowed('VIEWER', ['VIEWER'])).toBe(true)
  })
  it('returns false when role is not in the allowed list', () => {
    expect(isRoleAllowed('VIEWER', ['ADMIN'])).toBe(false)
  })
})

describe('roleDisplayLabel', () => {
  it('returns the friendly label for each role', () => {
    expect(roleDisplayLabel('ADMIN')).toBe('Administrator')
    expect(roleDisplayLabel('ANALYST')).toBe('Analyst')
    expect(roleDisplayLabel('VIEWER')).toBe('Viewer')
  })
})

describe('top navigation visibility', () => {
  it('renders nav items conditionally by role', () => {
    // We render an inline nav block using navItemsForRole rather
    // than mounting the whole TopNavigation (which depends on
    // AuthProvider).  This keeps the test isolated.
    function StubNav({ role }: { role: 'ADMIN' | 'ANALYST' | 'VIEWER' | null }) {
      const items = navItemsForRole(role)
      return (
        <nav aria-label="Primary">
          {items.map((i) => (
            <span key={i.path} data-testid={`nav-${i.label}`}>{i.label}</span>
          ))}
        </nav>
      )
    }
    const { rerender } = render(<StubNav role="ADMIN" />)
    expect(screen.getByTestId('nav-Users')).toBeInTheDocument()
    expect(screen.getByTestId('nav-Security')).toBeInTheDocument()
    rerender(<StubNav role="VIEWER" />)
    expect(screen.queryByTestId('nav-Users')).toBeNull()
    expect(screen.queryByTestId('nav-Security')).toBeNull()
    rerender(<StubNav role="ANALYST" />)
    expect(screen.getByTestId('nav-AI Cost Analyst')).toBeInTheDocument()
    expect(screen.getByTestId('nav-Conversations')).toBeInTheDocument()
    expect(screen.queryByTestId('nav-Users')).toBeNull()
  })
})
