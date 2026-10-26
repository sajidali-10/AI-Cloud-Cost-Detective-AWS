// Phase 7D.2 — Unified AWS + Azure Optimization page.
//
// One provider-aware Optimization experience for both clouds:
//
//   /api/cloud/recommendations?provider=AWS|AZURE&account_id=...
//
// The page reacts to the existing Cloud Context (ProviderContext +
// AccountContext) — there is no second provider/account selector
// inside Optimization.  Switching AWS → Azure → AWS or QA-Dev →
// Production → All → QA-Dev never blanks the page: the page reads
// from the canonical cache key, which already isolates
// (provider, account, region, days), and from the Cloud Context,
// which is the single source of truth for the active scope.
//
// Legacy AWS route `/api/aws/optimization/*` is preserved on the
// backend for compatibility but the Optimization UI no longer
// reads from it as its primary source — see
// `docs/phase7d2-unified-optimization-ui.md` for the rationale.

import { useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { EmptyState } from '../components/EmptyState'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { RefreshButton } from '../components/RefreshButton'
import { PartialWarning } from '../components/PartialWarning'
import { FilterBar, FilterSelect } from '../components/FilterBar'
import { ConfidenceBadge } from '../components/RecommendationSourceBadge'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { useFinopsQuery } from '../lib/finops/store'
import { invalidateFinopsCache } from '../lib/finops/store'
import { useFinopsPeriod } from '../lib/finops/period'
import { useAccountContext } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import {
  buildCloudRecommendationsCacheKey,
  fetchCloudRecommendations,
  type CloudRecommendationListResponse,
} from '../lib/finops/cloudRecommendations'
import {
  applyFilters,
  buildFacets,
  buildUnifiedEnvelope,
  DEFAULT_FILTERS,
  DEFAULT_SORT,
  friendlyAttribution,
  impactToneFor,
  sortRows,
  type AccountMapEntry,
  type FilterState,
  type SortKey,
  type UnifiedRecommendationRow,
} from '../lib/finops/unifiedRecommendations'
import {
  formatCurrencyCompact,
  formatCurrencyDecimal,
  formatLocalTime,
  parseDecimal,
} from '../lib/format'

const HEADLINE_KPI = {
  active: 'Active Recommendations',
  savings: 'Potential Monthly Savings',
  resources: 'Resources Affected',
  sources: 'Recommendation Sources',
}

const PAGE_TEST_IDS = {
  root: 'optimization-page',
  kpiActive: 'kpi-active-recommendations',
  kpiSavings: 'kpi-potential-monthly-savings',
  kpiResources: 'kpi-resources-affected',
  kpiSources: 'kpi-recommendation-sources',
  partialWarning: 'optimization-partial-warning',
  emptyState: 'optimization-empty-state',
  table: 'optimization-table',
  filterImpact: 'filter-impact',
  filterSource: 'filter-source',
  filterResourceType: 'filter-resource-type',
  filterRegion: 'filter-region',
  filterSearch: 'filter-search',
  sortSelect: 'sort-select',
  scopeLabel: 'optimization-scope-label',
  statusBadge: 'optimization-status-badge',
  noData: 'optimization-no-data',
  mixedCurrency: 'optimization-mixed-currency',
  detailPanel: 'optimization-detail-panel',
}

// ---------------------------------------------------------------------------
// Subtitle / page copy is provider-aware but identical in shape for both.
// ---------------------------------------------------------------------------

function pageSubtitle(provider: 'AWS' | 'AZURE' | 'RACKSPACE' | string): string {
  if (provider === 'AZURE') {
    return 'Identify cost-saving opportunities across your selected Azure scope'
  }
  if (provider === 'AWS') {
    return 'Identify cost-saving opportunities across your selected AWS scope'
  }
  if (provider === 'RACKSPACE') {
    return 'Identify review opportunities across your selected Rackspace scope'
  }
  return 'Identify cost-saving opportunities across your selected cloud scope'
}

// Phase 9D.1 — advisory warning source label per provider (used by
// the PartialWarning rows so a Rackspace service failure is never
// attributed to an AWS pipeline).
function advisorySourceLabel(provider: 'AWS' | 'AZURE' | 'RACKSPACE' | string): string {
  if (provider === 'AZURE') return 'azure-advisor'
  if (provider === 'RACKSPACE') return 'rackspace-optimization'
  return 'aws-optimization'
}

function scopeDisplayLabel(
  provider: 'AWS' | 'AZURE' | string,
  selection: string | null,
  accountMap: ReadonlyArray<AccountMapEntry>,
): string {
  return friendlyAttribution(selection, accountMap, provider).label
}

// ---------------------------------------------------------------------------
// Top-level page component.
// ---------------------------------------------------------------------------

export function OptimizationPage() {
  const { region, days } = useFinopsPeriod()
  const { selection, accounts } = useAccountContext()
  const { provider, providerVersion } = useProviderContext()

  // Effective region (null = no filter).  Azure ignores this but the
  // cache key still includes it for symmetry with Phase 7D.1.
  const effectiveRegion = region === 'all' ? null : region

  // Provider version is already part of the cache key through the
  // accountContext dependency chain.  We do not add a manual
  // providerVersion dep — the key builder reads the active
  // providerSlug implicitly via the bucket prefix and the
  // selection + provider slug themselves invalidate via
  // `invalidateFinopsCache()` below.

  const cacheProvider =
    provider === 'AZURE' ? 'azure' : provider === 'RACKSPACE' ? 'rackspace' : 'aws'

  // Build the accountMap for friendly attribution only (not sent
  // to the backend).  Each entry is the provider's *canonical*
  // selector string, mirroring what the AccountSelector emits:
  //   * AWS  → acct:<local-row-id>
  //   * Azure → sub:<guid>
  // We also store the raw account / subscription id so the lookup
  // can resolve the recommendation row's `account_id` field, which
  // carries the raw id (e.g. AWS account id `974053642038` or
  // Azure subscription GUID) — not the canonical selector.
  const accountMap: AccountMapEntry[] = useMemo(
    () =>
      accounts.map((a) => ({
        selector:
          provider === 'AZURE' ? `sub:${a.account_id}` : `acct:${a.id}`,
        rawAccountId: a.account_id,
        displayName: a.display_name,
        provider: provider,
      })),
    [accounts, provider],
  )

  // Provider / scope safety net: on a real provider or selection
  // change, drop the previous response so a stale AWS row can
  // never satisfy an Azure request (and vice versa).  Mirrors the
  // bea38fd provider-switch fix; expanded to also fire on the
  // selection change so a stale account's row cannot leak across.
  const prevSelectionRef = useRef(selection)
  const prevProviderRef = useRef(cacheProvider)
  useEffect(() => {
    if (
      prevSelectionRef.current !== selection ||
      prevProviderRef.current !== cacheProvider
    ) {
      invalidateFinopsCache()
      prevSelectionRef.current = selection
      prevProviderRef.current = cacheProvider
    }
  }, [selection, cacheProvider, providerVersion])

  const cacheKey = buildCloudRecommendationsCacheKey(
    selection,
    cacheProvider,
    effectiveRegion,
    days,
  )

  const query = useFinopsQuery<CloudRecommendationListResponse>(
    cacheKey,
    () =>
      fetchCloudRecommendations({
        provider: provider,
        accountId: selection,
        region: effectiveRegion,
        days,
      }),
    { ttlMs: 60_000 },
  )

  const response = query.entry.data
  const loading = query.entry.status === 'loading'
  const error = query.entry.error

  const envelope = useMemo(() => {
    if (!response) return null
    return buildUnifiedEnvelope({
      response,
      provider,
      selection,
      accountMap,
      savingsPeriod: response.summary?.savings_period ?? null,
    })
  }, [response, provider, selection, accountMap])

  // ------------------------------------------------------------------
  // Filter / sort state — client-side, deterministic, no Date.now.
  // ------------------------------------------------------------------

  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS)
  const [sortKey, setSortKey] = useState<SortKey>(DEFAULT_SORT)

  // Phase 9D.1 — selected recommendation row for the advisory
  // detail panel.  Clicking a table row opens (or closes) its
  // detail; narrowing the filters to exactly one row still shows
  // that row's detail without a click (Phase 7D.2 behavior).
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null)

  // Reset filters whenever the envelope shape changes (provider /
  // scope switch).  This prevents an Azure-only filter selection
  // (e.g. resource type "Microsoft.Compute/virtualMachines") from
  // lingering after switching to AWS where it would hide every
  // row.  A provider / scope switch also clears the selected
  // detail row so a stale selection cannot survive the switch.
  const envelopeIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (!envelope) return
    const id = `${envelope.provider}:${envelope.scope}:${envelope.rows.length}`
    if (envelopeIdRef.current !== id) {
      envelopeIdRef.current = id
      setFilters(DEFAULT_FILTERS)
      setSortKey(DEFAULT_SORT)
      setSelectedRowId(null)
    }
  }, [envelope])

  const facets = useMemo(
    () => buildFacets(envelope?.rows ?? []),
    [envelope?.rows],
  )
  const filteredRows = useMemo(
    () => (envelope ? sortRows(applyFilters(envelope.rows, filters), sortKey) : []),
    [envelope, filters, sortKey],
  )

  // Phase 9D.1 — the advisory detail row: the clicked row, or the
  // single remaining row after filtering (Phase 7D.2 behavior).
  const detailRow = useMemo(
    () =>
      envelope
        ? filteredRows.find((r) => r.id === selectedRowId) ??
            (filteredRows.length === 1 ? filteredRows[0] : null)
        : null,
    [envelope, filteredRows, selectedRowId],
  )

  const selectedScopeLabel = scopeDisplayLabel(provider, selection, accountMap)
  const summary = envelope?.kpis

  const refresh = () => query.refresh()

  return (
    <div className="space-y-5" data-testid={PAGE_TEST_IDS.root}>
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Optimization"
        subtitle={pageSubtitle(provider)}
        context={
          <>
            <span className="text-fg-muted">{provider === 'AZURE' ? 'Subscription' : 'Account'}</span>
            <span className="text-fg-secondary" data-testid={PAGE_TEST_IDS.scopeLabel}>
              {selectedScopeLabel}
            </span>
            <span className="text-fg-muted">Status</span>
            <StatusBadge
              tone={statusTone(envelope?.status ?? (loading ? 'LOADING' : 'IDLE'))}
              data-testid={PAGE_TEST_IDS.statusBadge}
            >
              {envelope?.status ?? (loading ? 'Loading' : 'Idle')}
            </StatusBadge>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">
              {formatLocalTime(query.entry.refreshedAt)}
            </span>
          </>
        }
        actions={
          <div className="flex items-center gap-2">
            <RefreshButton onClick={refresh} />
          </div>
        }
      />

      {error && !envelope && (
        <ErrorState
          title="Could not load optimization recommendations"
          message={error.message}
          onRetry={refresh}
        />
      )}

      {envelope && envelope.status === 'PARTIAL_SUCCESS' && envelope.partialErrors.length > 0 && (
        <div data-testid={PAGE_TEST_IDS.partialWarning}>
          <PartialWarning
            title="Partial data — some scopes were unavailable"
            warnings={envelope.partialErrors.map((e) => ({
              source: advisorySourceLabel(provider),
              code: e.code,
              message: `${e.display_name}: ${e.message}`,
              region: null,
            }))}
          />
        </div>
      )}

      {/* KPI grid — provider-neutral copy; never uses `acct:`/`sub:` */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div data-testid={PAGE_TEST_IDS.kpiActive}>
          <MetricCard
            label={HEADLINE_KPI.active}
            value={(summary?.activeRecommendations ?? 0).toLocaleString('en-US')}
            description={
              envelope
                ? `Status: ${envelope.status}`
                : loading
                  ? 'Loading…'
                  : '—'
            }
          />
        </div>
        <div data-testid={PAGE_TEST_IDS.kpiSavings}>
          <MetricCard
            label={HEADLINE_KPI.savings}
            value={renderHeadlineSavings(summary, loading)}
            description={savingsDescription(summary)}
          />
        </div>
        <div data-testid={PAGE_TEST_IDS.kpiResources}>
          <MetricCard
            label={HEADLINE_KPI.resources}
            value={(summary?.resourcesAffected ?? 0).toLocaleString('en-US')}
            description={
              envelope ? 'Distinct resources in scope' : loading ? 'Loading…' : '—'
            }
          />
        </div>
        <div data-testid={PAGE_TEST_IDS.kpiSources}>
          <MetricCard
            label={HEADLINE_KPI.sources}
            value={
              summary && summary.recommendationSources.length > 0
                ? summary.recommendationSources.length.toLocaleString('en-US')
                : '0'
            }
            description={
              summary && summary.recommendationSources.length > 0
                ? summary.recommendationSources.map((s) => s.label).join(' · ')
                : 'No source reported'
            }
          />
        </div>
      </div>

      {/* Mixed-currency disclosure — never fabricate a single total */}
      {summary?.savingsStatus === 'MIXED_CURRENCY' && (
        <div
          className="rounded-md border border-border bg-surface-2 px-3 py-2 text-xs text-fg-secondary"
          data-testid={PAGE_TEST_IDS.mixedCurrency}
        >
          <div className="font-medium text-fg-primary">Mixed currencies</div>
          <div className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-2">
            {summary.savingsByCurrency.map((entry) => (
              <div key={entry.currency} className="flex items-center justify-between gap-2">
                <span className="text-fg-muted">{entry.currency}</span>
                <span className="tabular-nums text-fg-primary">
                  {entry.currency} {entry.amount.toLocaleString('en-US', {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2,
                  })}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-1 text-[10px] text-fg-muted">
            We did not invent an FX rate — totals are shown per currency.
          </div>
        </div>
      )}

      {/* Breakdown chart row */}
      {envelope && envelope.rows.length > 0 && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <SectionCard
            title={provider === 'AZURE' ? 'By subscription' : 'By account'}
            description={
              provider === 'AZURE'
                ? 'Per Azure subscription attribution'
                : provider === 'RACKSPACE'
                  ? 'Per Rackspace account attribution'
                  : 'Per AWS account attribution'
            }
          >
            <BreakdownRows rows={accountBreakdown(envelope, accountMap)} />
          </SectionCard>
          <SectionCard title="By impact" description="Severity distribution">
            <BreakdownRows rows={impactBreakdown(summary)} />
          </SectionCard>
          <SectionCard title="By resource type" description="Most-affected resource types">
            <BreakdownRows rows={resourceTypeBreakdown(envelope.rows)} />
          </SectionCard>
        </div>
      )}

      {/* Filters */}
      <SectionCard
        title="Filters"
        description="Refine by impact, source, resource type, region, or search"
      >
        <FilterBar>
          <FilterSelect
            label="Impact"
            value={filters.impact}
            onChange={(v) =>
              setFilters((s) => ({ ...s, impact: v as FilterState['impact'] }))
            }
            options={[
              { value: 'all', label: 'All' },
              ...facets.impacts.map((entry) => ({
                value: entry.value,
                label: `${entry.label} (${entry.count})`,
              })),
            ]}
            testId={PAGE_TEST_IDS.filterImpact}
          />
          <FilterSelect
            label="Source"
            value={filters.source}
            onChange={(v) => setFilters((s) => ({ ...s, source: v }))}
            options={[
              { value: 'all', label: 'All' },
              ...facets.sources.map((entry) => ({
                value: entry.value,
                label: `${entry.label} (${entry.count})`,
              })),
            ]}
            testId={PAGE_TEST_IDS.filterSource}
          />
          <FilterSelect
            label="Resource type"
            value={filters.resourceType}
            onChange={(v) => setFilters((s) => ({ ...s, resourceType: v }))}
            options={[
              { value: 'all', label: 'All' },
              ...facets.resourceTypes.map((entry) => ({
                value: entry.value,
                label: `${entry.label} (${entry.count})`,
              })),
            ]}
            testId={PAGE_TEST_IDS.filterResourceType}
          />
          <FilterSelect
            label="Region"
            value={filters.region}
            onChange={(v) => setFilters((s) => ({ ...s, region: v }))}
            options={[
              { value: 'all', label: 'All' },
              ...facets.regions.map((entry) => ({
                value: entry.value,
                label: `${entry.label} (${entry.count})`,
              })),
            ]}
            testId={PAGE_TEST_IDS.filterRegion}
          />
          <SortSelect value={sortKey} onChange={setSortKey} testId={PAGE_TEST_IDS.sortSelect} />
          <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
            <span>Search</span>
            <input
              type="search"
              placeholder="Resource, action, description"
              value={filters.search}
              onChange={(e) => setFilters((s) => ({ ...s, search: e.target.value }))}
              data-testid={PAGE_TEST_IDS.filterSearch}
              className="w-56 rounded-md border border-border bg-surface px-2 py-1 text-xs text-fg-primary focus:outline-none focus-visible:shadow-focus"
            />
          </label>
        </FilterBar>
      </SectionCard>

      {/* Recommendations table */}
      <SectionCard
        title="Recommendations"
        description={
          envelope
            ? `${filteredRows.length.toLocaleString('en-US')} of ${envelope.rows.length.toLocaleString('en-US')} shown`
            : loading
              ? 'Loading…'
              : 'No data'
        }
      >
        {loading && !envelope ? (
          <LoadingSkeletonRows rows={6} />
        ) : envelope && envelope.rows.length === 0 ? (
          <div data-testid={PAGE_TEST_IDS.noData}>
            <EmptyState
              title="No active optimization recommendations were returned for this scope."
              description="This is a valid empty state — the upstream returned zero rows for the active provider and scope."
            />
          </div>
        ) : envelope && filteredRows.length === 0 ? (
          <EmptyState
            title="No recommendations match these filters"
            description="Adjust or clear the active filters to see more results."
          />
        ) : (
          envelope && (
            <div data-testid={PAGE_TEST_IDS.table}>
              <DataTable<UnifiedRecommendationRow>
                columns={tableColumns}
                rows={filteredRows}
                rowKey={(r) => r.id}
                caption={`${provider} optimization recommendations`}
                onRowClick={(r) =>
                  setSelectedRowId((current) => (current === r.id ? null : r.id))
                }
                isRowClickable={() => true}
              />
            </div>
          )
        )}
      </SectionCard>

      {/* Detail panel — read-only, no remediation actions.  Opens
          for the clicked row, or automatically when the filters
          narrow the list to exactly one row. */}
      {envelope && detailRow && (
        <DetailPanel row={detailRow} provider={provider} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Detail panel
// ---------------------------------------------------------------------------

function DetailPanel({
  row,
  provider,
}: {
  row: UnifiedRecommendationRow
  provider: 'AWS' | 'AZURE' | 'RACKSPACE' | string
}) {
  // Phase 9D.1 — canonical advisory fields ride in the provider
  // neutral ``extensions`` (Phase 9C.2 emits them for Rackspace).
  // They are rendered verbatim — the frontend never reinterprets
  // recommendation semantics, never upgrades advisory wording to
  // deletion language, and never invents a savings figure.
  const ext = (row.raw?.extensions ?? {}) as Record<string, unknown>
  const confidence = extString(ext, 'confidence')
  const recommendedAction = extString(ext, 'recommended_action')
  const evidence = extEvidenceList(ext)
  const limitations = extStringList(ext, 'limitations')
  return (
    <div data-testid={PAGE_TEST_IDS.detailPanel}>
    <SectionCard
      title="Recommendation detail"
      description={`${row.resourceTypeShort} · ${row.region ?? '—'} · ${row.attribution.label}`}
    >
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold text-fg-primary">Finding</h3>
          <p className="mt-1 text-xs text-fg-secondary">
            {row.description || row.title}
          </p>
          <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">
            Impact
          </p>
          <p className="mt-0.5 text-xs text-fg-primary">
            <StatusBadge tone={impactToneFor(row.impact)}>{row.impact}</StatusBadge>
            {confidence !== null && (
              <span className="ml-2" data-testid="recommendation-confidence">
                <ConfidenceBadge confidence={confidence} />
              </span>
            )}
          </p>
          <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">
            Source
          </p>
          <p className="mt-0.5 text-xs text-fg-primary">{row.sourceLabel}</p>
          <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">
            Provider
          </p>
          <p className="mt-0.5 text-xs text-fg-primary">{provider}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-fg-primary">Savings</h3>
          <p className="mt-1 text-xs text-fg-secondary" data-testid="recommendation-savings-detail">
            {row.estimatedMonthlySavings === null
              ? 'Not available'
              : `${formatCurrencyDecimal(row.estimatedMonthlySavings, row.currency ?? 'USD')} / mo`}
            {row.monthlyNormalized && row.estimatedAnnualSavings !== null
              ? ` (annual: ${formatCurrencyDecimal(row.estimatedAnnualSavings, row.currency ?? 'USD')})`
              : ''}
          </p>
          {row.estimatedMonthlySavings === null && (
            <p className="mt-1 text-[10px] text-fg-muted">
              Unknown savings are not reported as zero — no authoritative figure
              exists for this finding.
            </p>
          )}
          <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">
            Resource
          </p>
          <p className="mt-0.5 break-all text-xs font-mono text-fg-primary">
            {row.resourceId}
          </p>
          <p className="mt-3 text-[10px] uppercase tracking-wider text-fg-muted">
            Action
          </p>
          <p className="mt-0.5 text-xs text-fg-primary">{row.action}</p>
        </div>
      </div>

      {/* Phase 9D.1 — canonical advisory extension fields.  Rendered
          verbatim when present; absent for providers that do not
          emit them.  No frontend reinterpretation of semantics. */}
      {recommendedAction !== null && (
        <div className="mt-4" data-testid="recommendation-recommended-action">
          <p className="text-[10px] uppercase tracking-wider text-fg-muted">
            Recommended action
          </p>
          <p className="mt-0.5 text-xs text-fg-secondary">{recommendedAction}</p>
        </div>
      )}

      {evidence.length > 0 && (
        <div className="mt-4" data-testid="recommendation-evidence">
          <p className="text-[10px] uppercase tracking-wider text-fg-muted">
            Evidence
          </p>
          <dl className="mt-1 grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
            {evidence.map((item, index) => (
              <div key={`${item.kind}-${index}`} className="flex items-baseline justify-between gap-2">
                <dt className="font-mono text-[10px] text-fg-muted">{item.kind}</dt>
                <dd className="truncate text-xs text-fg-secondary" title={String(item.value)}>
                  {item.value === null || item.value === undefined
                    ? '—'
                    : String(item.value)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {limitations.length > 0 && (
        <div className="mt-4" data-testid="recommendation-limitations">
          <p className="text-[10px] uppercase tracking-wider text-fg-muted">
            Limitations
          </p>
          <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs text-fg-muted">
            {limitations.map((limitation, index) => (
              <li key={`${limitation}-${index}`}>{limitation}</li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-4 text-[10px] uppercase tracking-wider text-fg-muted">
        Read-only — no remediation actions are surfaced here. Advisory findings
        require owner validation before any change.
      </p>
    </SectionCard>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Phase 9D.1 — canonical advisory extension accessors
// ---------------------------------------------------------------------------

function extString(
  ext: Record<string, unknown> | undefined,
  key: string,
): string | null {
  if (!ext) return null
  const value = ext[key]
  return typeof value === 'string' && value.trim() ? value : null
}

interface AdvisoryEvidenceItem {
  kind: string
  value: unknown
}

function extEvidenceList(
  ext: Record<string, unknown> | undefined,
): AdvisoryEvidenceItem[] {
  if (!ext || !Array.isArray(ext['evidence'])) return []
  const out: AdvisoryEvidenceItem[] = []
  for (const item of ext['evidence']) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    if (typeof record['kind'] !== 'string' || !record['kind']) continue
    out.push({ kind: record['kind'], value: record['value'] ?? null })
  }
  return out
}

function extStringList(
  ext: Record<string, unknown> | undefined,
  key: string,
): string[] {
  if (!ext || !Array.isArray(ext[key])) return []
  return (ext[key] as unknown[]).filter(
    (v): v is string => typeof v === 'string' && v.trim().length > 0,
  )
}

// ---------------------------------------------------------------------------
// Subcomponents / helpers
// ---------------------------------------------------------------------------

function SortSelect({
  value,
  onChange,
  testId,
}: {
  value: SortKey
  onChange: (next: SortKey) => void
  testId?: string
}) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
      <span>Sort</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as SortKey)}
        data-testid={testId}
        className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-fg-primary focus:outline-none focus-visible:shadow-focus"
      >
        <option value="savings-desc">Highest savings</option>
        <option value="impact-desc">Highest impact</option>
        <option value="source-asc">Source</option>
        <option value="resource-asc">Resource</option>
      </select>
    </label>
  )
}

function statusTone(status: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  if (status === 'SUCCESS') return 'success'
  if (status === 'PARTIAL_SUCCESS') return 'warning'
  if (status === 'NO_DATA') return 'neutral'
  if (status === 'ERROR') return 'danger'
  if (status === 'THROTTLED') return 'warning'
  if (status === 'ACCESS_DENIED') return 'danger'
  if (status === 'LOADING') return 'info'
  return 'neutral'
}

function renderHeadlineSavings(
  summary: ReturnType<typeof buildUnifiedEnvelope>['kpis'] | undefined,
  loading: boolean,
): React.ReactNode {
  if (loading && !summary) return '—'
  if (!summary) return '—'
  if (summary.potentialMonthlySavings === null) {
    if (summary.savingsStatus === 'MIXED_CURRENCY') return 'Mixed currencies'
    return 'Not available'
  }
  return formatCurrencyCompact(summary.potentialMonthlySavings, summary.currency ?? 'USD')
}

function savingsDescription(
  summary: ReturnType<typeof buildUnifiedEnvelope>['kpis'] | undefined,
): string {
  if (!summary) return '—'
  if (summary.savingsStatus === 'MIXED_CURRENCY') {
    return 'Per-currency totals — no FX conversion'
  }
  if (summary.savingsStatus === 'EMPTY' || summary.savingsUnavailable) {
    return 'No authoritative savings figure'
  }
  if (summary.authoritativeZero) {
    return 'Authoritative zero'
  }
  return `Currency: ${summary.currency ?? '—'} · monthly`
}

function accountBreakdown(
  envelope: NonNullable<ReturnType<typeof buildUnifiedEnvelope>>,
  accountMap: ReadonlyArray<AccountMapEntry>,
): HorizontalBarRow[] {
  const buckets = new Map<string, { label: string; count: number }>()
  for (const row of envelope.rows) {
    const label = row.attribution.label
    const prev = buckets.get(label) ?? { label, count: 0 }
    prev.count += 1
    buckets.set(label, prev)
  }
  // Ensure the "All" aggregate label shows even when there are zero rows
  if (buckets.size === 0) {
    const label =
      envelope.provider === 'AZURE'
        ? 'All Azure Subscriptions'
        : envelope.provider === 'RACKSPACE'
          ? 'All Rackspace Cloud Accounts'
          : 'All AWS Accounts'
    return [{ key: label, label, title: label, value: '0' }]
  }
  return Array.from(buckets.entries())
    .sort((a, b) => b[1].count - a[1].count)
    .map(([key, entry]) => ({ key, label: entry.label, title: entry.label, value: String(entry.count) }))
  // accountMap reserved for tests / future lookup; safe to keep ref
  // here without removing it because the linter otherwise flags it.
  void accountMap
}

function impactBreakdown(
  summary: ReturnType<typeof buildUnifiedEnvelope>['kpis'] | undefined,
): HorizontalBarRow[] {
  if (!summary) return []
  const entries = [
    { label: 'High', count: summary.highImpact },
    { label: 'Medium', count: summary.mediumImpact },
    { label: 'Low', count: summary.lowImpact },
  ]
  return entries.map((e) => ({
    key: e.label,
    label: e.label,
    title: e.label,
    value: String(e.count),
  }))
}

function resourceTypeBreakdown(
  rows: ReturnType<typeof buildUnifiedEnvelope>['rows'],
): HorizontalBarRow[] {
  const buckets = new Map<string, number>()
  for (const row of rows) {
    buckets.set(row.resourceTypeShort, (buckets.get(row.resourceTypeShort) ?? 0) + 1)
  }
  return Array.from(buckets.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([label, count]) => ({
      key: label,
      label,
      title: label,
      value: String(count),
    }))
}

function BreakdownRows({ rows }: { rows: HorizontalBarRow[] }) {
  if (rows.length === 0) {
    return <EmptyState title="No breakdown" />
  }
  return <HorizontalBarChart rows={rows} currency="" maxRows={6} showShare safeShare />
}

const tableColumns: DataTableColumn<UnifiedRecommendationRow>[] = [
  {
    key: 'title',
    header: 'Finding / Action',
    cell: (r) => (
      <div className="space-y-0.5">
        <div className="truncate text-fg-primary" title={r.title}>
          {r.title}
        </div>
        <div className="text-[10px] text-fg-muted">{r.action}</div>
      </div>
    ),
  },
  {
    key: 'impact',
    header: 'Impact',
    cell: (r) => (
      <StatusBadge tone={impactToneFor(r.impact)}>{r.impact}</StatusBadge>
    ),
    width: 'w-24',
  },
  {
    key: 'resource',
    header: 'Resource',
    cell: (r) => (
      <span className="truncate font-mono text-[11px] text-fg-primary" title={r.resourceId}>
        {r.resource}
      </span>
    ),
  },
  {
    key: 'resourceType',
    header: 'Type',
    cell: (r) => (
      <span className="inline-flex items-center rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-fg-secondary">
        {r.resourceTypeShort}
      </span>
    ),
    width: 'w-32',
  },
  {
    key: 'region',
    header: 'Region',
    cell: (r) => r.region ?? '—',
    width: 'w-32',
  },
  {
    key: 'attribution',
    header: 'Scope',
    cell: (r) => (
      <span className="truncate text-fg-secondary" title={r.attribution.label}>
        {r.attribution.label}
      </span>
    ),
  },
  {
    key: 'source',
    header: 'Source',
    cell: (r) => (
      <span className="text-xs text-fg-secondary">{r.sourceLabel}</span>
    ),
    width: 'w-40',
  },
  {
    key: 'savings',
    header: 'Est. monthly',
    numeric: true,
    cell: (r) => savingsCell(r),
    width: 'w-36',
  },
]

function savingsCell(r: UnifiedRecommendationRow): React.ReactNode {
  if (r.estimatedMonthlySavings === null) {
    return (
      <span className="text-xs text-fg-muted" data-testid="savings-not-available">
        Not available
      </span>
    )
  }
  if (r.estimatedMonthlySavings === 0) {
    return <span className="text-xs text-fg-secondary">$0.00</span>
  }
  return (
    <span className="text-xs text-fg-primary" title={r.monthlyNormalized ? 'Normalised from annual figure' : undefined}>
      {formatCurrencyDecimal(r.estimatedMonthlySavings, r.currency ?? 'USD')}
      <span className="ml-1 text-fg-muted">/ mo</span>
    </span>
  )
}

// Internal helper used by tests to assert that the page consumes the
// canonical envelope shape (and not the legacy AWS-only types).
export const __phase7d2 = {
  pageTestIds: PAGE_TEST_IDS,
  parseDecimal,
}

export default OptimizationPage
