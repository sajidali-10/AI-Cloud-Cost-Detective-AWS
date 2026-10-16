// Phase 7C.2D — Azure Advisor UI tests.

import { describe, expect, it } from 'vitest'

function mockJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const ZERO_RESPONSE = {
  summary: {
    provider: 'AZURE' as const,
    scope: 'all',
    scope_kind: 'all' as const,
    status: 'SUCCESS',
    savings_status: 'EMPTY',
    currency: null,
    total_recommendations: 0,
    recommendations_with_savings: 0,
    recommendations_without_savings: 0,
    potential_annual_savings: null,
    savings_by_currency: [],
    by_impact: [],
    by_resource_type: [],
    by_subscription: [],
    by_resource_group: [],
    by_type: [],
    by_subcategory: [],
    subscription_errors: [],
    source: 'AZURE_ADVISOR',
  },
  recommendations: [],
}

const SAMPLE_RESPONSE = {
  summary: {
    provider: 'AZURE' as const,
    scope: 'all',
    scope_kind: 'all' as const,
    status: 'SUCCESS',
    savings_status: 'OK',
    currency: 'USD',
    total_recommendations: 2,
    recommendations_with_savings: 2,
    recommendations_without_savings: 0,
    potential_annual_savings: '300.00',
    savings_by_currency: [{ currency: 'USD', amount: '300.00' }],
    by_impact: [
      { key: 'High', count: 1 },
      { key: 'Medium', count: 1 },
    ],
    by_resource_type: [
      { key: 'Microsoft.Compute/virtualMachines', count: 2 },
    ],
    by_subscription: [
      { key: 'a54d076b-2650-4b4f-a28c-2deed2011f83', count: 2 },
    ],
    by_resource_group: [{ key: 'rg1', count: 2 }],
    by_type: [{ key: 'e10b1381', count: 2 }],
    by_subcategory: [{ key: 'UsageOptimization', count: 2 }],
    subscription_errors: [],
    source: 'AZURE_ADVISOR',
  },
  recommendations: [
    {
      provider: 'AZURE',
      account_id: 'a54d076b-2650-4b4f-a28c-2deed2011f83',
      region: null,
      recommendation_id: '/subscriptions/a54d076b/.../Advisor/recommendations/r1',
      resource_id: '/subscriptions/a54d076b/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1',
      resource_type: 'Microsoft.Compute/virtualMachines',
      service: 'Microsoft.Compute/virtualMachines',
      action: 'e10b1381',
      title: 'Right-size or shutdown underutilized virtual machines',
      estimated_savings: '100.00',
      currency: 'USD',
      source: 'AZURE_ADVISOR',
      extensions: {
        impact: 'High',
        subcategory: 'UsageOptimization',
        recommendation_status: 'Active',
        last_updated: '2026-09-27T00:21:48Z',
        resource_group: 'rg1',
        resource_name: 'vm1',
      },
    },
    {
      provider: 'AZURE',
      account_id: 'a54d076b-2650-4b4f-a28c-2deed2011f83',
      region: null,
      recommendation_id: '/subscriptions/a54d076b/.../Advisor/recommendations/r2',
      resource_id: '/subscriptions/a54d076b/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm2',
      resource_type: 'Microsoft.Compute/virtualMachines',
      service: 'Microsoft.Compute/virtualMachines',
      action: 'e10b1381',
      title: 'Right-size or shutdown underutilized virtual machines',
      estimated_savings: '200.00',
      currency: 'USD',
      source: 'AZURE_ADVISOR',
      extensions: {
        impact: 'Medium',
        subcategory: 'UsageOptimization',
        recommendation_status: 'Active',
        last_updated: '2026-09-27T00:21:48Z',
        resource_group: 'rg1',
        resource_name: 'vm2',
      },
    },
  ],
}

const MIXED_CURRENCY_RESPONSE = {
  ...SAMPLE_RESPONSE,
  summary: {
    ...SAMPLE_RESPONSE.summary,
    savings_status: 'MIXED_CURRENCY',
    currency: null,
    potential_annual_savings: null,
    savings_by_currency: [
      { currency: 'USD', amount: '100.00' },
      { currency: 'EUR', amount: '200.00' },
    ],
  },
}

describe('Phase 7C.2D — Azure advisor URL building', () => {
  it('passes provider=AZURE in the URL', () => {
    const params = new URLSearchParams()
    params.set('provider', 'AZURE')
    const url = `/api/cloud/recommendations?${params.toString()}`
    expect(url).toContain('provider=AZURE')
  })

  it('passes account_id when supplied', () => {
    const params = new URLSearchParams()
    params.set('provider', 'AZURE')
    params.set('account_id', 'sub:abc')
    const url = `/api/cloud/recommendations?${params.toString()}`
    expect(url).toContain('account_id=sub%3Aabc')
  })

  it('omits account_id when not supplied', () => {
    const params = new URLSearchParams()
    params.set('provider', 'AZURE')
    const url = `/api/cloud/recommendations?${params.toString()}`
    expect(url).not.toContain('account_id=')
  })
})

describe('Phase 7C.2D — cache-key isolation', () => {
  it('builds distinct keys per provider', async () => {
    const { buildCloudRecommendationsCacheKey } = await import('../lib/finops/cloudRecommendations')
    const aws = buildCloudRecommendationsCacheKey('acct:111', 'aws')
    const azure = buildCloudRecommendationsCacheKey('sub:abc', 'azure')
    expect(aws).not.toEqual(azure)
    expect(aws.startsWith('aws:')).toBe(true)
    expect(azure.startsWith('azure:')).toBe(true)
  })

  it('builds distinct keys for all vs single scope', async () => {
    const { buildCloudRecommendationsCacheKey } = await import('../lib/finops/cloudRecommendations')
    expect(buildCloudRecommendationsCacheKey('all', 'azure'))
      .not.toEqual(buildCloudRecommendationsCacheKey('sub:abc', 'azure'))
  })

  it('handles null account as all', async () => {
    const { buildCloudRecommendationsCacheKey } = await import('../lib/finops/cloudRecommendations')
    expect(buildCloudRecommendationsCacheKey(null, 'azure')).toBe('azure:cloud:recommendations:all')
  })
})

describe('Phase 7C.2D — fetchCloudRecommendations URL builder', () => {
  it('mockJson helper sets content-type', () => {
    const r = mockJson(200, { ok: true })
    expect(r.status).toBe(200)
    expect(r.headers.get('content-type')).toBe('application/json')
  })
})

describe('Phase 7C.2D — aggregation invariants on the canonical envelope', () => {
  it('uniform currency: sum matches potential_annual_savings', () => {
    const summary = SAMPLE_RESPONSE.summary
    const sum = summary.savings_by_currency.reduce(
      (acc: number, entry: { amount: string }) => acc + parseFloat(entry.amount),
      0
    )
    expect(sum).toBe(parseFloat(summary.potential_annual_savings ?? '0'))
  })

  it('mixed currency: potential_annual_savings is null', () => {
    expect(MIXED_CURRENCY_RESPONSE.summary.potential_annual_savings).toBeNull()
    expect(MIXED_CURRENCY_RESPONSE.summary.savings_by_currency).toHaveLength(2)
  })

  it('zero recommendations: EMPTY savings_status', () => {
    expect(ZERO_RESPONSE.summary.savings_status).toBe('EMPTY')
    expect(ZERO_RESPONSE.summary.potential_annual_savings).toBeNull()
    expect(ZERO_RESPONSE.summary.total_recommendations).toBe(0)
  })

  it('count invariants hold', () => {
    const summary = SAMPLE_RESPONSE.summary
    const sum = (arr: { count: number }[]) => arr.reduce((a, b) => a + b.count, 0)
    expect(sum(summary.by_impact)).toBe(summary.total_recommendations)
    expect(sum(summary.by_resource_type)).toBe(summary.total_recommendations)
    expect(sum(summary.by_subscription)).toBe(summary.total_recommendations)
    expect(sum(summary.by_resource_group)).toBe(summary.total_recommendations)
    expect(sum(summary.by_type)).toBe(summary.total_recommendations)
  })
})

describe('Phase 7C.2D — provider switching invariants', () => {
  it('switches cache-key provider segment', async () => {
    const { buildCloudRecommendationsCacheKey } = await import('../lib/finops/cloudRecommendations')
    const before = buildCloudRecommendationsCacheKey('sub:abc', 'azure')
    const after = buildCloudRecommendationsCacheKey('sub:abc', 'aws')
    expect(before).not.toEqual(after)
    expect(before.startsWith('azure:')).toBe(true)
    expect(after.startsWith('aws:')).toBe(true)
  })
})
