// Phase 7D.2 — Unified AWS + Azure Optimization view model.
//
// The Optimization page now renders for both providers from one
// provider-neutral pipeline:
//
//   ProviderContext + AccountContext  →  canonical fetch
//                                         (fetchCloudRecommendations)
//                                       ↓
//                            unified recommendation row
//                            (UnifiedRecommendationRow)
//                                       ↓
//                            shared KPIs / filters / sort /
//                            empty / partial / error rendering.
//
// Provider neutrality rules (Phase 7D.2):
//
//   * The row shape ALWAYS lives here; never inside a page.
//   * `provider` is a discriminator, not a branching trigger:
//     - When a value is provider-inherent (savings_period, currency)
//       the row carries it.  When a value is presentation only
//       (badge label, KPI tone) the page renders it from the row.
//   * Savings are normalised to "MONTHLY" for the headline KPI.
//     Azure values are annual at the wire; the conversion here is
//     *display-only* (divided by 12) when `savings_period ===
//     'ANNUAL'`.  The original annual figure is preserved in
//     `monthlyNormalized.fromAnnual` so tests can assert the path.
//   * Attribution uses friendly names only.  acct:<n> never
//     surfaces; sub:<guid> never surfaces.
//   * Filters / search / sort are derived from the same shape for
//     both providers — no separate AWS / Azure implementations.

import type {
  CloudRecommendationAccountError,
  CloudRecommendationGroupCount,
  CloudRecommendationItem,
  CloudRecommendationListResponse,
  CloudRecommendationSummaryEnvelope,
} from './cloudRecommendations'

export type Impact = 'High' | 'Medium' | 'Low' | 'Unknown'

export interface FriendlyAttribution {
  /** Always the provider-neutral display label ("QA-Dev Account", "MCPP Subscription"). */
  label: string
  /** Friendly account id masked ("QA-Dev Account", "MCPP Subscription", or "All AWS Accounts"). */
  maskedId: string
  /** The underlying canonical selector — used by tests, never displayed. */
  canonicalSelector: string
}

export interface UnifiedRecommendationRow {
  /** Stable row id for React keys / sort tiebreakers. */
  id: string
  provider: 'AWS' | 'AZURE' | string
  source: string
  sourceLabel: string
  category: string | null
  impact: Impact
  impactTone: 'danger' | 'warning' | 'success' | 'neutral'
  resource: string
  resourceId: string
  resourceType: string
  resourceTypeShort: string
  region: string | null
  action: string
  title: string
  description: string
  estimatedMonthlySavings: number | null
  /** Raw annual figure when the backend emitted ANNUAL savings. */
  estimatedAnnualSavings: number | null
  currency: string | null
  attribution: FriendlyAttribution
  /** True when this row's savings figure was synthesised from an
   *  ANNUAL value (Azure Advisor).  Used by tests + UI hints. */
  monthlyNormalized: boolean
  /** Raw record kept for downstream consumers that want the
   *  underlying extensions or original savings_period. */
  raw: CloudRecommendationItem
}

export interface UnifiedKpis {
  activeRecommendations: number
  potentialMonthlySavings: number | null
  /** Provider-neutral currency for the headline savings number. */
  currency: string | null
  savingsStatus: 'OK' | 'MIXED_CURRENCY' | 'EMPTY' | string
  /** Per-currency totals — populated when status === 'MIXED_CURRENCY'. */
  savingsByCurrency: Array<{ currency: string; amount: number }>
  resourcesAffected: number
  highImpact: number
  mediumImpact: number
  lowImpact: number
  recommendationSources: Array<{ key: string; label: string; count: number }>
  status: string
  /** True when zero is authoritative (not a fabrication). */
  authoritativeZero: boolean
  /** True when savings have no authoritative figure at all. */
  savingsUnavailable: boolean
}

export interface UnifiedEnvelope {
  provider: 'AWS' | 'AZURE' | string
  scope: string
  scopeKind: 'all' | 'single' | string
  status: 'SUCCESS' | 'PARTIAL_SUCCESS' | 'NO_DATA' | 'ERROR' | string
  kpis: UnifiedKpis
  rows: UnifiedRecommendationRow[]
  /** Account / subscription-level failures surfaced for "Partial data". */
  partialErrors: CloudRecommendationAccountError[]
  /** Raw response (for tests / future consumers). */
  raw: CloudRecommendationListResponse
}

// ---------------------------------------------------------------------------
// Display-only helpers
// ---------------------------------------------------------------------------

const KNOWN_AWS_SOURCES: Record<string, string> = {
  AWS_COST_OPTIMIZATION_HUB: 'AWS Cost Optimization Hub',
  AWS_COMPUTE_OPTIMIZER: 'AWS Compute Optimizer',
  AWS_OPTIMIZATION: 'Cost Detective',
  CALCULATED: 'Cost Detective',
  UNKNOWN: 'Cost Detective',
  DETERMINISTIC_ENGINE: 'Cost Detective',
  COST_DETECTIVE: 'Cost Detective',
}

const KNOWN_AZURE_SOURCES: Record<string, string> = {
  AZURE_ADVISOR: 'Azure Advisor',
}

// Phase 9D.1 — Rackspace canonical sources.  The Phase 9C.1
// deterministic engine emits RACKSPACE_OPTIMIZATION; the billing
// adapter emits RACKSPACE_BILLING (cost surfaces).
const KNOWN_RACKSPACE_SOURCES: Record<string, string> = {
  RACKSPACE_OPTIMIZATION: 'Rackspace Optimization',
  RACKSPACE_BILLING: 'Rackspace Billing',
  RACKSPACE_CLOUD_API: 'Rackspace Cloud API',
}

export function friendlySourceLabel(source: string): string {
  if (KNOWN_AWS_SOURCES[source]) return KNOWN_AWS_SOURCES[source]
  if (KNOWN_AZURE_SOURCES[source]) return KNOWN_AZURE_SOURCES[source]
  if (KNOWN_RACKSPACE_SOURCES[source]) return KNOWN_RACKSPACE_SOURCES[source]
  // Generic humanisation: AZURE_FOO_BAR → "Azure Foo Bar".
  if (source.startsWith('AZURE_')) {
    const tail = source
      .slice('AZURE_'.length)
      .toLowerCase()
      .split('_')
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ')
    return tail ? `Azure ${tail}` : 'Azure'
  }
  if (source.startsWith('AWS_')) {
    const tail = source
      .slice('AWS_'.length)
      .toLowerCase()
      .split('_')
      .filter(Boolean)
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ')
    return tail ? `AWS ${tail}` : 'AWS'
  }
  return source
}

export function friendlyImpact(impact: string | null | undefined): Impact {
  if (!impact) return 'Unknown'
  const lower = String(impact).toLowerCase()
  if (lower === 'high') return 'High'
  if (lower === 'medium') return 'Medium'
  if (lower === 'low') return 'Low'
  return 'Unknown'
}

export function impactToneFor(impact: Impact): 'danger' | 'warning' | 'success' | 'neutral' {
  if (impact === 'High') return 'danger'
  if (impact === 'Medium') return 'warning'
  if (impact === 'Low') return 'success'
  return 'neutral'
}

export function shortResourceType(rt: string): string {
  if (!rt) return '—'
  const parts = rt.split('/')
  return (parts[parts.length - 1] || rt).replace(/_/g, ' ')
}

// ---------------------------------------------------------------------------
// Attribution
// ---------------------------------------------------------------------------

export interface AccountMapEntry {
  selector: string
  /** Raw account / subscription identifier as reported by the backend. */
  rawAccountId?: string | null
  displayName: string
  /** Provider as reported by AccountContext. */
  provider: 'AWS' | 'AZURE' | string
}

/**
 * Map an internal selector to a friendly display label.
 *
 * `accountMap` is the list of selectable rows from AccountContext
 * (already filtered to the active provider by the selector).
 * Friendly labels only — never expose `acct:<n>` or a raw
 * `sub:<guid>` in the UI.  Test fixtures may use obviously fake
 * GUIDs (e.g. `00000000-0000-0000-0000-000000000000`).
 */
export function friendlyAttribution(
  selector: string | null,
  accountMap: ReadonlyArray<AccountMapEntry>,
  provider: 'AWS' | 'AZURE' | string,
): FriendlyAttribution {
  // Aggregate scopes.
  if (!selector || selector === 'all') {
    if (provider === 'AZURE') {
      return {
        label: 'All Azure Subscriptions',
        maskedId: 'All Azure Subscriptions',
        canonicalSelector: 'all',
      }
    }
    if (provider === 'AWS') {
      return {
        label: 'All AWS Accounts',
        maskedId: 'All AWS Accounts',
        canonicalSelector: 'all',
      }
    }
    if (provider === 'RACKSPACE') {
      return {
        label: 'All Rackspace Cloud Accounts',
        maskedId: 'All Rackspace Cloud Accounts',
        canonicalSelector: 'all',
      }
    }
    return {
      label: `All ${String(provider)} Accounts`,
      maskedId: `All ${String(provider)} Accounts`,
      canonicalSelector: 'all',
    }
  }
  // Specific selector — match by canonical selector first, then
  // by the raw account / subscription id (which is what the
  // backend carries on each recommendation row).
  const found =
    accountMap.find((a) => a.selector === selector) ||
    accountMap.find((a) => a.rawAccountId != null && a.rawAccountId === selector)
  if (found) {
    return {
      label: found.displayName,
      maskedId: found.displayName,
      canonicalSelector: selector,
    }
  }
  // Self-corrected stale selector or unknown row — never leak raw.
  if (provider === 'AZURE' && selector.startsWith('sub:')) {
    const guid = selector.slice('sub:'.length)
    const masked = `${guid.slice(0, 4)}…${guid.slice(-4)}`
    return {
      label: `Subscription ${masked}`,
      maskedId: masked,
      canonicalSelector: selector,
    }
  }
  if (provider === 'AWS' && selector.startsWith('acct:')) {
    return {
      label: `Account ${selector.slice('acct:'.length)}`,
      maskedId: selector.slice('acct:'.length),
      canonicalSelector: selector,
    }
  }
  // Phase 9D.1 — Rackspace accounts share the AWS-style ``acct:<n>``
  // selector form (the backend maps the local row id to the
  // registered Cloud tenant).
  if (provider === 'RACKSPACE' && selector.startsWith('acct:')) {
    return {
      label: 'Rackspace Cloud Account',
      maskedId: selector.slice('acct:'.length),
      canonicalSelector: selector,
    }
  }
  return {
    label: 'Unknown scope',
    maskedId: 'Unknown',
    canonicalSelector: selector,
  }
}

// ---------------------------------------------------------------------------
// Row mapping
// ---------------------------------------------------------------------------

function parseDecimalSafe(v: unknown): number | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v !== 'string') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export interface BuildRowContext {
  provider: 'AWS' | 'AZURE' | string
  selection: string | null
  accountMap: ReadonlyArray<AccountMapEntry>
  /** Provider-aware savings period: AWS = MONTHLY, Azure = ANNUAL. */
  savingsPeriod: 'MONTHLY' | 'ANNUAL' | null
}

/**
 * Normalise one canonical recommendation into the shared row shape.
 *
 * Savings:
 *   * savingsPeriod === 'MONTHLY' → pass through.
 *   * savingsPeriod === 'ANNUAL'  → divide by 12 for the monthly
 *     presentation; preserve the annual figure in
 *     `estimatedAnnualSavings` so tests + detail panel can show it.
 *   * null                       → both null.
 */
export function buildRow(
  rec: CloudRecommendationItem,
  ctx: BuildRowContext,
): UnifiedRecommendationRow {
  const impact = friendlyImpact(rec.impact)
  const attribution = friendlyAttribution(rec.account_id ?? null, ctx.accountMap, ctx.provider)
  const rawSavings = parseDecimalSafe(rec.estimated_savings)
  let monthly: number | null = null
  let annual: number | null = null
  let monthlyNormalized = false
  if (rawSavings !== null) {
    if (ctx.savingsPeriod === 'ANNUAL') {
      annual = rawSavings
      monthly = rawSavings / 12
      monthlyNormalized = true
    } else if (ctx.savingsPeriod === 'MONTHLY') {
      monthly = rawSavings
      annual = null
    } else {
      // Unknown period: pass through, do not invent.
      monthly = rawSavings
      annual = null
    }
  }
  const description =
    rec.description ??
    (rec.extensions && typeof (rec.extensions as Record<string, unknown>)['description'] === 'string'
      ? ((rec.extensions as Record<string, unknown>)['description'] as string)
      : '')
  return {
    id: rec.recommendation_id || `${rec.provider}-${rec.resource_id}-${rec.action}`,
    provider: rec.provider,
    source: rec.source,
    sourceLabel: friendlySourceLabel(rec.source),
    category: rec.category ?? null,
    impact,
    impactTone: impactToneFor(impact),
    resource:
      (rec.extensions && typeof (rec.extensions as Record<string, unknown>)['resource_name'] === 'string'
        ? ((rec.extensions as Record<string, unknown>)['resource_name'] as string)
        : rec.resource_id) || '—',
    resourceId: rec.resource_id,
    resourceType: rec.resource_type || '—',
    resourceTypeShort: shortResourceType(rec.resource_type || ''),
    region: rec.region ?? null,
    action: rec.action,
    title: rec.title || rec.action || '—',
    description,
    estimatedMonthlySavings: monthly,
    estimatedAnnualSavings: annual,
    currency: rec.currency ?? null,
    attribution,
    monthlyNormalized,
    raw: rec,
  }
}

// ---------------------------------------------------------------------------
// KPI derivation
// ---------------------------------------------------------------------------

function pickSingleCurrency(
  raw: CloudRecommendationSummaryEnvelope,
): { currency: string | null; amount: number | null } {
  const totals = (raw.savings_by_currency ?? []).map((c) => ({
    currency: c.currency,
    amount: parseDecimalSafe(c.amount),
  }))
  const usable = totals.filter((t) => t.amount !== null)
  if (usable.length === 1) {
    return { currency: usable[0].currency, amount: usable[0].amount }
  }
  if (usable.length === 0) {
    return { currency: raw.currency ?? null, amount: parseDecimalSafe(raw.potential_savings) }
  }
  return { currency: null, amount: null }
}

export interface BuildKpisArgs {
  raw: CloudRecommendationSummaryEnvelope
  rows: UnifiedRecommendationRow[]
}

export function buildKpis({ raw, rows }: BuildKpisArgs): UnifiedKpis {
  const status = raw.status ?? 'SUCCESS'
  const savingsStatus = raw.savings_status ?? 'EMPTY'
  const total = raw.total_recommendations ?? rows.length

  // Derive impact counts from the actual rows so the KPI matches
  // the rendered table exactly even if the backend summary is
  // slightly stale.
  let high = 0
  let medium = 0
  let low = 0
  for (const r of rows) {
    if (r.impact === 'High') high += 1
    else if (r.impact === 'Medium') medium += 1
    else if (r.impact === 'Low') low += 1
  }

  const resourcesAffected = new Set(rows.map((r) => r.resourceId)).size

  // Headline savings — only show a single number when:
  //   * the backend explicitly reports ONE currency total (savings_status === 'OK')
  //   * OR zero is authoritative (savings_status === 'OK' and amount == 0)
  let headlineAmount: number | null = null
  let headlineCurrency: string | null = null
  let authoritativeZero = false
  let savingsUnavailable = false
  if (savingsStatus === 'OK') {
    // Pick the period-scope figure: for AWS the backend populates
    // `potential_savings` (MONTHLY); for Azure the canonical
    // envelope carries the ANNUAL total in `potential_annual_savings`
    // and may leave `potential_savings` null.  Handle both shapes
    // so the Phase 7D.3 dashboard normalises both correctly.
    const period = (raw.savings_period ?? '').toUpperCase()
    let periodAmount: number | null
    if (period === 'ANNUAL') {
      periodAmount =
        parseDecimalSafe(raw.potential_annual_savings) ??
        parseDecimalSafe(raw.potential_savings)
    } else {
      periodAmount =
        parseDecimalSafe(raw.potential_savings) ??
        parseDecimalSafe(raw.potential_annual_savings)
    }
    if (periodAmount === null) {
      headlineAmount = null
      headlineCurrency = raw.currency ?? null
      savingsUnavailable = true
    } else if (periodAmount === 0) {
      headlineAmount = 0
      headlineCurrency = raw.currency ?? null
      authoritativeZero = true
    } else {
      // Always present as MONTHLY.  Backend already reports MONTHLY
      // for AWS; for Azure convert from ANNUAL if needed.
      if (period === 'ANNUAL') {
        headlineAmount = periodAmount / 12
      } else {
        headlineAmount = periodAmount
      }
      headlineCurrency = raw.currency ?? null
    }
  } else if (savingsStatus === 'EMPTY') {
    headlineAmount = null
    headlineCurrency = null
    savingsUnavailable = true
  } else {
    // MIXED_CURRENCY or anything else — never invent a single number.
    headlineAmount = null
    headlineCurrency = null
  }

  // Per-currency totals (used for the "Mixed currencies" rendering).
  const savingsByCurrency = (raw.savings_by_currency ?? [])
    .map((c) => ({ currency: c.currency, amount: parseDecimalSafe(c.amount) ?? 0 }))
    .filter((c) => c.currency)

  // Recommendation sources breakdown — present both AWS (by_source)
  // and Azure (by_source).  The by_source field is provider-neutral
  // per Phase 7D.1.
  const sourcesRaw: CloudRecommendationGroupCount[] =
    (raw.by_source as CloudRecommendationGroupCount[] | undefined) ?? []
  const recommendationSources = sourcesRaw.map((entry) => ({
    key: entry.key,
    label: friendlySourceLabel(entry.key),
    count: entry.count,
  }))

  return {
    activeRecommendations: total,
    potentialMonthlySavings: headlineAmount,
    currency: headlineCurrency,
    savingsStatus,
    savingsByCurrency,
    resourcesAffected,
    highImpact: high,
    mediumImpact: medium,
    lowImpact: low,
    recommendationSources,
    status,
    authoritativeZero,
    savingsUnavailable,
  }
}

// ---------------------------------------------------------------------------
// Envelope assembly
// ---------------------------------------------------------------------------

export interface BuildEnvelopeArgs extends BuildRowContext {
  response: CloudRecommendationListResponse
}

export function buildUnifiedEnvelope(args: BuildEnvelopeArgs): UnifiedEnvelope {
  const period = (args.response.summary?.savings_period ?? args.savingsPeriod ?? null) as
    | 'MONTHLY'
    | 'ANNUAL'
    | null
  const rows = (args.response.recommendations ?? []).map((r) =>
    buildRow(r, { ...args, savingsPeriod: period }),
  )
  return {
    provider: args.response.summary?.provider ?? args.provider,
    scope: args.response.summary?.scope ?? 'all',
    scopeKind: args.response.summary?.scope_kind ?? 'all',
    status: args.response.summary?.status ?? 'SUCCESS',
    kpis: buildKpis({ raw: args.response.summary, rows }),
    rows,
    partialErrors: args.response.summary?.account_errors ?? [],
    raw: args.response,
  }
}

// ---------------------------------------------------------------------------
// Filters
// ---------------------------------------------------------------------------

export interface FilterState {
  impact: 'all' | Impact
  source: 'all' | string
  resourceType: 'all' | string
  region: 'all' | string
  search: string
}

export const DEFAULT_FILTERS: FilterState = {
  impact: 'all',
  source: 'all',
  resourceType: 'all',
  region: 'all',
  search: '',
}

export interface FilterFacets {
  impacts: Array<{ value: Impact; label: string; count: number }>
  sources: Array<{ value: string; label: string; count: number }>
  resourceTypes: Array<{ value: string; label: string; count: number }>
  regions: Array<{ value: string; label: string; count: number }>
}

export function buildFacets(rows: UnifiedRecommendationRow[]): FilterFacets {
  const impactMap = new Map<Impact, number>()
  const sourceMap = new Map<string, number>()
  const typeMap = new Map<string, number>()
  const regionMap = new Map<string, number>()
  for (const r of rows) {
    impactMap.set(r.impact, (impactMap.get(r.impact) ?? 0) + 1)
    sourceMap.set(r.sourceLabel, (sourceMap.get(r.sourceLabel) ?? 0) + 1)
    typeMap.set(r.resourceTypeShort, (typeMap.get(r.resourceTypeShort) ?? 0) + 1)
    if (r.region) {
      regionMap.set(r.region, (regionMap.get(r.region) ?? 0) + 1)
    }
  }
  const impacts: FilterFacets['impacts'] = (['High', 'Medium', 'Low', 'Unknown'] as Impact[])
    .map((v) => ({ value: v, label: v, count: impactMap.get(v) ?? 0 }))
    .filter((entry) => entry.count > 0)
  const sources: FilterFacets['sources'] = Array.from(sourceMap.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([value, count]) => ({ value, label: value, count }))
  const resourceTypes: FilterFacets['resourceTypes'] = Array.from(typeMap.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([value, count]) => ({ value, label: value, count }))
  const regions: FilterFacets['regions'] = Array.from(regionMap.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([value, count]) => ({ value, label: value, count }))
  return { impacts, sources, resourceTypes, regions }
}

export function applyFilters(
  rows: UnifiedRecommendationRow[],
  filters: FilterState,
): UnifiedRecommendationRow[] {
  const search = filters.search.trim().toLowerCase()
  return rows.filter((row) => {
    if (filters.impact !== 'all' && row.impact !== filters.impact) return false
    if (filters.source !== 'all' && row.sourceLabel !== filters.source) return false
    if (filters.resourceType !== 'all' && row.resourceTypeShort !== filters.resourceType)
      return false
    if (filters.region !== 'all' && row.region !== filters.region) return false
    if (search) {
      const haystack = [
        row.resource,
        row.resourceId,
        row.action,
        row.title,
        row.description,
      ]
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(search)) return false
    }
    return true
  })
}

// ---------------------------------------------------------------------------
// Sorting — highest savings → impact → source → resource.
// Stable: same input always yields the same order (no Date.now, no
// random IDs leaking into the comparator).
// ---------------------------------------------------------------------------

export type SortKey =
  | 'savings-desc'
  | 'impact-desc'
  | 'source-asc'
  | 'resource-asc'

export const DEFAULT_SORT: SortKey = 'savings-desc'

const IMPACT_RANK: Record<Impact, number> = {
  High: 0,
  Medium: 1,
  Low: 2,
  Unknown: 3,
}

export function sortRows(
  rows: UnifiedRecommendationRow[],
  key: SortKey,
): UnifiedRecommendationRow[] {
  const copy = [...rows]
  copy.sort((a, b) => {
    switch (key) {
      case 'savings-desc': {
        const av = a.estimatedMonthlySavings
        const bv = b.estimatedMonthlySavings
        // Nulls sort last so we never fabricate a $0 to fill a gap.
        if (av === null && bv === null) {
          return stableStringCompare(a, b)
        }
        if (av === null) return 1
        if (bv === null) return -1
        if (bv !== av) return bv - av
        return stableStringCompare(a, b)
      }
      case 'impact-desc': {
        const ar = IMPACT_RANK[a.impact]
        const br = IMPACT_RANK[b.impact]
        if (ar !== br) return ar - br
        return stableStringCompare(a, b)
      }
      case 'source-asc': {
        if (a.sourceLabel !== b.sourceLabel) {
          return a.sourceLabel.localeCompare(b.sourceLabel)
        }
        return stableStringCompare(a, b)
      }
      case 'resource-asc': {
        return stableStringCompare(a, b)
      }
      default:
        return 0
    }
  })
  return copy
}

function stableStringCompare(a: UnifiedRecommendationRow, b: UnifiedRecommendationRow): number {
  if (a.resource !== b.resource) return a.resource.localeCompare(b.resource)
  if (a.action !== b.action) return a.action.localeCompare(b.action)
  return a.id.localeCompare(b.id)
}
