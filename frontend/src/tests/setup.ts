// Vitest setup — runs once per test file.
//
// Provides:
//  - jest-dom matchers (toBeInTheDocument, etc.)
//  - matchMedia stub (used by the theme provider)
//  - IntersectionObserver stub
//  - localStorage reset between tests
//  - history reset
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

// ---------------------------------------------------------------------------
// matchMedia — jsdom does not implement it.  Defined on window
// directly so it survives `vi.restoreAllMocks()` (which restores
// spies on objects but does NOT undo property assignments).
// ---------------------------------------------------------------------------
;(window as unknown as { matchMedia: unknown }).matchMedia = (
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

// ---------------------------------------------------------------------------
// IntersectionObserver — not used by Phase 6A but stubbed so any
// future component that imports a third-party chart lib does not
// crash in jsdom.
// ---------------------------------------------------------------------------
class _MockIntersectionObserver {
  observe = () => undefined
  unobserve = () => undefined
  disconnect = () => undefined
  takeRecords = () => [] as IntersectionObserverEntry[]
  root = null
  rootMargin = ''
  thresholds: ReadonlyArray<number> = []
}
;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver =
  _MockIntersectionObserver

// ---------------------------------------------------------------------------
// Storage / navigation reset between tests.
// ---------------------------------------------------------------------------
beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.history.replaceState({}, '', '/')
})

afterEach(() => {
  cleanup()
})
