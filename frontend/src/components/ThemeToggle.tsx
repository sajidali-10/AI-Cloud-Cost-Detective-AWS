// Phase 6A — Sun/moon theme toggle.
//
// HipLink reference: a single icon button in the top-right of the
// header that flips between dark and light.  The button shows the
// icon for the NEXT state (i.e. when in dark mode, show the sun
// icon meaning "switch to light").
//
// Accessibility:
//  - aria-label is dynamic ("Switch to light/dark mode").
//  - aria-pressed reflects current state.
//  - keyboard activation via Space / Enter (native button).
//  - icon is aria-hidden so the label is the source of truth.

import { useTheme } from '../lib/theme'

export function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const nextLabel = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={`Switch to ${nextLabel} mode`}
      aria-pressed={theme === 'light'}
      title={`Switch to ${nextLabel} mode`}
      data-testid="theme-toggle"
      className="
        inline-flex h-9 w-9 items-center justify-center rounded-md
        border border-border bg-surface text-fg-secondary
        hover:bg-surface-hover hover:text-fg-primary
        focus:outline-none focus-visible:shadow-focus
        transition-colors
      "
    >
      {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
    </button>
  )
}

function SunIcon() {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2" />
      <path d="M12 20v2" />
      <path d="m4.93 4.93 1.41 1.41" />
      <path d="m17.66 17.66 1.41 1.41" />
      <path d="M2 12h2" />
      <path d="M20 12h2" />
      <path d="m4.93 19.07 1.41-1.41" />
      <path d="m17.66 6.34 1.41-1.41" />
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  )
}
