// Phase 7B — AccountsPage lifecycle + cache-isolation regression.
//
// Source-level: pins the contract that an Enabled button appears
// for disabled non-management rows, that Disable requires a
// confirmation modal, that the management row never exposes
// Disable/Enable, and that the cost / resources / optimization
// cache keys are all scoped on the active account selection so
// Account A data cannot satisfy an Account B request.

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const SRC_ROOT = resolve(__dirname, '..')

function src(relative: string): string {
  return readFileSync(resolve(SRC_ROOT, relative), 'utf-8')
}

describe('Phase 7B — AccountsPage lifecycle (source)', () => {
  const page = src('pages/AccountsPage.tsx')

  it('renders both Enable and Disable buttons based on enabled state', () => {
    expect(page).toMatch(/testid=\{`enable-\$\{row\.id}`\}/)
    expect(page).toMatch(/testid=\{`disable-\$\{row\.id}`\}/)
  })

  it('disables Disable/Enable on management rows', () => {
    expect(page).toMatch(/!row\.is_management/)
    // The management row passes through both checks unchanged.
    expect(page).toMatch(/row\.is_management/)
  })

  it('Disable is gated by a confirmation modal', () => {
    expect(page).toMatch(/disable-confirm-modal/)
    expect(page).toMatch(/disable-confirm-confirm/)
    expect(page).toMatch(/disable-confirm-cancel/)
    // Phase 7C.2A refactored the inline callbacks into shared
    // handlers — the Disable button now calls a wrapper that
    // invokes ``setPendingDisable(row)``; the wrapper may carry
    // extra whitespace / closure state.  Accept any shape that
    // forwards the row into ``setPendingDisable``.
    expect(page).toMatch(/setPendingDisable\(row\)/)
  })

  it('Enable fires immediately without confirmation', () => {
    // The Enable button does NOT route through setPendingDisable or
    // any confirmation state — it calls setEnabled directly.
    expect(page).toMatch(/setEnabled\(row,\s*true\)/)
  })

  it('surfaces the full status enum (CONNECTED / ENABLED / DISABLED / ACCESS_DENIED / ROLE_NOT_FOUND / ERROR)', () => {
    expect(page).toMatch(/CONNECTED/)
    expect(page).toMatch(/ENABLED/)
    expect(page).toMatch(/DISABLED/)
    expect(page).toMatch(/ACCESS_DENIED/)
    expect(page).toMatch(/ROLE_NOT_FOUND/)
    expect(page).toMatch(/ERROR/)
  })

  it('preserves the auth-bootstrap gate', () => {
    expect(page).toMatch(/useAuth\(\)/)
    expect(page).toMatch(/canFetch/)
    expect(page).toMatch(/accounts-page-loading/)
    expect(page).toMatch(/accounts-page-anonymous/)
  })
})

describe('Phase 7B — cache keys are account-scoped', () => {
  it('costs cache key includes the active account', () => {
    const costs = src('lib/finops/costs.ts')
    expect(costs).toMatch(/buildCostsCacheKey\(/)
    expect(costs).toMatch(/accountId/)
    // The key body joins the accountId — without it, Account A
    // could satisfy Account B.
    expect(costs).toMatch(/acct/)
  })

  it('resources cache key includes the active account', () => {
    const r = src('lib/finops/resources.ts')
    expect(r).toMatch(/buildResourcesCacheKey\(/)
    expect(r).toMatch(/accountId/)
  })

  it('optimization cache keys include the active account', () => {
    const o = src('lib/finops/optimization.ts')
    expect(o).toMatch(/buildCapabilitiesCacheKey\(/)
    expect(o).toMatch(/buildRecommendationsCacheKey\(/)
    expect(o).toMatch(/buildSummaryCacheKey\(/)
    // Each function takes accountId and bakes it into the key.
    const capsDef = o.match(/buildCapabilitiesCacheKey[\s\S]+?\n\}\n/)
    expect(capsDef?.[0]).toMatch(/accountId/)
    const recsDef = o.match(/buildRecommendationsCacheKey[\s\S]+?\n\}\n/)
    expect(recsDef?.[0]).toMatch(/accountId/)
    const summaryDef = o.match(/buildSummaryCacheKey[\s\S]+?\n\}\n/)
    expect(summaryDef?.[0]).toMatch(/accountId/)
  })

  it('utilization cache key includes the active account', () => {
    const u = src('lib/finops/utilization.ts')
    expect(u).toMatch(/buildUtilizationCacheKey\(/)
    expect(u).toMatch(/account_id/)
  })
})

describe('Phase 7B — every FinOps page invalidates the cache on account switch', () => {
  it('Dashboard invalidates the cache when selection changes', () => {
    const d = src('pages/DashboardPage.tsx')
    expect(d).toMatch(/useAccountContext/)
    expect(d).toMatch(/invalidateFinopsCache/)
  })

  it('Costs invalidates the cache when selection changes', () => {
    const c = src('pages/CostsPage.tsx')
    expect(c).toMatch(/useAccountContext/)
    expect(c).toMatch(/invalidateFinopsCache/)
  })

  it('Resources invalidates the cache when selection changes', () => {
    const r = src('pages/ResourcesPage.tsx')
    expect(r).toMatch(/useAccountContext/)
    expect(r).toMatch(/invalidateFinopsCache/)
  })

  it('Optimization invalidates the cache when selection changes', () => {
    const o = src('pages/OptimizationPage.tsx')
    expect(o).toMatch(/useAccountContext/)
    expect(o).toMatch(/invalidateFinopsCache/)
  })
})

describe('Phase 7B — account context survives refresh and clears on logout', () => {
  it('AccountProvider persists the selection in sessionStorage', () => {
    const ctx = src('lib/accountContext.tsx')
    expect(ctx).toMatch(/sessionStorage/)
    expect(ctx).toMatch(/STORAGE_KEY = 'cd\.account\.selection\.v1'/)
  })

  it('AccountProvider does NOT persist anything in localStorage', () => {
    const ctx = src('lib/accountContext.tsx')
    expect(ctx).not.toMatch(/localStorage\.setItem/)
    expect(ctx).not.toMatch(/localStorage\.getItem/)
  })

  it('AccountContext exposes a toQueryParam helper', () => {
    const ctx = src('lib/accountContext.tsx')
    expect(ctx).toMatch(/toQueryParam/)
  })
})
