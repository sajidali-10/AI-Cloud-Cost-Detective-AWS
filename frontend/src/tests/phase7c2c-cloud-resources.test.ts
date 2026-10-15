// Phase 7C.2C — Cloud resource cache-key isolation + shape tests.

import { describe, expect, it } from 'vitest'

describe('Phase 7C.2C — cache key isolation', () => {
  it('builds distinct keys per provider', async () => {
    const { buildCloudResourcesCacheKey } = await import('../lib/finops/cloudResources')
    const awsKey = buildCloudResourcesCacheKey('acct:111', 'aws')
    const azureKey = buildCloudResourcesCacheKey('sub:abc', 'azure')
    expect(awsKey).not.toEqual(azureKey)
    expect(awsKey.startsWith('aws:')).toBe(true)
    expect(azureKey.startsWith('azure:')).toBe(true)
  })

  it('builds distinct keys for all vs single scope', async () => {
    const { buildCloudResourcesCacheKey } = await import('../lib/finops/cloudResources')
    const all = buildCloudResourcesCacheKey('all', 'azure')
    const single = buildCloudResourcesCacheKey('sub:abc', 'azure')
    expect(all).not.toEqual(single)
  })

  it('handles null account as all', async () => {
    const { buildCloudResourcesCacheKey } = await import('../lib/finops/cloudResources')
    expect(buildCloudResourcesCacheKey(null, 'azure')).toBe('azure:cloud:resources:all')
  })
})

describe('Phase 7C.2C — fetchCloudResources URL building', () => {
  it('passes provider=AZURE in the URL', () => {
    const params = new URLSearchParams()
    params.set('provider', 'AZURE')
    const url = `/api/cloud/resources?${params.toString()}`
    expect(url).toContain('provider=AZURE')
  })

  it('passes account_id when supplied', () => {
    const params = new URLSearchParams()
    params.set('provider', 'AZURE')
    params.set('account_id', 'sub:abc')
    const url = `/api/cloud/resources?${params.toString()}`
    expect(url).toContain('account_id=sub%3Aabc')
  })
})

describe('Phase 7C.2C — no-data handling', () => {
  it('NO_DATA status renders zero-resource state without error', async () => {
    const noData = {
      summary: {
        provider: 'AZURE',
        scope: 'sub:abc',
        scope_kind: 'single',
        status: 'NO_DATA',
        total_resources: 0,
        by_type: [],
        by_location: [],
        by_resource_group: [],
        by_subscription: [
          { subscription_id: 'sub-abc', display_name: 'Sponsorship', count: 0 },
        ],
        subscription_errors: [],
        source: 'AZURE_RESOURCE_GRAPH',
      },
      resources: [],
    }
    expect(noData.summary.total_resources).toBe(0)
    expect(noData.summary.status).toBe('NO_DATA')
    expect(noData.resources).toHaveLength(0)
  })
})
