// Phase 7C.2C.1 — Azure inventory view.
//
// Renders Azure Resource Graph data from the canonical
// ``/api/cloud/resources`` endpoint.  The component reuses the
// same shared building blocks as the AWS inventory view
// (LoadingSkeleton, ErrorState, PartialWarning, PageHeader,
// SectionCard) so the visual rhythm matches the rest of the
// platform.
//
// Differences from the AWS view:
//
// * Data comes from ``fetchCloudResources`` rather than
//   ``fetchResources`` so the response shape is the canonical
//   provider-neutral envelope (Phase 7C.2C).
// * The summary breakdown is provider-agnostic:
//   by_type / by_location / by_resource_group / by_subscription.
// * The resource table renders the normalised ``CloudResourceItem``
//   fields directly — no AWS-specific TypeScript types are touched.
// * Utilization sub-panels are intentionally absent — CloudWatch
//   metrics are an AWS surface.  A future Azure Monitor integration
//   can plug into the same panel contract.

import { useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { PartialWarning } from '../components/PartialWarning'
import { FilterBar, FilterSelect } from '../components/FilterBar'
import { RefreshButton } from '../components/RefreshButton'
import { HorizontalBarChart, type HorizontalBarRow } from '../components/HorizontalBarChart'
import { useFinopsQuery } from '../lib/finops/store'
import { invalidateFinopsCache } from '../lib/finops/store'
import {
  fetchCloudResources,
  buildCloudResourcesCacheKey,
  type CloudResourceItem,
  type CloudResourceListResponse,
} from '../lib/finops/cloudResources'
import { useAccountContext } from '../lib/accountContext'
import { useProviderContext } from '../lib/providerContext'
import {
  formatLocalTime,
  maskAccountId,
  parseDecimal,
} from '../lib/format'

interface AzureResourcesViewProps {
  selection: string | null
  cacheProvider: 'aws' | 'azure' | 'rackspace'
  providerVersion: number
}

function maskResourceId(id: string): string {
  // ARM ids are long (e.g. ``/subscriptions/<guid>/resourceGroups/<rg>/providers/<type>/<name>``).
  // Mask the middle but keep the trailing provider/name so the user
  // can still recognise the resource in a list.
  if (id.length <= 24) return id
  return `${id.slice(0, 18)}…${id.slice(-8)}`
}

function resourceGroupFromMetadata(metadata: Record<string, unknown> | undefined): string {
  if (!metadata) return '—'
  const value = metadata['resource_group']
  return typeof value === 'string' && value ? value : '—'
}

function subscriptionFromItem(item: CloudResourceItem): string {
  // The canonical envelope exposes ``account_id`` which carries the
  // subscription GUID for Azure rows.
  return item.account_id || '—'
}

function statusTone(status: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  if (status === 'SUCCESS') return 'success'
  if (status === 'PARTIAL_SUCCESS') return 'warning'
  if (status === 'NO_DATA') return 'neutral'
  if (status === 'ERROR') return 'danger'
  return 'info'
}

export function AzureResourcesView({
  selection,
  cacheProvider,
  providerVersion,
}: AzureResourcesViewProps) {
  const [kindFilter, setKindFilter] = useState<string>('all')
  const [regionFilter, setRegionFilter] = useState<string>('all')
  const [search, setSearch] = useState<string>('')

  // Phase 7B / 7C.1: invalidate stale data on provider / account
  // change.  The canonical cache key already includes the provider
  // segment, so AWS and Azure cannot collide.
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

  const resources = useFinopsQuery<CloudResourceListResponse>(
    buildCloudResourcesCacheKey(selection, cacheProvider),
    () => fetchCloudResources({ provider: 'AZURE', accountId: selection }),
  )

  const data = resources.entry.data
  const summary = data?.summary
  const items = data?.resources ?? []
  const loading = resources.entry.status === 'loading'
  const error = resources.entry.error

  // Type / location options derived from the normalised resources so
  // the filter bar always reflects what the API actually returned.
  const kindOptions = useMemo(() => {
    const seen = new Set<string>()
    for (const r of items) seen.add(r.resource_type)
    const out: { value: string; label: string }[] = [{ value: 'all', label: 'All types' }]
    for (const t of Array.from(seen).sort()) out.push({ value: t, label: t.split('/').pop() ?? t })
    return out
  }, [items])

  const regionOptions = useMemo(() => {
    const seen = new Set<string>()
    for (const r of items) if (r.region) seen.add(r.region)
    const out: { value: string; label: string }[] = [{ value: 'all', label: 'All regions' }]
    for (const r of Array.from(seen).sort()) out.push({ value: r, label: r })
    return out
  }, [items])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((item) => {
      if (kindFilter !== 'all' && item.resource_type !== kindFilter) return false
      if (regionFilter !== 'all' && item.region !== regionFilter) return false
      if (!q) return true
      return (
        (item.name ?? '').toLowerCase().includes(q) ||
        item.resource_id.toLowerCase().includes(q) ||
        subscriptionFromItem(item).toLowerCase().includes(q) ||
        resourceGroupFromMetadata(item.metadata).toLowerCase().includes(q)
      )
    })
  }, [items, kindFilter, regionFilter, search])

  const byTypeRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_type ?? []).slice(0, 10).map((entry) => ({
        key: entry.key,
        label: entry.key.split('/').pop() ?? entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_type],
  )

  const byRegionRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_location ?? []).slice(0, 10).map((entry) => ({
        key: entry.key,
        label: entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_location],
  )

  const byResourceGroupRows: HorizontalBarRow[] = useMemo(
    () =>
      (summary?.by_resource_group ?? []).slice(0, 10).map((entry) => ({
        key: entry.key,
        label: entry.key,
        title: entry.key,
        value: String(entry.count),
      })),
    [summary?.by_resource_group],
  )

  const distinctTypes = summary?.by_type.length ?? 0
  const distinctLocations = summary?.by_location.length ?? 0
  const distinctResourceGroups = summary?.by_resource_group.length ?? 0
  const totalResources = summary?.total_resources ?? 0

  const tableColumns: DataTableColumn<CloudResourceItem>[] = [
    {
      key: 'name',
      header: 'Name',
      cell: (r) => (
        <span title={r.resource_id} className="truncate">
          {r.name || r.resource_id.split('/').pop() || r.resource_id}
        </span>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      cell: (r) => (
        <span className="truncate" title={r.resource_type}>
          {r.resource_type.split('/').pop() ?? r.resource_type}
        </span>
      ),
    },
    {
      key: 'location',
      header: 'Location',
      cell: (r) => r.region ?? '—',
    },
    {
      key: 'resource_group',
      header: 'Resource Group',
      cell: (r) => resourceGroupFromMetadata(r.metadata),
    },
    {
      key: 'subscription',
      header: 'Subscription',
      cell: (r) => maskAccountId(subscriptionFromItem(r)),
    },
    {
      key: 'provider',
      header: 'Provider',
      cell: () => 'Azure',
    },
  ]

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Resources"
        subtitle="Azure inventory via Microsoft Resource Graph"
        context={
          <>
            <span className="text-fg-muted">Subscription</span>
            <span className="text-fg-secondary">
              {selection === 'all' || selection === null
                ? 'All Azure Subscriptions'
                : maskAccountId(selection)}
            </span>
            <span className="text-fg-muted">Status</span>
            <span className="text-fg-secondary">
              <StatusBadge tone={statusTone(summary?.status ?? 'SUCCESS')}>
                {summary?.status ?? '—'}
              </StatusBadge>
            </span>
            <span className="text-fg-muted">Total</span>
            <span className="text-fg-secondary">{totalResources.toLocaleString('en-US')}</span>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">{formatLocalTime(resources.entry.refreshedAt)}</span>
          </>
        }
        actions={
          <RefreshButton onClick={resources.refresh} />
        }
      />

      {error && !data && (
        <ErrorState
          title="Could not load Azure resources"
          message={error.message}
          onRetry={resources.refresh}
        />
      )}

      {(summary?.subscription_errors ?? []).length > 0 && (
        <PartialWarning
          warnings={(summary?.subscription_errors ?? []).map((e) => ({
            source: 'azure',
            code: e.code,
            message: `${e.display_name}: ${e.message}`,
            region: null,
          }))}
          title="Some Azure subscriptions failed to enumerate"
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard label="Total Resources" value={totalResources.toLocaleString('en-US')} description={loading ? 'Loading…' : 'Discovered via Resource Graph'} />
        <MetricCard label="Resource Types" value={distinctTypes.toLocaleString('en-US')} description="Distinct type identifiers" />
        <MetricCard label="Locations" value={distinctLocations.toLocaleString('en-US')} description="Distinct Azure regions" />
        <MetricCard label="Resource Groups" value={distinctResourceGroups.toLocaleString('en-US')} description="Distinct resource groups" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <SectionCard title="Resources by Type" description="Top types by count">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={byTypeRows} currency="" maxRows={8} showShare safeShare />
          )}
        </SectionCard>
        <SectionCard title="Resources by Location" description="Regional distribution">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={byRegionRows} currency="" maxRows={8} showShare safeShare />
          )}
        </SectionCard>
        <SectionCard title="Resources by Resource Group" description="Resource group distribution">
          {loading && !data ? (
            <LoadingSkeletonRows rows={4} />
          ) : (
            <HorizontalBarChart rows={byResourceGroupRows} currency="" maxRows={8} showShare safeShare />
          )}
        </SectionCard>
      </div>

      <SectionCard
        title="Azure Resources"
        description={`${totalResources.toLocaleString('en-US')} total · ${filtered.length.toLocaleString('en-US')} shown`}
      >
        <FilterBar>
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name, id, resource group, subscription"
            aria-label="Search Azure resources"
            className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-fg-primary placeholder:text-fg-muted focus:outline-none focus-visible:shadow-focus"
            data-testid="azure-resources-search"
          />
          <FilterSelect
            label="Type"
            value={kindFilter}
            onChange={setKindFilter}
            options={kindOptions}
          />
          <FilterSelect
            label="Location"
            value={regionFilter}
            onChange={setRegionFilter}
            options={regionOptions}
          />
        </FilterBar>
        {loading && !data ? (
          <LoadingSkeleton className="h-32 w-full" />
        ) : (
          <DataTable
            columns={tableColumns}
            rows={filtered}
            rowKey={(r) => r.composite_id || r.resource_id}
            caption="Azure Resource Graph inventory"
          />
        )}
      </SectionCard>

      {/* Subscription attribution panel — visible whenever more than
          one subscription contributed to the result. */}
      {(summary?.by_subscription?.length ?? 0) > 1 && (
        <SectionCard
          title="Resources by Subscription"
          description="Per-subscription attribution"
        >
          <ul className="space-y-1 text-xs">
            {(summary?.by_subscription ?? []).map((entry) => (
              <li
                key={entry.subscription_id}
                className="flex items-center justify-between gap-3"
                data-testid="azure-by-subscription-row"
              >
                <span className="text-fg-primary">{entry.display_name}</span>
                <span className="text-fg-muted">{maskAccountId(entry.subscription_id)}</span>
                <span className="tabular-nums text-fg-secondary">{entry.count.toLocaleString('en-US')}</span>
              </li>
            ))}
          </ul>
        </SectionCard>
      )}
    </div>
  )
}
