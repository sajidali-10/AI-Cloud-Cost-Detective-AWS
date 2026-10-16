// Phase 7C.1 — FinOps cache key isolation tests.
//
// Pins that every FinOps cache key is scoped on the active
// provider so a stale entry from one provider can never satisfy a
// request for another provider.

import { describe, expect, it } from 'vitest'
import { buildCostsCacheKey } from '../lib/finops/costs'
import {
  buildCapabilitiesCacheKey,
  buildRecommendationsCacheKey,
  buildSummaryCacheKey,
} from '../lib/finops/optimization'
import { buildResourcesCacheKey } from '../lib/finops/resources'
import { buildUtilizationCacheKey } from '../lib/finops/utilization'

const monthPeriod = { kind: 'month' as const, month: '2026-10' }
const daysPeriod = { kind: 'days' as const, days: 30 }

describe('Phase 7C.1 — FinOps cache keys are provider-scoped', () => {
  it('costs cache key includes the provider segment', () => {
    const aws_provider_key = buildCostsCacheKey(monthPeriod, 'us-east-1', 'all', 'aws')
    const azureKey = buildCostsCacheKey(monthPeriod, 'us-east-1', 'all', 'azure')
    expect(aws_provider_key.startsWith('aws:')).toBe(true)
    expect(azureKey.startsWith('azure:')).toBe(true)
    expect(aws_provider_key).not.toBe(azureKey)
  })

  it('costs cache key keeps month + region + account distinct', () => {
    const a = buildCostsCacheKey(monthPeriod, 'us-east-1', 'acct:42', 'aws')
    const b = buildCostsCacheKey(monthPeriod, 'us-west-2', 'acct:42', 'aws')
    const c = buildCostsCacheKey(daysPeriod, 'us-east-1', 'acct:42', 'aws')
    expect(new Set([a, b, c]).size).toBe(3)
  })

  it('costs cache key defaults to aws provider', () => {
    const key = buildCostsCacheKey(monthPeriod, 'us-east-1', 'all')
    expect(key.startsWith('aws:')).toBe(true)
  })

  it('resources cache key includes the provider segment', () => {
    const aws_provider_key = buildResourcesCacheKey('us-east-1', 'all', 'aws')
    const azureKey = buildResourcesCacheKey('us-east-1', 'all', 'azure')
    expect(aws_provider_key.startsWith('aws:')).toBe(true)
    expect(azureKey.startsWith('azure:')).toBe(true)
    expect(aws_provider_key).not.toBe(azureKey)
  })

  it('optimization cache keys include the provider segment', () => {
    const capsA = buildCapabilitiesCacheKey('us-east-1', 'all', 'aws')
    const capsB = buildCapabilitiesCacheKey('us-east-1', 'all', 'azure')
    expect(capsA.startsWith('aws:')).toBe(true)
    expect(capsB.startsWith('azure:')).toBe(true)
    expect(capsA).not.toBe(capsB)

    const recsA = buildRecommendationsCacheKey(monthPeriod, 'us-east-1', 'all', 'aws')
    const recsB = buildRecommendationsCacheKey(monthPeriod, 'us-east-1', 'all', 'azure')
    expect(recsA).not.toBe(recsB)

    const sumA = buildSummaryCacheKey(monthPeriod, 'us-east-1', 'all', 'aws')
    const sumB = buildSummaryCacheKey(monthPeriod, 'us-east-1', 'all', 'azure')
    expect(sumA).not.toBe(sumB)
  })

  it('utilization cache key includes the provider segment', () => {
    const body = {
      region: 'us-east-1',
      lookback_days: 30 as const,
      resource_types: ['ec2'],
      account_id: 'all',
    }
    const aws_provider_key = buildUtilizationCacheKey(body, 'aws')
    const azureKey = buildUtilizationCacheKey(body, 'azure')
    expect(aws_provider_key.startsWith('aws:')).toBe(true)
    expect(azureKey.startsWith('azure:')).toBe(true)
    expect(aws_provider_key).not.toBe(azureKey)
  })

  it('all cache keys include account_id so the account switch stays isolated', () => {
    const a = buildCostsCacheKey(monthPeriod, 'us-east-1', 'acct:1', 'aws')
    const b = buildCostsCacheKey(monthPeriod, 'us-east-1', 'acct:2', 'aws')
    expect(a).not.toBe(b)
  })

  it('all cache keys include region so a region switch stays isolated', () => {
    const a = buildCostsCacheKey(monthPeriod, 'us-east-1', 'all', 'aws')
    const b = buildCostsCacheKey(monthPeriod, 'eu-west-1', 'all', 'aws')
    expect(a).not.toBe(b)
  })
})

describe('Phase 7C.1 — rackspace is the third reserved provider', () => {
  it('costs cache key supports rackspace segment', () => {
    const key = buildCostsCacheKey(monthPeriod, 'DFW', 'all', 'rackspace')
    expect(key.startsWith('rackspace:')).toBe(true)
  })

  it('resources cache key supports rackspace segment', () => {
    const key = buildResourcesCacheKey('DFW', 'all', 'rackspace')
    expect(key.startsWith('rackspace:')).toBe(true)
  })
})
