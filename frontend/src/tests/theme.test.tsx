// Phase 6A — Theme tests.
//
// Covers the spec mandate:
//   - dark mode renders
//   - light mode renders
//   - toggle works
//   - selection persists
//   - system theme initial behavior
//   - flash/hydration handling (data-theme set before React mounts)

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThemeProvider, useTheme, __themeTestInternals } from '../lib/theme'
import { ThemeToggle } from '../components/ThemeToggle'

// Local override that lets individual tests set a deterministic
// system preference without leaving vi.fn()-based spies lying
// around (those get restored by `vi.restoreAllMocks()` and break
// subsequent tests).
function setSystemPrefersLight(value: boolean) {
  ;(window as unknown as { matchMedia: (q: string) => { matches: boolean } }).matchMedia = (
    query: string,
  ) => ({
    matches: query.includes('light') ? value : !value,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })
}

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
  document.documentElement.classList.remove('theme-dark', 'theme-light')
  // Reset matchMedia to the deterministic default defined in setup.ts
  // (matches=false for every query).  Individual tests can override.
  ;(window as unknown as { matchMedia: (q: string) => { matches: boolean } }).matchMedia = (
    query: string,
  ) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })
})
afterEach(() => {
  vi.restoreAllMocks()
})

describe('ThemeProvider', () => {
  it('defaults to dark when no preference is set and system prefers dark', () => {
    setSystemPrefersLight(false)
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme').textContent).toBe('dark')
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('defaults to light when the system prefers light', () => {
    setSystemPrefersLight(true)
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme').textContent).toBe('light')
  })

  it('honours a previously persisted choice', () => {
    window.localStorage.setItem('accd.theme', 'light')
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme').textContent).toBe('light')
  })

  it('persists the user choice to localStorage when set explicitly', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    act(() => {
      screen.getByTestId('set-light').click()
    })
    expect(window.localStorage.getItem('accd.theme')).toBe('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })

  it('toggle flips between dark and light', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    expect(screen.getByTestId('theme').textContent).toBe('dark')
    act(() => {
      screen.getByTestId('toggle').click()
    })
    expect(screen.getByTestId('theme').textContent).toBe('light')
    expect(window.localStorage.getItem('accd.theme')).toBe('light')
    act(() => {
      screen.getByTestId('toggle').click()
    })
    expect(screen.getByTestId('theme').textContent).toBe('dark')
    expect(window.localStorage.getItem('accd.theme')).toBe('dark')
  })

  it('applies the theme via the data-theme attribute (no flash)', () => {
    render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    )
    act(() => {
      screen.getByTestId('set-light').click()
    })
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(document.documentElement.classList.contains('theme-light')).toBe(true)
    expect(document.documentElement.classList.contains('theme-dark')).toBe(false)
  })

  it('readInitialTheme returns a valid theme', () => {
    const initial = __themeTestInternals.readInitialTheme()
    expect(['dark', 'light']).toContain(initial.theme)
  })

  it('ThemeToggle has accessible name and toggles via click', async () => {
    window.localStorage.setItem('accd.theme', 'dark')
    const user = userEvent.setup()
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    )
    const btn = screen.getByTestId('theme-toggle')
    expect(btn).toHaveAttribute('aria-label')
    expect(btn.getAttribute('aria-label')).toMatch(/light|dark/i)
    await user.click(btn)
    expect(window.localStorage.getItem('accd.theme')).toBe('light')
  })
})

function Probe() {
  const { theme, setTheme, toggle, isSystemDefault } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="system-default">{String(isSystemDefault)}</span>
      <button data-testid="set-light" onClick={() => setTheme('light')}>
        set light
      </button>
      <button data-testid="set-dark" onClick={() => setTheme('dark')}>
        set dark
      </button>
      <button data-testid="toggle" onClick={toggle}>
        toggle
      </button>
    </div>
  )
}
