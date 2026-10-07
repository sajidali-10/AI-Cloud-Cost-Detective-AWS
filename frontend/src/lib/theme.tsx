// Phase 6A — Theme provider.
//
// Three sources of truth, in priority order:
//   1. localStorage['accd.theme'] — explicit user choice (persisted).
//   2. matchMedia('(prefers-color-scheme: light)') — system default on first visit.
//   3. 'dark' — final fallback.
//
// The actual <html data-theme="..."> attribute is set by the inline
// script in `index.html` BEFORE React mounts, so the user never sees
// a wrong-palette flash.  This provider keeps the attribute in sync
// with React state and exposes `theme`, `setTheme`, `toggle`.
//
// The provider intentionally does NOT read from CSS classes — only
// from the data-theme attribute — so it stays consistent with the
// inline bootstrap.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'accd.theme'

interface ThemeContextValue {
  theme: Theme
  setTheme: (next: Theme) => void
  toggle: () => void
  /** True when the choice came from prefers-color-scheme (not yet overridden). */
  isSystemDefault: boolean
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readInitialTheme(): { theme: Theme; isSystemDefault: boolean } {
  if (typeof window === 'undefined') {
    return { theme: 'dark', isSystemDefault: true }
  }
  // 1. Stored preference wins.
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light') {
      return { theme: stored, isSystemDefault: false }
    }
  } catch {
    /* localStorage may be blocked; fall through to system check */
  }
  // 2. System default.
  if (
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: light)').matches
  ) {
    return { theme: 'light', isSystemDefault: true }
  }
  return { theme: 'dark', isSystemDefault: true }
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-theme', theme)
  document.documentElement.classList.remove('theme-dark', 'theme-light')
  document.documentElement.classList.add('theme-' + theme)
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Read once at mount.  The inline script has already set the
  // data-theme attribute; we read whatever it picked.
  const [{ theme, isSystemDefault }, setState] = useState<{ theme: Theme; isSystemDefault: boolean }>(
    () => readInitialTheme(),
  )

  // Keep the DOM attribute authoritative on mount (covers the case
  // where the React tree is rendered into an iframe or test harness
  // that bypassed the inline bootstrap).
  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    applyTheme(next)
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* persistence is best-effort */
    }
    setState({ theme: next, isSystemDefault: false })
  }, [])

  const toggle = useCallback(() => {
    setState((prev) => {
      const next: Theme = prev.theme === 'dark' ? 'light' : 'dark'
      applyTheme(next)
      try {
        window.localStorage.setItem(STORAGE_KEY, next)
      } catch {
        /* persistence is best-effort */
      }
      return { theme: next, isSystemDefault: false }
    })
  }, [])

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, setTheme, toggle, isSystemDefault }),
    [theme, setTheme, toggle, isSystemDefault],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return ctx
}

// Internal export for tests — do NOT use from app code.
export const __themeTestInternals = {
  STORAGE_KEY,
  applyTheme,
  readInitialTheme,
}
