// Phase 9D.1 — Rackspace inventory view.
//
// Renders canonical Rackspace resource data from the
// ``/api/cloud/resources?provider=RACKSPACE`` endpoint.  The
// component reuses the same shared building blocks as the AWS and
// Azure inventory views (LoadingSkeleton, ErrorState,
// PartialWarning, PageHeader, SectionCard, MetricCard, DataTable,
// StatusBadge) so the visual rhythm matches the rest of the
// platform.
//
// Differences from the AWS / Azure views:
//
// * Data comes from ``fetchCloudResources`` with
//   ``provider: 'RACKSPACE'`` — the same canonical envelope, never
//   a direct Rackspace product API call from the browser.
// * The summary cards consume the canonical ``by_type`` breakdown
//   (SERVER / VOLUME / LOAD_BALANCER) — counts are never derived
//   from unrelated data and never fabricated.
// * Three product sections render the normalized 9B.2 fields:
//   Cloud Servers (flavor / vCPU / RAM / disk / created), Cloud
//   Block Storage (attachment state, size, type, created) and
//   Cloud Load Balancers.
//
// Safety semantics (Phase 9D.1 contract):
//
// * ACTIVE is the normal healthy server status — no utilization,
//   rightsizing or "oversized" inference is performed here.
// * DETACHED is evidence for review only — it is NOT "safe to
//   delete".  The UI renders the state without any Delete /
//   Detach / Cleanup actions.
// * A zero-row load balancer result is a SUCCESSFUL empty state,
//   never an error.
// * When a service could not be inventoried (PARTIAL_SUCCESS),
//   its card shows "—" instead of a fabricated zero and its
//   section renders a professional unavailable state.
// * Read-only / advisory UI only — no management actions.

import { useEffect, useMemo, useRef, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { EmptyState } from '../components/EmptyState'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { PartialWarning } from '../components/PartialWarning'
import { FilterBar } from '../components/FilterBar'
import { RefreshButton } from '../components/RefreshButton'
import { useFinopsQuery } from '../lib/finops/store'
import { invalidateFinopsCache } from '../lib/finops/store'
import {
  fetchCloudResources,
  buildCloudResourcesCacheKey,
  type CloudResourceItem,
  type CloudResourceListResponse,
} from '../lib/finops/cloudResources'
import { maskAccountId } from '../lib/format'

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface RackspaceResourcesViewProps {
  selection: string | null
  cacheProvider: 'aws' | 'azure' | 'rackspace'
  providerVersion: number
}

// ---------------------------------------------------------------------------
// Canonical constants — these mirror the Phase 9B.2 wire contract
// (resource_type values + service display labels).  They are NOT
// inventories or counts — just the stable vocabulary the canonical
// envelope uses for Rackspace.
// ---------------------------------------------------------------------------

const SERVER_TYPE = 'SERVER'
const VOLUME_TYPE = 'VOLUME'
const LOAD_BALANCER_TYPE = 'LOAD_BALANCER'

const SERVICE_CLOUD_SERVERS = 'Cloud Servers'
const SERVICE_CLOUD_BLOCK_STORAGE = 'Cloud Block Storage'
const SERVICE_CLOUD_LOAD_BALANCERS = 'Cloud Load Balancers'

// ---------------------------------------------------------------------------
// Metadata helpers — the canonical resource rows carry product
// fields in ``metadata`` (Phase 9B.2 normalization).  Missing
// optional fields always fall back to the shared "—" placeholder.
// ---------------------------------------------------------------------------

function metaStr(
  metadata: Record<string, unknown> | undefined,
  key: string,
): string | null {
  if (!metadata) return null
  const value = metadata[key]
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'number') return String(value)
  return null
}

function metaInt(
  metadata: Record<string, unknown> | undefined,
  key: string,
): number | null {
  if (!metadata) return null
  const value = metadata[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() && /^-?\d+$/.test(value.trim())) {
    const parsed = Number.parseInt(value.trim(), 10)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

/** RAM is canonical in MB — present as GB only when the conversion
 *  is exact, otherwise keep the raw MB figure.  Never invented. */
function formatRam(mb: number | null): string {
  if (mb === null) return '—'
  if (mb > 0 && mb % 1024 === 0) return `${mb / 1024} GB`
  return `${mb} MB`
}

/** "Oct 3, 2026" — created timestamps are UTC ISO strings. */
function formatCreatedDate(value: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

function resourceIdOrName(item: CloudResourceItem): string {
  return item.name || item.resource_id || '—'
}

function serverStatusTone(state: string | null): 'success' | 'neutral' {
  // ACTIVE is the normal healthy operational status — no
  // utilization or rightsizing inference rides on this badge.
  return state === 'ACTIVE' ? 'success' : 'neutral'
}

function volumeStatusTone(state: string | null): 'success' | 'danger' | 'neutral' {
  if (state === 'ERROR' || state === 'ERROR_DELETING' || state === 'ERROR_RESTORING' || state === 'ERROR_EXTENDING' || state === 'ERROR_BACKUP') {
    return 'danger'
  }
  if (state === 'AVAILABLE' || state === 'IN-USE') return 'success'
  return 'neutral'
}

function attachmentTone(
  attachmentState: string | null,
): 'success' | 'warning' | 'neutral' {
  if (attachmentState === 'ATTACHED') return 'success'
  if (attachmentState === 'DETACHED') return 'warning'
  return 'neutral'
}

function statusTone(status: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  if (status === 'SUCCESS') return 'success'
  if (status === 'PARTIAL_SUCCESS') return 'warning'
  if (status === 'NO_DATA') return 'neutral'
  if (status === 'ERROR') return 'danger'
  return 'info'
}

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

export function RackspaceResourcesView({
  selection,
  cacheProvider,
  providerVersion,
}: RackspaceResourcesViewProps) {
  const [search, setSearch] = useState<string>('')
  const [regionFilter, setRegionFilter] = useState<string>('all')

  // Phase 7B / 7C.1: invalidate stale data on provider / account
  // change.  The canonical cache key already includes the provider
  // segment, so Rackspace rows can never collide with AWS / Azure
  // rows — this effect is the belt-and-braces wipe so a provider
  // switch always lands on a fresh loading state.
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
    () => fetchCloudResources({ provider: 'RACKSPACE', accountId: selection }),
  )

  const data = resources.entry.data
  const summary = data?.summary
  const items = data?.resources ?? []
  // "Request pending" covers both the in-flight status and the
  // pre-auth 'idle' window (the store gates the first fetch on
  // auth bootstrap).  Either way no data exists yet — the loading
  // skeleton is the honest state, never stale provider content and
  // never fabricated zero counts.
  const loading =
    resources.entry.status === 'loading' ||
    resources.entry.status === 'idle'
  const error = resources.entry.error
  // A fatal failure (no data at all) renders the error state
  // INSTEAD of the summary cards — the cards must not fabricate
  // zero counts for a surface that never answered.
  const fatal = !!error && !data

  // ---- Derived product slices -------------------------------------------
  const servers = useMemo(
    () => items.filter((r) => r.resource_type === SERVER_TYPE),
    [items],
  )
  const volumes = useMemo(
    () => items.filter((r) => r.resource_type === VOLUME_TYPE),
    [items],
  )
  const loadBalancers = useMemo(
    () => items.filter((r) => r.resource_type === LOAD_BALANCER_TYPE),
    [items],
  )

  // ---- Canonical counts (by_type — never recomputed from unrelated
  //      data, never fabricated).  A service that could not be
  //      inventoried surfaces as "unknown", not zero. ---------------
  const countByType = (type: string): number | null => {
    const entry = (summary?.by_type ?? []).find((row) => row.key === type)
    return entry ? entry.count : 0
  }
  const failedServices = useMemo(
    () =>
      new Set(
        (summary?.subscription_errors ?? []).map((e) => e.display_name),
      ),
    [summary?.subscription_errors],
  )
  const serviceUnavailable = (service: string): boolean =>
    failedServices.has(service)

  const totalResources = summary?.total_resources ?? 0
  const serverCount = countByType(SERVER_TYPE)
  const volumeCount = countByType(VOLUME_TYPE)
  const lbCount = countByType(LOAD_BALANCER_TYPE)

  // ---- Filters ------------------------------------------------------------
  const regionOptions = useMemo(() => {
    const seen = new Set<string>()
    for (const r of items) if (r.region) seen.add(r.region)
    const out: { value: string; label: string }[] = [
      { value: 'all', label: 'All regions' },
    ]
    for (const region of Array.from(seen).sort()) {
      out.push({ value: region, label: region })
    }
    return out
  }, [items])

  const matchesSearch = (item: CloudResourceItem, q: string): boolean => {
    if (!q) return true
    return (
      (item.name ?? '').toLowerCase().includes(q) ||
      item.resource_id.toLowerCase().includes(q)
    )
  }

  const filteredServers = useMemo(() => {
    const q = search.trim().toLowerCase()
    return servers.filter(
      (r) =>
        (regionFilter === 'all' || r.region === regionFilter) &&
        matchesSearch(r, q),
    )
  }, [servers, regionFilter, search])

  const filteredVolumes = useMemo(() => {
    const q = search.trim().toLowerCase()
    return volumes.filter(
      (r) =>
        (regionFilter === 'all' || r.region === regionFilter) &&
        matchesSearch(r, q),
    )
  }, [volumes, regionFilter, search])

  const filteredLoadBalancers = useMemo(() => {
    const q = search.trim().toLowerCase()
    return loadBalancers.filter(
      (r) =>
        (regionFilter === 'all' || r.region === regionFilter) &&
        matchesSearch(r, q),
    )
  }, [loadBalancers, regionFilter, search])

  // ---- Table column definitions -------------------------------------------

  const serverColumns: DataTableColumn<CloudResourceItem>[] = [
    {
      key: 'name',
      header: 'Name',
      cell: (r) => (
        <span className="truncate" title={r.resource_id}>
          {resourceIdOrName(r)}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (r) => (
        <StatusBadge tone={serverStatusTone(r.state)}>
          {r.state ?? '—'}
        </StatusBadge>
      ),
      width: 'w-28',
    },
    {
      key: 'region',
      header: 'Region',
      cell: (r) => r.region ?? '—',
      width: 'w-28',
    },
    {
      key: 'flavor',
      header: 'Flavor',
      cell: (r) =>
        metaStr(r.metadata, 'flavor_name') ??
        metaStr(r.metadata, 'flavor_id') ??
        '—',
    },
    {
      key: 'vcpu',
      header: 'vCPU',
      cell: (r) => {
        const v = metaInt(r.metadata, 'flavor_vcpus')
        return v === null ? '—' : String(v)
      },
      width: 'w-16',
    },
    {
      key: 'ram',
      header: 'RAM',
      cell: (r) => formatRam(metaInt(r.metadata, 'flavor_ram_mb')),
      width: 'w-24',
    },
    {
      key: 'disk',
      header: 'Disk',
      cell: (r) => {
        const v = metaInt(r.metadata, 'flavor_disk_gb')
        return v === null ? '—' : `${v} GB`
      },
      width: 'w-20',
    },
    {
      key: 'created',
      header: 'Created',
      cell: (r) => formatCreatedDate(metaStr(r.metadata, 'created')),
      width: 'w-28',
    },
  ]

  const volumeColumns: DataTableColumn<CloudResourceItem>[] = [
    {
      key: 'name',
      header: 'Volume',
      cell: (r) => (
        <span className="truncate" title={r.resource_id}>
          {resourceIdOrName(r)}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (r) => (
        <StatusBadge tone={volumeStatusTone(r.state)}>
          {r.state ?? '—'}
        </StatusBadge>
      ),
      width: 'w-28',
    },
    {
      key: 'attachment',
      header: 'Attachment',
      cell: (r) => {
        const state = metaStr(r.metadata, 'attachment_state')
        return (
          <span data-testid={`volume-attachment-${r.resource_id}`}>
            <StatusBadge tone={attachmentTone(state)}>
              {state ?? '—'}
            </StatusBadge>
          </span>
        )
      },
      width: 'w-32',
    },
    {
      key: 'size',
      header: 'Size',
      cell: (r) => {
        const v = metaInt(r.metadata, 'size_gb')
        return v === null ? '—' : `${v} GB`
      },
      width: 'w-20',
    },
    {
      key: 'region',
      header: 'Region',
      cell: (r) => r.region ?? '—',
      width: 'w-28',
    },
    {
      key: 'type',
      header: 'Type',
      cell: (r) => metaStr(r.metadata, 'volume_type') ?? '—',
    },
    {
      key: 'created',
      header: 'Created',
      cell: (r) => formatCreatedDate(metaStr(r.metadata, 'created')),
      width: 'w-28',
    },
  ]

  const loadBalancerColumns: DataTableColumn<CloudResourceItem>[] = [
    {
      key: 'name',
      header: 'Name',
      cell: (r) => (
        <span className="truncate" title={r.resource_id}>
          {resourceIdOrName(r)}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      cell: (r) => (
        <StatusBadge tone={r.state === 'ACTIVE' ? 'success' : 'neutral'}>
          {r.state ?? '—'}
        </StatusBadge>
      ),
      width: 'w-28',
    },
    {
      key: 'protocol',
      header: 'Protocol',
      cell: (r) => metaStr(r.metadata, 'protocol') ?? '—',
    },
    {
      key: 'port',
      header: 'Port',
      cell: (r) => {
        const v = metaInt(r.metadata, 'port')
        return v === null ? '—' : String(v)
      },
      width: 'w-20',
    },
    {
      key: 'region',
      header: 'Region',
      cell: (r) => r.region ?? '—',
      width: 'w-28',
    },
    {
      key: 'created',
      header: 'Created',
      cell: (r) => formatCreatedDate(metaStr(r.metadata, 'created')),
      width: 'w-28',
    },
  ]

  return (
    <div className="space-y-5" data-testid="rackspace-resources-view">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Resources"
        subtitle="Rackspace inventory via Rackspace Cloud APIs (read-only)"
        context={
          <>
            <span className="text-fg-muted">Account</span>
            <span className="text-fg-secondary">
              {selection === 'all' || selection === null
                ? 'All Rackspace Cloud Accounts'
                : maskAccountId(selection)}
            </span>
            <span className="text-fg-muted">Status</span>
            <span className="text-fg-secondary">
              <StatusBadge tone={statusTone(summary?.status ?? 'SUCCESS')}>
                {summary?.status ?? '—'}
              </StatusBadge>
            </span>
            <span className="text-fg-muted">Total</span>
            <span className="text-fg-secondary" data-testid="rackspace-resources-total">
              {loading && !data ? '—' : totalResources.toLocaleString('en-US')}
            </span>
            <span className="text-fg-muted">Updated</span>
            <span className="text-fg-secondary">
              {formatLocalTimeSafe(resources.entry.refreshedAt)}
            </span>
          </>
        }
        actions={<RefreshButton onClick={resources.refresh} />}
      />

      {error && !data && (
        <ErrorState
          title="Could not load Rackspace resources"
          message={error.message}
          onRetry={resources.refresh}
        />
      )}

      {!fatal && (
        <>
      {(summary?.subscription_errors ?? []).length > 0 && (
        <div data-testid="rackspace-resources-partial-warning">
          <PartialWarning
            warnings={(summary?.subscription_errors ?? []).map((e) => ({
              source: 'rackspace-cloud-api',
              code: e.code,
              message: `${e.display_name}: ${e.message}`,
              region: null,
            }))}
            title="Some Rackspace services could not be inventoried"
          />
        </div>
      )}

      {/* Provider-level resource summary — canonical by_type counts.
          A service that failed inventory shows "—" rather than a
          fabricated zero. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div data-testid="rackspace-card-total">
          <MetricCard
            label="Total Resources"
            value={loading && !data ? undefined : totalResources.toLocaleString('en-US')}
            description={loading && !data ? 'Loading…' : 'Canonical inventory total'}
          />
        </div>
        <div data-testid="rackspace-card-servers">
          <MetricCard
            label="Cloud Servers"
            value={
              loading && !data
                ? undefined
                : serviceUnavailable(SERVICE_CLOUD_SERVERS)
                  ? '—'
                  : (serverCount ?? 0).toLocaleString('en-US')
            }
            description={
              serviceUnavailable(SERVICE_CLOUD_SERVERS)
                ? 'Inventory unavailable'
                : 'Compute instances'
            }
          />
        </div>
        <div data-testid="rackspace-card-volumes">
          <MetricCard
            label="Block Storage"
            value={
              loading && !data
                ? undefined
                : serviceUnavailable(SERVICE_CLOUD_BLOCK_STORAGE)
                  ? '—'
                  : (volumeCount ?? 0).toLocaleString('en-US')
            }
            description={
              serviceUnavailable(SERVICE_CLOUD_BLOCK_STORAGE)
                ? 'Inventory unavailable'
                : 'Cloud Block Storage volumes'
            }
          />
        </div>
        <div data-testid="rackspace-card-load-balancers">
          <MetricCard
            label="Load Balancers"
            value={
              loading && !data
                ? undefined
                : serviceUnavailable(SERVICE_CLOUD_LOAD_BALANCERS)
                  ? '—'
                  : (lbCount ?? 0).toLocaleString('en-US')
            }
            description={
              serviceUnavailable(SERVICE_CLOUD_LOAD_BALANCERS)
                ? 'Inventory unavailable'
                : 'Cloud Load Balancers'
            }
          />
        </div>
      </div>

      {/* Filters + inventory sections */}
      <SectionCard
        title="Rackspace Inventory"
        description="Read-only inventory — no management actions are surfaced for Rackspace resources"
        actions={
          <FilterBar>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name or id"
              aria-label="Search Rackspace resources"
              className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-fg-primary placeholder:text-fg-muted focus:outline-none focus-visible:shadow-focus"
              data-testid="rackspace-resources-search"
            />
            <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
              <span>Region</span>
              <select
                value={regionFilter}
                onChange={(e) => setRegionFilter(e.target.value)}
                data-testid="rackspace-resources-region-filter"
                className="rounded-md border border-border bg-surface px-2 py-1 text-xs text-fg-primary focus:outline-none focus-visible:shadow-focus"
              >
                {regionOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </FilterBar>
        }
      >
        {loading && !data ? (
          <LoadingSkeletonRows rows={6} />
        ) : (
          <div className="space-y-5">
            {/* ---------------- Cloud Servers ---------------- */}
            <div>
              <h3
                className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-muted"
                data-testid="rackspace-servers-heading"
              >
                Cloud Servers
              </h3>
              {serviceUnavailable(SERVICE_CLOUD_SERVERS) ? (
                <ServiceUnavailableState service={SERVICE_CLOUD_SERVERS} />
              ) : filteredServers.length === 0 ? (
                <EmptyState
                  title="No Rackspace cloud servers discovered."
                  description="A zero-server result is a valid inventory outcome — not an error."
                />
              ) : (
                <DataTable
                  columns={serverColumns}
                  rows={filteredServers}
                  rowKey={(r) => r.composite_id || r.resource_id}
                  caption="Rackspace Cloud Servers inventory"
                />
              )}
            </div>

            {/* ---------------- Block Storage ---------------- */}
            <div>
              <h3
                className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-muted"
                data-testid="rackspace-volumes-heading"
              >
                Block Storage
              </h3>
              {serviceUnavailable(SERVICE_CLOUD_BLOCK_STORAGE) ? (
                <ServiceUnavailableState service={SERVICE_CLOUD_BLOCK_STORAGE} />
              ) : filteredVolumes.length === 0 ? (
                <EmptyState
                  title="No Rackspace block storage volumes discovered."
                  description="A zero-volume result is a valid inventory outcome — not an error."
                />
              ) : (
                <>
                  <DataTable
                    columns={volumeColumns}
                    rows={filteredVolumes}
                    rowKey={(r) => r.composite_id || r.resource_id}
                    caption="Rackspace Cloud Block Storage inventory"
                  />
                  <p
                    className="mt-2 text-[11px] text-fg-muted"
                    data-testid="rackspace-volume-review-note"
                  >
                    A detached volume is evidence for review only — it is not
                    flagged as safe to delete. No delete, detach or cleanup
                    actions are offered here.
                  </p>
                </>
              )}
            </div>

            {/* ---------------- Load Balancers ---------------- */}
            <div>
              <h3
                className="mb-2 text-xs font-medium uppercase tracking-wider text-fg-muted"
                data-testid="rackspace-load-balancers-heading"
              >
                Load Balancers
              </h3>
              {serviceUnavailable(SERVICE_CLOUD_LOAD_BALANCERS) ? (
                <ServiceUnavailableState service={SERVICE_CLOUD_LOAD_BALANCERS} />
              ) : filteredLoadBalancers.length === 0 ? (
                <EmptyState
                  title="No Rackspace load balancers discovered."
                  description="A zero-result is a valid inventory outcome — not an error or an unavailable service."
                />
              ) : (
                <DataTable
                  columns={loadBalancerColumns}
                  rows={filteredLoadBalancers}
                  rowKey={(r) => r.composite_id || r.resource_id}
                  caption="Rackspace Cloud Load Balancers inventory"
                />
              )}
            </div>
          </div>
        )}
        {loading && data && (
          <div className="mt-3">
            <LoadingSkeleton className="h-4 w-32" />
          </div>
        )}
      </SectionCard>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Shared sub-states
// ---------------------------------------------------------------------------

/** Professional per-service unavailable state (PARTIAL_SUCCESS —
 *  the service could not be queried; the other services' data is
 *  still rendered).  Never fabricates empty data or zero counts. */
function ServiceUnavailableState({ service }: { service: string }) {
  return (
    <div
      role="status"
      data-testid="rackspace-service-unavailable"
      className="flex flex-col items-center justify-center gap-1 rounded-md border border-border bg-surface-2 px-6 py-6 text-center"
    >
      <span className="text-xs font-medium text-fg-secondary">
        {service} inventory unavailable
      </span>
      <span className="max-w-md text-[11px] text-fg-muted">
        The {service} service could not be queried for this scope. Other
        services below and above still reflect the valid data that was
        returned.
      </span>
    </div>
  )
}

function formatLocalTimeSafe(
  value: string | Date | null | undefined,
): string {
  if (value === null || value === undefined) return '—'
  try {
    const d = value instanceof Date ? value : new Date(value)
    if (Number.isNaN(d.getTime())) return '—'
    return d.toLocaleString('en-US', { hour12: true })
  } catch {
    return '—'
  }
}
