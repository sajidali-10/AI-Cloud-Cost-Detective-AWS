// Phase 7C.1 — page-level provider-scoping regression.
//
// Pins the contract that every page that uses FinOps cache keys
// reads the active provider from ProviderContext and includes it
// in the cache key + invalidates the cache when the provider
// changes.

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const SRC_ROOT = resolve(__dirname, '..')

function src(relative: string): string {
  return readFileSync(resolve(SRC_ROOT, relative), 'utf-8')
}

describe('Phase 7C.1 — pages wire provider into cache keys', () => {
  it('Dashboard consumes provider from ProviderContext', () => {
    const p = src('pages/DashboardPage.tsx')
    expect(p).toMatch(/useProviderContext/)
    expect(p).toMatch(/cacheProvider/)
    // Phase 7D.3 — Dashboard composes cache keys inline from
    // ``dashboardScopeCacheSegment(selection, provider)``, which
    // itself prefixes every segment with the lower-case provider
    // slug.  That helper is the canonical provider-folding path
    // for the unified dashboard; the legacy builder imports are
    // kept only for type re-exports and the page-level effects.
    expect(p).toMatch(/dashboardScopeCacheSegment\(/)
    expect(p).toMatch(/cacheProvider/)
  })

  it('Costs consumes provider from ProviderContext', () => {
    const p = src('pages/CostsPage.tsx')
    expect(p).toMatch(/useProviderContext/)
    expect(p).toMatch(/cacheProvider/)
    // Phase 7D.3 — Costs page uses the canonical cloud cost
    // builder for the unified view; the legacy buildCostsCacheKey
    // path is reserved for backward-compatible routes only.
    expect(p).toMatch(/buildCloudCostsCacheKey\([^)]*cacheProvider/)
  })

  it('Resources consumes provider from ProviderContext', () => {
    const p = src('pages/ResourcesPage.tsx')
    expect(p).toMatch(/useProviderContext/)
    expect(p).toMatch(/cacheProvider/)
    // Phase 7D.3 — Resources page routes the canonical cloud
    // resource cache key through the legacy buildResourcesCacheKey
    // helper (which itself folds cacheProvider into the key).
    // The unified /api/cloud/resources builder is wired by the
    // Dashboard; the Resources page keeps the per-kind slice.
    expect(p).toMatch(/buildResourcesCacheKey\([^)]*cacheProvider/)
    expect(p).toMatch(/buildUtilizationCacheKey\(\s*\{[\s\S]*\}\s*,\s*cacheProvider/)
  })

  it('Optimization consumes provider from ProviderContext', () => {
    const p = src('pages/OptimizationPage.tsx')
    expect(p).toMatch(/useProviderContext/)
    expect(p).toMatch(/cacheProvider/)
    // Phase 7D.2 — unified AWS + Azure Optimization reads from
    // the single canonical `/api/cloud/recommendations` cache
    // key, which itself folds the provider segment in.
    expect(p).toMatch(/buildCloudRecommendationsCacheKey\([^)]*cacheProvider/)
    // The legacy AWS capability / recs / summary cache key
    // builders MUST NOT be the primary source for the unified
    // page (those endpoints remain backend compatibility only).
    expect(p).not.toMatch(/buildCapabilitiesCacheKey\([^)]*cacheProvider/)
    expect(p).not.toMatch(/buildRecommendationsCacheKey\([^)]*cacheProvider/)
    expect(p).not.toMatch(/buildSummaryCacheKey\([^)]*cacheProvider/)
  })
})

describe('Phase 7C.1 — pages invalidate cache on provider change', () => {
  const pages = [
    'DashboardPage.tsx',
    'CostsPage.tsx',
    'ResourcesPage.tsx',
    'OptimizationPage.tsx',
  ]
  for (const page of pages) {
    it(`${page} invalidates FinOps cache when the active provider changes`, () => {
      const p = src(`pages/${page}`)
      // Every page has a ref-based effect that calls
      // invalidateFinopsCache() when either the selection or the
      // provider changes — i.e. it tracks both refs.
      expect(p).toMatch(/useProviderContext/)
      expect(p).toMatch(/invalidateFinopsCache/)
      expect(p).toMatch(/prevProviderRef/)
    })
  }
})
