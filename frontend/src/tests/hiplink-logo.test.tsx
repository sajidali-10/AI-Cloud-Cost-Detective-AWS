// Phase 6C.1 — Centralized HiplinkLogo component tests.
//
// Asserts the single source-of-truth for the official HipLink brand
// mark.  Page-specific tests (BrandHeader, DashboardHero) cover
// their own composition; this suite locks the centralized contract.
//
// Covered:
//   - dark theme -> /branding/hiplink-logo-on-dark.png
//   - light theme -> /branding/hiplink-logo-on-light.png
//   - alt="HipLink" on every render
//   - compact size -> h-8 w-auto (preserves aspect ratio)
//   - hero size -> h-16 w-auto (preserves aspect ratio)
//   - never stretched (no fixed w-N pixel class)
//   - data-testid + data-theme-asset for stable test selection
//   - ariaLabel override works

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { HiplinkLogo } from '../components/HiplinkLogo'
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

describe('HiplinkLogo (centralized)', () => {
  it('renders dark-theme asset under dark theme', () => {
    wrap(<HiplinkLogo />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.src).toContain('/branding/hiplink-logo-on-dark.png')
    expect(img.getAttribute('data-theme-asset')).toBe('dark')
  })

  it('renders light-theme asset under light theme', () => {
    wrap(<HiplinkLogo />, 'light')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.src).toContain('/branding/hiplink-logo-on-light.png')
    expect(img.getAttribute('data-theme-asset')).toBe('light')
  })

  it('exposes alt="HipLink" by default', () => {
    wrap(<HiplinkLogo />, 'dark')
    expect(screen.getByAltText('HipLink')).toBeInTheDocument()
  })

  it('honours ariaLabel override', () => {
    wrap(<HiplinkLogo ariaLabel="HipLink brand" />, 'dark')
    expect(screen.getByAltText('HipLink brand')).toBeInTheDocument()
  })

  it('compact size uses h-8 w-auto (preserves aspect ratio)', () => {
    wrap(<HiplinkLogo size="compact" />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.className).toMatch(/h-8/)
    expect(img.className).toMatch(/w-auto/)
    expect(img.className).not.toMatch(/(^|\s)w-\d+\b/)
    expect(img.getAttribute('data-size')).toBe('compact')
  })

  it('hero size uses h-16 w-auto (preserves aspect ratio)', () => {
    wrap(<HiplinkLogo size="hero" />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.className).toMatch(/h-16/)
    expect(img.className).toMatch(/w-auto/)
    expect(img.className).not.toMatch(/(^|\s)w-\d+\b/)
    expect(img.getAttribute('data-size')).toBe('hero')
  })

  it('exposes testid + theme-asset attribute', () => {
    wrap(<HiplinkLogo testId="custom" />, 'light')
    const img = screen.getByTestId('custom')
    expect(img.getAttribute('data-theme-asset')).toBe('light')
  })

  it('is not draggable (decorative)', () => {
    wrap(<HiplinkLogo />, 'dark')
    expect((screen.getByAltText('HipLink') as HTMLImageElement).draggable).toBe(false)
  })
})
