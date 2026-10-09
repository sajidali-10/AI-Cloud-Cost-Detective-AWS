// Phase 6C.1 — Dashboard hero card tests.
//
// Locks the corporate HipLink AI Assistant visual language:
//   - large theme-aware Hiplink logo
//   - AI Cloud Cost Detective title
//   - AWS cost visibility + optimization + AI-powered FinOps tagline
//   - compact environment/status line driven by real data
//
// Theme-aware logo selection is exercised here too — the hero must
// use the same centralized <HiplinkLogo size="hero" /> (no
// page-specific duplicate logo logic).

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DashboardHero } from '../components/DashboardHero'
import { ThemeProvider } from '../lib/theme'

function wrap(node: React.ReactNode, theme: 'dark' | 'light') {
  window.localStorage.setItem('accd.theme', theme)
  return render(<ThemeProvider>{node}</ThemeProvider>)
}

beforeEach(() => {
  window.localStorage.clear()
})
afterEach(() => {
  window.localStorage.clear()
})

describe('DashboardHero', () => {
  it('renders the large theme-aware Hiplink logo', () => {
    wrap(<DashboardHero />, 'dark')
    const img = screen.getByTestId('dashboard-hero-logo') as HTMLImageElement
    expect(img.src).toContain('hiplink-logo-on-dark.png')
    expect(img.className).toMatch(/h-16/)
    expect(img.className).toMatch(/w-auto/)
  })

  it('switches logo asset when theme switches', () => {
    wrap(<DashboardHero />, 'light')
    const img = screen.getByTestId('dashboard-hero-logo') as HTMLImageElement
    expect(img.src).toContain('hiplink-logo-on-light.png')
  })

  it('renders the AI Cloud Cost Detective title', () => {
    wrap(<DashboardHero />, 'dark')
    expect(screen.getByTestId('dashboard-hero-title').textContent).toBe('AI Cloud Cost Detective')
  })

  it('renders the corporate tagline', () => {
    wrap(<DashboardHero />, 'dark')
    expect(screen.getByTestId('dashboard-hero-tagline').textContent).toMatch(
      /AWS cost visibility, optimization and AI-powered FinOps analysis/,
    )
  })

  it('exposes alt="HipLink" on the hero logo', () => {
    wrap(<DashboardHero />, 'dark')
    expect(screen.getByAltText('HipLink')).toBeInTheDocument()
  })

  it('renders compact environment/status line when data is supplied', () => {
    wrap(
      <DashboardHero
        accountLabel="1234…5678"
        regionLabel="us-east-1"
        refreshedLabel="2 min ago"
        dataStatus="live"
      />,
      'dark',
    )
    const meta = screen.getByTestId('dashboard-hero-meta')
    expect(meta.textContent).toMatch(/1234…5678/)
    expect(meta.textContent).toMatch(/us-east-1/)
    expect(meta.textContent).toMatch(/2 min ago/)
    expect(screen.getByTestId('dashboard-hero-status').textContent).toMatch(/Live/)
  })

  it('omits missing environment rows rather than fabricating them', () => {
    wrap(<DashboardHero dataStatus="unavailable" />, 'dark')
    const meta = screen.getByTestId('dashboard-hero-meta')
    expect(meta.textContent).not.toMatch(/Account/)
    expect(meta.textContent).not.toMatch(/Region/)
    expect(meta.textContent).not.toMatch(/Refreshed/)
    expect(screen.getByTestId('dashboard-hero-status').textContent).toMatch(/Unavailable/)
  })

  it('maps each data status to a StatusBadge tone', () => {
    const { unmount } = wrap(<DashboardHero dataStatus="loading" />, 'dark')
    expect(screen.getByTestId('dashboard-hero-status').textContent).toMatch(/Loading/)
    unmount()
    wrap(<DashboardHero dataStatus="idle" />, 'dark')
    expect(screen.getByTestId('dashboard-hero-status').textContent).toMatch(/Idle/)
  })
})
