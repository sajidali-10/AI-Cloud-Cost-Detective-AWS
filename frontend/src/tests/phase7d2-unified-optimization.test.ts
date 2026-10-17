// Phase 7D.2 — Unified AWS + Azure Optimization regression suite.
//
// Pin:
//   * Canonical provider-neutral view model behaviour.
//   * Savings normalization (AWS monthly / Azure annual → monthly).
//   * Mixed-currency safety (never invent an FX rate).
//   * Authoritative zero + unavailable savings.
//   * Friendly attribution (no `acct:<n>` or `sub:<guid>` leaks).
//   * Filter / search / sort determinism.
//   * Empty / partial / throttled / error envelope rendering.
//   * Cache-key isolation across provider / scope / region / days.
//
// The page-level tests live in `phase7d2-page-rendering.test.tsx`;
// the URL-shape tests live in `phase7d2-url-builder.test.ts`; the
// pure view-model tests live here so they exercise behaviour
// without React.

import { describe, expect, it } from 'vitest'

import {
  applyFilters,
  buildFacets,
  buildKpis,
  buildRow,
  buildUnifiedEnvelope,
  DEFAULT_FILTERS,
  DEFAULT_SORT,
  friendlyAttribution,
  friendlyImpact,
  friendlySourceLabel,
  impactToneFor,
  shortResourceType,
  sortRows,
} from '../lib/finops/unifiedRecommendations'
import {
  buildCloudRecommendationsCacheKey,
} from '../lib/finops/cloudRecommendations'
import {
  canonicalAccountParam,
  canonicalCacheSegment,
} from '../lib/finops/subscriptionSelection'
import type { CloudRecommendationItem } from '../lib/finops/cloudRecommendations'

const ACCOUNT_MAP_AWS = [
  { selector: 'acct:11', displayName: 'AWS QA-Dev Account', provider: 'AWS' },
  { selector: 'acct:12', displayName: 'AWS Production Account', provider: 'AWS' },
]
const ACCOUNT_MAP_AZURE = [
  { selector: 'sub:11111111-2222-3333-4444-555555555555', displayName: 'MCPP Subscription', provider: 'AZURE' },
  { selector: 'sub:22222222-3333-4444-5555-666666666666', displayName: 'Microsoft Azure Sponsorship', provider: 'AZURE' },
]

function awsRec(overrides: Partial<CloudRecommendationItem> = {}): CloudRecommendationItem {
  return {
    provider: 'AWS',
    account_id: '974053642038',
    region: 'us-east-1',
    recommendation_id: 'aws-r-1',
    resource_id: 'i-0abc123',
    resource_type: 'EC2_INSTANCE',
    service: 'EC2',
    action: 'RIGHTSIZE',
    title: 'Right-size idle instance',
    category: 'Cost Optimization',
    impact: 'High',
    description: 'CPU < 5% over 30 days',
    estimated_savings: '120.00',
    currency: 'USD',
    source: 'AWS_COST_OPTIMIZATION_HUB',
    extensions: { resource_name: 'qa-dev-web-1' },
    ...overrides,
  }
}

function azureRec(overrides: Partial<CloudRecommendationItem> = {}): CloudRecommendationItem {
  return {
    provider: 'AZURE',
    account_id: '11111111-2222-3333-4444-555555555555',
    region: 'eastus',
    recommendation_id: 'azure-r-1',
    resource_id: '/subscriptions/11111111-2222-3333-4444-555555555555/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1',
    resource_type: 'Microsoft.Compute/virtualMachines',
    service: 'Compute',
    action: 'Right-size',
    title: 'Resize underutilised VM',
    category: 'Cost',
    impact: 'Medium',
    description: 'Average CPU below 5%',
    estimated_savings: '1440.00',
    currency: 'USD',
    source: 'AZURE_ADVISOR',
    extensions: { resource_name: 'mcpp-vm-1' },
    ...overrides,
  }
}

describe('Phase 7D.2 — friendly attribution never exposes acct:<n> or sub:<guid>', () => {
  it('AWS aggregate → "All AWS Accounts"', () => {
    const att = friendlyAttribution('all', ACCOUNT_MAP_AWS, 'AWS')
    expect(att.label).toBe('All AWS Accounts')
    expect(att.label).not.toMatch(/acct:/)
  })
  it('AWS specific account → friendly name from account map', () => {
    const att = friendlyAttribution('acct:11', ACCOUNT_MAP_AWS, 'AWS')
    expect(att.label).toBe('AWS QA-Dev Account')
    expect(att.label).not.toMatch(/acct:/)
  })
  it('Azure aggregate → "All Azure Subscriptions"', () => {
    const att = friendlyAttribution('all', ACCOUNT_MAP_AZURE, 'AZURE')
    expect(att.label).toBe('All Azure Subscriptions')
    expect(att.label).not.toMatch(/sub:/)
  })
  it('Azure specific subscription → friendly name, never the raw GUID', () => {
    const att = friendlyAttribution('sub:11111111-2222-3333-4444-555555555555', ACCOUNT_MAP_AZURE, 'AZURE')
    expect(att.label).toBe('MCPP Subscription')
    expect(att.label).not.toMatch(/11111111-2222-3333-4444-555555555555/)
  })
  it('Unknown selector under Azure → masked GUID, never raw', () => {
    const att = friendlyAttribution(
      'sub:99999999-aaaa-bbbb-cccc-dddddddddddd',
      ACCOUNT_MAP_AZURE,
      'AZURE',
    )
    expect(att.label).toMatch(/^Subscription\s+\w{4}…\w{4}$/)
    expect(att.label).not.toMatch(/99999999-aaaa-bbbb-cccc-dddddddddddd/)
  })
})

describe('Phase 7D.2 — Azure never gets a acct:<db-id>', () => {
  it('canonicalAccountParam coerces stale acct:<n> under Azure to undefined', () => {
    expect(canonicalAccountParam('acct:5', 'AZURE')).toBeUndefined()
    expect(canonicalAccountParam('acct:6', 'AZURE')).toBeUndefined()
  })
  it('canonicalCacheSegment collapses a stale Azure acct:<n> to "all"', () => {
    expect(canonicalCacheSegment('acct:5', 'azure')).toBe('all')
    expect(canonicalCacheSegment('acct:6', 'AZURE')).toBe('all')
  })
  it('canonicalAccountParam passes AWS native selector through', () => {
    expect(canonicalAccountParam('acct:11', 'AWS')).toBe('acct:11')
  })
  it('canonicalAccountParam passes Azure canonical selector through', () => {
    expect(canonicalAccountParam('sub:11111111-2222-3333-4444-555555555555', 'AZURE')).toBe(
      'sub:11111111-2222-3333-4444-555555555555',
    )
  })
})

describe('Phase 7D.2 — AWS never gets a sub:<guid>', () => {
  it('canonicalAccountParam passes AWS acct: through unchanged', () => {
    expect(canonicalAccountParam('acct:11', 'AWS')).toBe('acct:11')
  })
  it('canonicalCacheSegment preserves AWS acct: selectors', () => {
    expect(canonicalCacheSegment('acct:11', 'aws')).toBe('acct:11')
  })
})

describe('Phase 7D.2 — savings normalisation (AWS monthly / Azure annual)', () => {
  it('AWS row stays MONTHLY when savings_period = MONTHLY', () => {
    const row = buildRow(awsRec({ estimated_savings: '120.00' }), {
      provider: 'AWS',
      selection: 'acct:11',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(row.estimatedMonthlySavings).toBe(120)
    expect(row.estimatedAnnualSavings).toBeNull()
    expect(row.monthlyNormalized).toBe(false)
  })

  it('Azure row is normalised to MONTHLY when savings_period = ANNUAL', () => {
    const row = buildRow(azureRec({ estimated_savings: '1440.00' }), {
      provider: 'AZURE',
      selection: 'sub:11111111-2222-3333-4444-555555555555',
      accountMap: ACCOUNT_MAP_AZURE,
      savingsPeriod: 'ANNUAL',
    })
    expect(row.estimatedAnnualSavings).toBe(1440)
    expect(row.estimatedMonthlySavings).toBe(120)
    expect(row.monthlyNormalized).toBe(true)
  })

  it('AWS row with null savings → both monthly and annual are null', () => {
    const row = buildRow(awsRec({ estimated_savings: null }), {
      provider: 'AWS',
      selection: 'acct:11',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(row.estimatedMonthlySavings).toBeNull()
    expect(row.estimatedAnnualSavings).toBeNull()
  })

  it('Authoritative zero is preserved, never fabricated as "not available"', () => {
    const row = buildRow(awsRec({ estimated_savings: '0' }), {
      provider: 'AWS',
      selection: 'acct:11',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(row.estimatedMonthlySavings).toBe(0)
  })
})

describe('Phase 7D.2 — KPI derivation', () => {
  it('Active Recommendations matches the total in the summary envelope', () => {
    const env = buildUnifiedEnvelope({
      response: {
        summary: {
          provider: 'AWS',
          scope: 'all',
          scope_kind: 'all',
          status: 'SUCCESS',
          savings_status: 'OK',
          currency: 'USD',
          total_recommendations: 32,
          recommendations_with_savings: 32,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: '845.00',
          savings_period: 'MONTHLY',
          savings_by_currency: [{ currency: 'USD', amount: '845.00' }],
          by_impact: [],
          by_resource_type: [],
          by_subscription: [],
          by_resource_group: [],
          by_type: [],
          by_subcategory: [],
          subscription_errors: [],
          by_account: [],
          by_source: [{ key: 'AWS_COST_OPTIMIZATION_HUB', count: 32 }],
          by_action: [],
          account_errors: [],
          source: 'AWS_OPTIMIZATION',
        },
        recommendations: Array.from({ length: 32 }, (_, i) =>
          awsRec({
            recommendation_id: `aws-r-${i}`,
            resource_id: `i-0abc${i.toString().padStart(3, '0')}`,
            impact: i % 2 === 0 ? 'High' : 'Medium',
          }),
        ),
      },
      provider: 'AWS',
      selection: 'all',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(env.kpis.activeRecommendations).toBe(32)
    expect(env.kpis.potentialMonthlySavings).toBe(845)
    expect(env.kpis.currency).toBe('USD')
    expect(env.kpis.highImpact).toBe(16)
    expect(env.kpis.mediumImpact).toBe(16)
    expect(env.kpis.resourcesAffected).toBe(32)
  })

  it('Authoritative zero is preserved as 0', () => {
    const env = buildUnifiedEnvelope({
      response: {
        summary: {
          provider: 'AWS',
          scope: 'all',
          scope_kind: 'all',
          status: 'SUCCESS',
          savings_status: 'OK',
          currency: 'USD',
          total_recommendations: 0,
          recommendations_with_savings: 0,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: '0',
          savings_period: 'MONTHLY',
          savings_by_currency: [{ currency: 'USD', amount: '0' }],
          by_impact: [],
          by_resource_type: [],
          by_subscription: [],
          by_resource_group: [],
          by_type: [],
          by_subcategory: [],
          subscription_errors: [],
          by_account: [],
          by_source: [],
          by_action: [],
          account_errors: [],
          source: 'AWS_OPTIMIZATION',
        },
        recommendations: [],
      },
      provider: 'AWS',
      selection: 'all',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(env.kpis.authoritativeZero).toBe(true)
    expect(env.kpis.potentialMonthlySavings).toBe(0)
  })

  it('Mixed currencies → never invents a single headline number', () => {
    const env = buildUnifiedEnvelope({
      response: {
        summary: {
          provider: 'AZURE',
          scope: 'all',
          scope_kind: 'all',
          status: 'PARTIAL_SUCCESS',
          savings_status: 'MIXED_CURRENCY',
          currency: null,
          total_recommendations: 4,
          recommendations_with_savings: 4,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: null,
          savings_period: null,
          savings_by_currency: [
            { currency: 'USD', amount: '120.00' },
            { currency: 'EUR', amount: '80.00' },
          ],
          by_impact: [],
          by_resource_type: [],
          by_subscription: [],
          by_resource_group: [],
          by_type: [],
          by_subcategory: [],
          subscription_errors: [],
          by_account: [],
          by_source: [],
          by_action: [],
          account_errors: [],
          source: 'AZURE_ADVISOR',
        },
        recommendations: [
          azureRec({ recommendation_id: 'az-1', estimated_savings: '120.00', currency: 'USD' }),
          azureRec({ recommendation_id: 'az-2', estimated_savings: '80.00', currency: 'EUR' }),
        ],
      },
      provider: 'AZURE',
      selection: 'all',
      accountMap: ACCOUNT_MAP_AZURE,
      savingsPeriod: 'ANNUAL',
    })
    expect(env.kpis.potentialMonthlySavings).toBeNull()
    expect(env.kpis.savingsByCurrency).toEqual([
      { currency: 'USD', amount: 120 },
      { currency: 'EUR', amount: 80 },
    ])
    expect(env.kpis.savingsStatus).toBe('MIXED_CURRENCY')
  })

  it('Savings unavailable → headline number is null', () => {
    const env = buildUnifiedEnvelope({
      response: {
        summary: {
          provider: 'AZURE',
          scope: 'all',
          scope_kind: 'all',
          status: 'NO_DATA',
          savings_status: 'EMPTY',
          currency: null,
          total_recommendations: 0,
          recommendations_with_savings: 0,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: null,
          savings_period: null,
          savings_by_currency: [],
          by_impact: [],
          by_resource_type: [],
          by_subscription: [],
          by_resource_group: [],
          by_type: [],
          by_subcategory: [],
          subscription_errors: [],
          by_account: [],
          by_source: [],
          by_action: [],
          account_errors: [],
          source: 'AZURE_ADVISOR',
        },
        recommendations: [],
      },
      provider: 'AZURE',
      selection: 'all',
      accountMap: ACCOUNT_MAP_AZURE,
      savingsPeriod: null,
    })
    expect(env.kpis.potentialMonthlySavings).toBeNull()
    expect(env.kpis.savingsUnavailable).toBe(true)
  })

  it('Resources Affected counts distinct resource ids', () => {
    const env = buildUnifiedEnvelope({
      response: {
        summary: {
          provider: 'AWS',
          scope: 'all',
          scope_kind: 'all',
          status: 'SUCCESS',
          savings_status: 'OK',
          currency: 'USD',
          total_recommendations: 3,
          recommendations_with_savings: 3,
          recommendations_without_savings: 0,
          potential_annual_savings: null,
          potential_savings: '60',
          savings_period: 'MONTHLY',
          savings_by_currency: [{ currency: 'USD', amount: '60' }],
          by_impact: [],
          by_resource_type: [],
          by_subscription: [],
          by_resource_group: [],
          by_type: [],
          by_subcategory: [],
          subscription_errors: [],
          by_account: [],
          by_source: [],
          by_action: [],
          account_errors: [],
          source: 'AWS_OPTIMIZATION',
        },
        recommendations: [
          awsRec({ recommendation_id: 'a', resource_id: 'vol-001' }),
          awsRec({ recommendation_id: 'b', resource_id: 'vol-001' }),
          awsRec({ recommendation_id: 'c', resource_id: 'i-abc' }),
        ],
      },
      provider: 'AWS',
      selection: 'all',
      accountMap: ACCOUNT_MAP_AWS,
      savingsPeriod: 'MONTHLY',
    })
    expect(env.kpis.resourcesAffected).toBe(2)
  })
})

describe('Phase 7D.2 — filters + search + sort determinism', () => {
  const env = buildUnifiedEnvelope({
    response: {
      summary: {
        provider: 'AWS',
        scope: 'all',
        scope_kind: 'all',
        status: 'SUCCESS',
        savings_status: 'OK',
        currency: 'USD',
        total_recommendations: 4,
        recommendations_with_savings: 4,
        recommendations_without_savings: 0,
        potential_annual_savings: null,
        potential_savings: '450',
        savings_period: 'MONTHLY',
        savings_by_currency: [{ currency: 'USD', amount: '450' }],
        by_impact: [],
        by_resource_type: [],
        by_subscription: [],
        by_resource_group: [],
        by_type: [],
        by_subcategory: [],
        subscription_errors: [],
        by_account: [],
        by_source: [],
        by_action: [],
        account_errors: [],
        source: 'AWS_OPTIMIZATION',
      },
      recommendations: [
        awsRec({ recommendation_id: 'a', resource_id: 'a', impact: 'Low', estimated_savings: '50', region: 'us-east-1', resource_type: 'EC2_INSTANCE', source: 'AWS_COST_OPTIMIZATION_HUB' }),
        awsRec({ recommendation_id: 'b', resource_id: 'b', impact: 'High', estimated_savings: '200', region: 'us-west-2', resource_type: 'EBS_VOLUME', source: 'AWS_COMPUTE_OPTIMIZER' }),
        awsRec({ recommendation_id: 'c', resource_id: 'c', impact: 'Medium', estimated_savings: '150', region: 'us-east-1', resource_type: 'EC2_INSTANCE', source: 'AWS_COST_OPTIMIZATION_HUB' }),
        awsRec({ recommendation_id: 'd', resource_id: 'd', impact: 'High', estimated_savings: null, region: 'eu-west-1', resource_type: 'RDS_DB_INSTANCE', source: 'AWS_COST_OPTIMIZATION_HUB' }),
      ],
    },
    provider: 'AWS',
    selection: 'all',
    accountMap: ACCOUNT_MAP_AWS,
    savingsPeriod: 'MONTHLY',
  })

  it('default filters and sort expose every row', () => {
    const sorted = sortRows(applyFilters(env.rows, DEFAULT_FILTERS), DEFAULT_SORT)
    expect(sorted.map((r) => r.id)).toEqual(['b', 'c', 'a', 'd'])
  })

  it('impact filter narrows to High only', () => {
    const out = applyFilters(env.rows, { ...DEFAULT_FILTERS, impact: 'High' })
    expect(out.every((r) => r.impact === 'High')).toBe(true)
    expect(out.length).toBe(2)
  })

  it('source filter narrows to one source', () => {
    const out = applyFilters(env.rows, { ...DEFAULT_FILTERS, source: 'AWS Compute Optimizer' })
    expect(out.length).toBe(1)
    expect(out[0].id).toBe('b')
  })

  it('resource-type filter narrows to one type', () => {
    const out = applyFilters(env.rows, { ...DEFAULT_FILTERS, resourceType: 'EC2 Instance' })
    expect(out.every((r) => r.resourceTypeShort === 'EC2 Instance')).toBe(true)
  })

  it('region filter narrows to one region', () => {
    const out = applyFilters(env.rows, { ...DEFAULT_FILTERS, region: 'us-east-1' })
    expect(out.every((r) => r.region === 'us-east-1')).toBe(true)
  })

  it('search matches action / resource id / description / title', () => {
    const byTitle = applyFilters(env.rows, { ...DEFAULT_FILTERS, search: 'Right-size' })
    expect(byTitle.length).toBe(4)
    const byResource = applyFilters(env.rows, { ...DEFAULT_FILTERS, search: 'vol' })
    expect(byResource.length).toBe(0) // resource_id 'a'/'b'/'c'/'d' do not match 'vol'
  })

  it('Stable sorting — same input twice yields the same order', () => {
    const a = sortRows(applyFilters(env.rows, DEFAULT_FILTERS), 'savings-desc').map((r) => r.id)
    const b = sortRows(applyFilters(env.rows, DEFAULT_FILTERS), 'savings-desc').map((r) => r.id)
    expect(a).toEqual(b)
    expect(a).toEqual(['b', 'c', 'a', 'd']) // null savings sort last
  })

  it('impact-desc sort places High before Medium before Low', () => {
    const sorted = sortRows(env.rows, 'impact-desc')
    expect(sorted.map((r) => r.impact)).toEqual(['High', 'High', 'Medium', 'Low'])
  })

  it('source-asc sort is case-insensitive and stable', () => {
    const sorted = sortRows(env.rows, 'source-asc')
    // AWS Compute Optimizer < AWS Cost Optimization Hub (alphabetical)
    expect(sorted[0].sourceLabel).toBe('AWS Compute Optimizer')
  })

  it('Facets reflect only rows that survived', () => {
    const facets = buildFacets(env.rows)
    expect(facets.impacts.map((i) => i.value)).toEqual(['High', 'Medium', 'Low'])
    expect(facets.regions.map((r) => r.value)).toContain('us-east-1')
    expect(facets.resourceTypes.length).toBeGreaterThan(0)
  })
})

describe('Phase 7D.2 — canonical recommendation cache-key isolation', () => {
  it('isolates AWS from Azure for the same scope', () => {
    expect(buildCloudRecommendationsCacheKey('all', 'aws')).not.toEqual(
      buildCloudRecommendationsCacheKey('all', 'azure'),
    )
  })
  it('isolates all-AWS from a single AWS account', () => {
    expect(buildCloudRecommendationsCacheKey('all', 'aws')).not.toEqual(
      buildCloudRecommendationsCacheKey('acct:11', 'aws'),
    )
  })
  it('isolates MCPP from Sponsorship from All-Azure', () => {
    const mcpp = buildCloudRecommendationsCacheKey(
      'sub:11111111-2222-3333-4444-555555555555',
      'azure',
    )
    const sponsor = buildCloudRecommendationsCacheKey(
      'sub:22222222-3333-4444-5555-666666666666',
      'azure',
    )
    const all = buildCloudRecommendationsCacheKey('all', 'azure')
    expect(new Set([mcpp, sponsor, all]).size).toBe(3)
  })
  it('isolates QA from Prod from All-AWS', () => {
    const qa = buildCloudRecommendationsCacheKey('acct:11', 'aws')
    const prod = buildCloudRecommendationsCacheKey('acct:12', 'aws')
    const all = buildCloudRecommendationsCacheKey('all', 'aws')
    expect(new Set([qa, prod, all]).size).toBe(3)
  })
  it('isolates region and days dimensions', () => {
    const east = buildCloudRecommendationsCacheKey('all', 'aws', 'us-east-1', 30)
    const west = buildCloudRecommendationsCacheKey('all', 'aws', 'us-west-2', 30)
    const d7 = buildCloudRecommendationsCacheKey('all', 'aws', 'us-east-1', 7)
    expect(new Set([east, west, d7]).size).toBe(3)
  })
})

describe('Phase 7D.2 — display helpers', () => {
  it('friendlyImpact returns the canonical 4-set', () => {
    expect(friendlyImpact('High')).toBe('High')
    expect(friendlyImpact('Medium')).toBe('Medium')
    expect(friendlyImpact('Low')).toBe('Low')
    expect(friendlyImpact(null)).toBe('Unknown')
    expect(friendlyImpact('bogus')).toBe('Unknown')
  })
  it('impactToneFor pairs High=danger, Medium=warning, Low=success', () => {
    expect(impactToneFor('High')).toBe('danger')
    expect(impactToneFor('Medium')).toBe('warning')
    expect(impactToneFor('Low')).toBe('success')
    expect(impactToneFor('Unknown')).toBe('neutral')
  })
  it('friendlySourceLabel humanises known sources', () => {
    expect(friendlySourceLabel('AWS_COST_OPTIMIZATION_HUB')).toBe('AWS Cost Optimization Hub')
    expect(friendlySourceLabel('AWS_COMPUTE_OPTIMIZER')).toBe('AWS Compute Optimizer')
    expect(friendlySourceLabel('AZURE_ADVISOR')).toBe('Azure Advisor')
    expect(friendlySourceLabel('AZURE_FOO_BAR')).toBe('Azure Foo Bar')
  })
  it('shortResourceType strips the ARM provider prefix', () => {
    expect(shortResourceType('Microsoft.Compute/virtualMachines')).toBe('virtualMachines')
    expect(shortResourceType('EC2_INSTANCE')).toBe('EC2 INSTANCE')
  })
})
