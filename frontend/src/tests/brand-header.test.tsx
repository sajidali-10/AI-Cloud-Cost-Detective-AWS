// Phase 6C — BrandHeader theme-aware logo selection.
//
// Verifies:
//   - dark theme -> hiplink-logo-on-dark.png
//   - light theme -> hiplink-logo-on-light.png
//   - alt="HipLink" is present on every rendered <img>
//   - logo is rendered with h-8 w-auto (aspect ratio preserved, not stretched)
//   - compact and non-compact both render the logo + product name
//   - LOGO_SRC constant picks the right asset per theme (no string mixing)
//
// The component reads from useTheme(); the ThemeProvider bootstraps
// from localStorage / prefers-color-scheme.  We use the existing
// theme helpers (`__themeTestInternals.applyTheme`) to drive the
// data-theme attribute deterministically in tests.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { BrandHeader } from '../components/BrandHeader'
import {
  ThemeProvider,
  __themeTestInternals,
} from '../lib/theme'
import { Router } from '../lib/router'

function wrap(node: React.ReactNode, theme: 'dark' | 'light') {
  // Seed localStorage so the ThemeProvider's `readInitialTheme`
  // bootstraps with the requested theme.  `applyTheme` only updates
  // the DOM attribute — the React state still resolves from
  // localStorage on first render.
  window.localStorage.setItem('accd.theme', theme)
  __themeTestInternals.applyTheme(theme)
  return render(
    <ThemeProvider>
      <Router>{node}</Router>
    </ThemeProvider>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  window.history.replaceState({}, '', '/')
})

afterEach(() => {
  window.localStorage.clear()
})

describe('BrandHeader', () => {
  it('renders the dark-theme logo asset when theme is dark', () => {
    wrap(<BrandHeader />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img).toBeInTheDocument()
    expect(img.src).toContain('/branding/hiplink-logo-on-dark.png')
    expect(img.getAttribute('data-theme-asset')).toBe('dark')
  })

  it('renders the light-theme logo asset when theme is light', () => {
    wrap(<BrandHeader />, 'light')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img).toBeInTheDocument()
    expect(img.src).toContain('/branding/hiplink-logo-on-light.png')
    expect(img.getAttribute('data-theme-asset')).toBe('light')
  })

  it('provides accessible alt text on the logo', () => {
    wrap(<BrandHeader />, 'dark')
    expect(screen.getByAltText('HipLink')).toBeInTheDocument()
  })

  it('preserves logo aspect ratio (height fixed, width auto)', () => {
    wrap(<BrandHeader />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.className).toMatch(/h-8/)
    expect(img.className).toMatch(/w-auto/)
    // No class should force an arbitrary width that would stretch the
    // asset.  `w-` followed by a number other than `auto` is the
    // pattern we forbid.
    expect(img.className).not.toMatch(/(^|\s)w-\d+\b/)
  })

  it('logo is not draggable (decorative)', () => {
    wrap(<BrandHeader />, 'dark')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.draggable).toBe(false)
  })

  it('compact prop preserves the product name text', () => {
    wrap(<BrandHeader compact />, 'dark')
    // Logo still rendered.
    expect(screen.getByAltText('HipLink')).toBeInTheDocument()
    // Product name present.
    expect(screen.getByText('AI Cloud Cost Detective')).toBeInTheDocument()
  })

  it('non-compact prop preserves the product name text', () => {
    wrap(<BrandHeader />, 'dark')
    expect(screen.getByAltText('HipLink')).toBeInTheDocument()
    expect(screen.getByText('AI Cloud Cost Detective')).toBeInTheDocument()
  })

  it('keeps a compact header dimension (h-8 = 32px) regardless of theme', () => {
    wrap(<BrandHeader compact />, 'light')
    const img = screen.getByAltText('HipLink') as HTMLImageElement
    expect(img.className).toMatch(/h-8/)
  })

  it('exposes a brand-header testid', () => {
    wrap(<BrandHeader />, 'dark')
    expect(screen.getByTestId('brand-header')).toBeInTheDocument()
    expect(screen.getByTestId('brand-header-logo')).toBeInTheDocument()
  })

  it('switches assets when theme switches mid-test', () => {
    const r1 = wrap(<BrandHeader />, 'dark')
    expect(
      (screen.getByAltText('HipLink') as HTMLImageElement).src,
    ).toContain('hiplink-logo-on-dark.png')
    r1.unmount()

    const r2 = wrap(<BrandHeader />, 'light')
    expect(
      (screen.getByAltText('HipLink') as HTMLImageElement).src,
    ).toContain('hiplink-logo-on-light.png')
    r2.unmount()
  })
})
