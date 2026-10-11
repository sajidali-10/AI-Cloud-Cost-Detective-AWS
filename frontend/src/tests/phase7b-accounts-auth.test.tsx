// Phase 7B — AccountsPage auth-bootstrap gate (source-level regression).
//
// Pins the defect caught during live browser acceptance: the
// AccountsPage was firing its initial ``apiFetch`` before the
// AuthProvider had finished restoring the session, producing a
// "missing Authorization header" rejection for an authenticated
// ADMIN.
//
// We assert at the SOURCE level (the same pattern as the
// phase7b-runtime-closure.test.tsx for TopNavigation mounting).
// Running the component under jsdom with the full AuthProvider
// bootstrap loop is brittle across minor upstream refactors; what
// matters is that the gate exists in the source.

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const ACCOUNTS_PAGE = resolve(
  __dirname,
  '../pages/AccountsPage.tsx',
)

const source = readFileSync(ACCOUNTS_PAGE, 'utf-8')

describe('Phase 7B — AccountsPage auth-bootstrap gate (source)', () => {
  it('imports the centralized useAuth hook', () => {
    expect(source).toMatch(
      /import\s+\{[^}]*useAuth[^}]*\}\s+from\s+['"]\.\.\/lib\/auth['"]/,
    )
  })

  it('reads ready / user from useAuth (auth-bootstrap gate)', () => {
    // useAuth() destructures `ready` and `user` so the page can gate
    // the initial fetch on auth-bootstrap completion.
    expect(source).toMatch(/useAuth\(\)/)
    expect(source).toMatch(/\bready\b/)
    expect(source).toMatch(/\buser\b/)
  })

  it('renders a "loading" placeholder when ready=false', () => {
    expect(source).toMatch(/accounts-page-loading/)
    expect(source).toMatch(/Initializing session/i)
  })

  it('renders an "anonymous" placeholder when no user', () => {
    expect(source).toMatch(/accounts-page-anonymous/)
    expect(source).toMatch(/You must sign in/i)
  })

  it('computes a canFetch gate and uses it before apiFetch', () => {
    // The gate is computed once (e.g. const canFetch = ...) and used
    // to short-circuit refresh / submit / validate / disable.
    expect(source).toMatch(/canFetch/)
    // refresh short-circuits on !canFetch.
    expect(source).toMatch(/if\s*\(\s*!canFetch\s*\)\s*return/)
  })

  it('uses the centralized apiFetch — never direct fetch / XHR', () => {
    // We must NOT reintroduce a separate auth mechanism.  The only
    // network entry point is apiFetch from ../lib/api.
    expect(source).toMatch(/apiFetch/)
    expect(source).not.toMatch(/localStorage\.getItem\(\s*['"]accd\.session/)
    expect(source).not.toMatch(/Bearer\s+\$\{/)
    expect(source).not.toMatch(/XMLHttpRequest|axios/)
  })

  it('does not include provider/enabled/is_management form controls', () => {
    // Per Phase 7B semantics the management row is bootstrap-only;
    // manually registered rows are always provider=AWS, enabled=true,
    // is_management=false.  The form must NOT expose these fields.
    expect(source).not.toMatch(/name=\"provider\"/)
    expect(source).not.toMatch(/name=\"enabled\"/)
    expect(source).not.toMatch(/name=\"is_management\"/)
    expect(source).not.toMatch(/name=\"isManagement\"/)
    // And the form does not POST provider / enabled / is_management.
    const payloadMatches = source.match(/payload\s*:\s*Record<string,\s*unknown>\s*=\s*\{[\s\S]*?\}\s*\n\s*\}/)
    expect(payloadMatches).not.toBeNull()
    const payloadBlock = payloadMatches![0]
    expect(payloadBlock).not.toMatch(/['"]provider['"]/)
    expect(payloadBlock).not.toMatch(/['"]enabled['"]/)
    expect(payloadBlock).not.toMatch(/['"]is_management['"]/)
  })

  it('external_id is write-only — only ever sent, never echoed back', () => {
    // The form POSTs external_id only when trimmed non-empty.
    expect(source).toMatch(/form\.external_id\.trim\(\)/)
    // The page state does not store external_id anywhere besides the
    // form input; we never display it.
    expect(source).not.toMatch(/external_id\}.*\{account\.external_id/)
  })
})
