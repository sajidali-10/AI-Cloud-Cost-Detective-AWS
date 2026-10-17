// Phase 7D.1 — canonical AWS recommendations client tests.
//
// The provider-neutral `/api/cloud/recommendations` client now
// supports `provider: 'AWS'` alongside Azure.  These tests pin:
//   * cache-key isolation (AWS vs Azure, all vs single, region/days)
//   * the pre-7D.1 key string is preserved when new dims are omitted
//   * the request URL carries provider/account/region/days
// The unified AWS visual redesign is Phase 7D.2.

import { afterEach, describe, expect, it } from 'vitest'

import { configureApi, __resetApiForTests } from '../lib/api'
import {
  buildCloudRecommendationsCacheKey,
  fetchCloudRecommendations,
} from '../lib/finops/cloudRecommendations'

afterEach(() => {
  __resetApiForTests()
})

describe('Phase 7D.1 — canonical recommendation cache-key isolation', () => {
  it('isolates AWS from Azure for the same scope', () => {
    const aws = buildCloudRecommendationsCacheKey('all', 'aws')
    const azure = buildCloudRecommendationsCacheKey('all', 'azure')
    expect(aws).not.toEqual(azure)
    expect(aws.startsWith('aws:')).toBe(true)
    expect(azure.startsWith('azure:')).toBe(true)
  })

  it('isolates all-AWS from a single AWS account', () => {
    const all = buildCloudRecommendationsCacheKey('all', 'aws')
    const one = buildCloudRecommendationsCacheKey('acct:111', 'aws')
    expect(all).not.toEqual(one)
  })

  it('isolates region dimension', () => {
    const east = buildCloudRecommendationsCacheKey('all', 'aws', 'us-east-1', 30)
    const west = buildCloudRecommendationsCacheKey('all', 'aws', 'us-west-2', 30)
    expect(east).not.toEqual(west)
  })

  it('isolates lookback-window dimension', () => {
    const d7 = buildCloudRecommendationsCacheKey('all', 'aws', 'us-east-1', 7)
    const d90 = buildCloudRecommendationsCacheKey('all', 'aws', 'us-east-1', 90)
    expect(d7).not.toEqual(d90)
  })

  it('preserves the exact pre-7D.1 key when new dimensions are omitted', () => {
    expect(buildCloudRecommendationsCacheKey(null, 'azure')).toBe(
      'azure:cloud:recommendations:all',
    )
    expect(buildCloudRecommendationsCacheKey('sub:abc', 'azure')).toBe(
      'azure:cloud:recommendations:sub:abc',
    )
  })
})

describe('Phase 7D.1 — canonical AWS request URL', () => {
  it('sends provider=AWS with account, region and days', async () => {
    let captured = ''
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: async (input) => {
        captured = String(input)
        return new Response(
          JSON.stringify({ summary: { provider: 'AWS' }, recommendations: [] }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      },
    })
    await fetchCloudRecommendations({
      provider: 'AWS',
      accountId: 'acct:111',
      region: 'us-east-1',
      days: 30,
    })
    expect(captured).toContain('provider=AWS')
    expect(captured).toContain('account_id=acct%3A111')
    expect(captured).toContain('region=us-east-1')
    expect(captured).toContain('days=30')
  })

  it('omits optional params when not supplied', async () => {
    let captured = ''
    configureApi({
      getToken: () => null,
      onSessionExpired: () => {},
      transport: async (input) => {
        captured = String(input)
        return new Response(
          JSON.stringify({ summary: { provider: 'AWS' }, recommendations: [] }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      },
    })
    await fetchCloudRecommendations({ provider: 'AWS' })
    expect(captured).toContain('provider=AWS')
    expect(captured).not.toContain('region=')
    expect(captured).not.toContain('days=')
    expect(captured).not.toContain('account_id=')
  })
})