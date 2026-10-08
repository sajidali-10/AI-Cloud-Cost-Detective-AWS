// Phase 6B — Resources page wired to real Phase 1 inventory.
//
// Three panels:
//   1. Resource-type summary cards (EC2 / EBS / EIP / NAT / ELBv2 /
//      RDS / Lambda / S3) with item count + per-service status.
//   2. Filter bar: resource type, region, search by name / ID.
//   3. Operational data table with type-specific safe columns.
//   4. Utilization sub-panel for the selected resource type —
//      CloudWatch metrics when available, "No data" otherwise.

import { useMemo, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { DataTable, type DataTableColumn } from '../components/DataTable'
import { StatusBadge } from '../components/StatusBadge'
import { EmptyState } from '../components/EmptyState'
import { FilterBar, FilterSelect } from '../components/FilterBar'
import { LoadingSkeleton, LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { ErrorState } from '../components/ErrorState'
import { RefreshButton } from '../components/RefreshButton'
import { UtilizationPanel } from '../components/UtilizationPanel'
import { PartialWarning } from '../components/PartialWarning'
import { useFinopsQuery } from '../lib/finops/store'
import { fetchResources, buildResourcesCacheKey } from '../lib/finops/resources'
import {
  fetchUtilization,
  buildUtilizationCacheKey,
} from '../lib/finops/utilization'
import { useFinopsPeriod } from '../lib/finops/period'
import type {
  ResourcesResponse,
  ServiceResult,
  UtilizationResponse,
  Ec2Instance,
  EbsVolume,
  ElasticIp,
  NatGateway,
  LoadBalancerV2,
  RdsInstance,
  LambdaFunction,
  S3Bucket,
} from '../types/finops'
import { serviceStatusLabel } from '../lib/format'

type ResourceKind =
  | 'ec2'
  | 'ebs'
  | 'eip'
  | 'nat'
  | 'elbv2'
  | 'rds'
  | 'lambda'
  | 's3'

interface NormalizedRow {
  key: string
  kind: ResourceKind
  kindLabel: string
  name: string
  resourceId: string
  region: string
  state: string
  extra: string
}

const KIND_LABELS: Record<ResourceKind, string> = {
  ec2: 'EC2',
  ebs: 'EBS',
  eip: 'Elastic IP',
  nat: 'NAT Gateway',
  elbv2: 'Load Balancer',
  rds: 'RDS',
  lambda: 'Lambda',
  s3: 'S3',
}

export function ResourcesPage() {
  const { region } = useFinopsPeriod()
  const effectiveRegion = region === 'all' ? null : region

  const resources = useFinopsQuery<ResourcesResponse>(
    buildResourcesCacheKey(effectiveRegion),
    () => fetchResources(effectiveRegion),
  )

  const kindOptions = useMemo<{ value: string; label: string }[]>(() => {
    const available = Object.keys(resources.entry.data?.services ?? {}) as ResourceKind[]
    const out: { value: string; label: string }[] = [{ value: 'all', label: 'All types' }]
    for (const k of available) {
      out.push({ value: k, label: KIND_LABELS[k] ?? k })
    }
    return out
  }, [resources.entry.data])

  const regionOptions = useMemo(() => {
    const seen = new Set<string>()
    const out: { value: string; label: string }[] = [{ value: 'all', label: 'All regions' }]
    const services = (resources.entry.data?.services ?? {}) as unknown as Record<string, ServiceResult<Record<string, unknown>> | undefined>
    for (const svc of Object.values(services)) {
      if (!svc) continue
      for (const item of svc.items) {
        const r = (item as { region?: string | null }).region
        if (r && !seen.has(r)) {
          seen.add(r)
          out.push({ value: r, label: r })
        }
      }
    }
    return out
  }, [resources.entry.data])

  const [kindFilter, setKindFilter] = useState<string>('all')
  const [regionFilter, setRegionFilter] = useState<string>('all')
  const [search, setSearch] = useState<string>('')
  const [selectedResource, setSelectedResource] = useState<{ id: string; kind: ResourceKind } | null>(null)

  const normalized = useMemo(
    () => normalizeAll(resources.entry.data),
    [resources.entry.data],
  )

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return normalized.filter((row) => {
      if (kindFilter !== 'all' && row.kind !== kindFilter) return false
      if (regionFilter !== 'all' && row.region !== regionFilter) return false
      if (q && !(row.name.toLowerCase().includes(q) || row.resourceId.toLowerCase().includes(q) || row.extra.toLowerCase().includes(q))) {
        return false
      }
      return true
    })
  }, [normalized, kindFilter, regionFilter, search])

  // Utilization lookup — only fetched when the user has selected a
  // resource row.  Uses the FinOps period for lookback consistency.
  const util = useFinopsQuery<UtilizationResponse | null>(
    selectedResource
      ? buildUtilizationCacheKey({
          region: effectiveRegion ?? resources.entry.data?.region ?? 'us-east-1',
          lookback_days: 30,
          resource_types: utilizationKindsFor(selectedResource.kind),
        })
      : `utilization:none`,
    async () => {
      if (!selectedResource) return null
      return fetchUtilization({
        region: effectiveRegion ?? resources.entry.data?.region ?? 'us-east-1',
        lookback_days: 30,
        resource_types: utilizationKindsFor(selectedResource.kind),
      })
    },
    { ttlMs: 60_000 },
  )

  const utilResource = useMemo(() => {
    if (!util.entry.data || !selectedResource) return undefined
    return util.entry.data.resources.find((r) => r.resource_id === selectedResource.id)
  }, [util.entry.data, selectedResource])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Resources"
        subtitle="Inventory of discovered AWS resources"
        context={
          <>
            <span className="text-fg-muted">Region</span>
            <span className="text-fg-secondary">{resources.entry.data?.region ?? effectiveRegion ?? '—'}</span>
            <span className="text-fg-muted">Total</span>
            <span className="text-fg-secondary" data-testid="resources-total">
              {normalized.length.toLocaleString('en-US')}
            </span>
          </>
        }
        actions={<RefreshButton onClick={resources.refresh} />}
      />

      {resources.entry.error && !resources.entry.data && (
        <ErrorState
          title="Could not load resources"
          message={resources.entry.error.message}
          onRetry={resources.refresh}
        />
      )}

      {/* Resource-type summary cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
        {(Object.keys(KIND_LABELS) as ResourceKind[]).map((kind) => {
          const svc = resources.entry.data?.services[kind] as unknown as ServiceResult<Record<string, unknown>> | undefined
          return <ResourceTypeCard key={kind} kind={kind} service={svc} loading={resources.entry.status === 'loading' && !resources.entry.data} />
        })}
      </div>

      {/* Filters + table */}
      <SectionCard
        title="Discovered resources"
        description={`${filtered.length.toLocaleString('en-US')} of ${normalized.length.toLocaleString('en-US')} rows`}
        actions={
          <FilterBar>
            <FilterSelect
              label="Type"
              value={kindFilter}
              onChange={setKindFilter}
              options={kindOptions}
              testId="kind-filter"
            />
            <FilterSelect
              label="Region"
              value={regionFilter}
              onChange={setRegionFilter}
              options={regionOptions}
              testId="region-filter-resources"
            />
            <SearchInput value={search} onChange={setSearch} />
          </FilterBar>
        }
      >
        {resources.entry.status === 'loading' && !resources.entry.data ? (
          <LoadingSkeletonRows rows={6} />
        ) : (
          <DataTable
            columns={columns}
            rows={filtered.slice(0, 200)}
            rowKey={(r) => r.key}
            emptyState={
              <EmptyState
                title="No matching resources"
                description="Adjust filters or wait for the next discovery scan."
              />
            }
            caption="Discovered AWS resources"
            onRowClick={(row) => {
              if (utilizationKindsFor(row.kind).length === 0) return
              setSelectedResource({ id: row.resourceId, kind: row.kind })
            }}
            isRowClickable={(row) => utilizationKindsFor(row.kind).length > 0}
          />
        )}
        {filtered.length > 200 && (
          <p className="mt-2 text-xs text-fg-muted">
            Showing first 200 of {filtered.length.toLocaleString('en-US')} rows.
          </p>
        )}
      </SectionCard>

      {/* Utilization panel */}
      <SectionCard
        title="Utilization"
        description="CloudWatch metrics for the selected resource (30-day lookback)"
      >
        {!selectedResource && (
          <EmptyState
            title="Select a resource"
            description="Click any row above to view its CloudWatch utilization evidence."
          />
        )}
        {selectedResource && (
          <>
            <p className="mb-2 text-xs text-fg-muted">
              Resource: <span className="text-fg-primary">{selectedResource.id}</span>
              {' · '}
              {KIND_LABELS[selectedResource.kind]}
            </p>
            <UtilizationPanel
              resource={utilResource}
              isLoading={util.entry.status === 'loading'}
              error={util.entry.error}
            />
            {util.entry.data && util.entry.data.warnings.length > 0 && (
              <div className="mt-3">
                <PartialWarning warnings={util.entry.data.warnings} title="CloudWatch warnings" />
              </div>
            )}
          </>
        )}
      </SectionCard>
    </div>
  )
}

function ResourceTypeCard({
  kind,
  service,
  loading,
}: {
  kind: ResourceKind
  service?: ServiceResult<Record<string, unknown>>
  loading: boolean
}) {
  const label = KIND_LABELS[kind]
  const count = service?.items.length ?? 0
  const status = service?.status ?? null
  const tone =
    status === 'denied' ? 'warning' : status === 'error' ? 'danger' : status === 'ok' ? 'success' : 'neutral'
  return (
    <button
      type="button"
      onClick={() => undefined}
      className="
        flex flex-col items-start gap-1 rounded-lg border border-border
        bg-surface p-3 text-left shadow-card-sm hover:bg-surface-hover
        focus:outline-none focus-visible:shadow-focus
      "
      data-testid={`resource-card-${kind}`}
    >
      <div className="flex w-full items-center justify-between text-xs text-fg-muted">
        <span className="uppercase tracking-wider">{label}</span>
        <StatusBadge tone={tone}>{serviceStatusLabel(status)}</StatusBadge>
      </div>
      {loading ? (
        <LoadingSkeleton className="h-6 w-12" />
      ) : (
        <span className="text-xl font-semibold tabular-nums text-fg-primary" data-testid={`resource-card-${kind}-count`}>
          {count.toLocaleString('en-US')}
        </span>
      )}
      {service?.error_code && (
        <span className="truncate text-[10px] text-fg-muted">code: {service.error_code}</span>
      )}
    </button>
  )
}

function SearchInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="inline-flex items-center gap-2 text-xs text-fg-muted">
      <span>Search</span>
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="name or id"
        data-testid="resource-search"
        className="
          w-40 rounded-md border border-border bg-surface px-2 py-1 text-xs
          text-fg-primary focus:outline-none focus-visible:shadow-focus
        "
      />
    </label>
  )
}

const columns: DataTableColumn<NormalizedRow>[] = [
  {
    key: 'kindLabel',
    header: 'Type',
    cell: (r) => (
      <span className="inline-flex items-center rounded-md border border-border bg-surface-2 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider text-fg-secondary">
        {r.kindLabel}
      </span>
    ),
    width: 'w-28',
  },
  {
    key: 'name',
    header: 'Name',
    cell: (r) => <span className="truncate" title={r.name}>{r.name}</span>,
  },
  {
    key: 'resourceId',
    header: 'Resource ID',
    cell: (r) => <span className="truncate font-mono text-[11px] text-fg-secondary" title={r.resourceId}>{r.resourceId}</span>,
    width: 'w-64',
  },
  {
    key: 'region',
    header: 'Region',
    cell: (r) => <span className="text-fg-secondary">{r.region}</span>,
    width: 'w-32',
  },
  {
    key: 'state',
    header: 'State',
    cell: (r) => <span className="text-fg-secondary">{r.state}</span>,
    width: 'w-28',
  },
  {
    key: 'extra',
    header: 'Details',
    cell: (r) => <span className="truncate text-fg-muted" title={r.extra}>{r.extra}</span>,
  },
]

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

function normalizeAll(data: ResourcesResponse | null): NormalizedRow[] {
  if (!data) return []
  const rows: NormalizedRow[] = []
  for (const kind of Object.keys(KIND_LABELS) as ResourceKind[]) {
    const svc = data.services[kind]
    if (!svc) continue
    const items = svc.items as unknown[]
    for (const item of items) {
      const row = normalizeItem(kind, item)
      if (row) rows.push(row)
    }
  }
  return rows
}

function normalizeItem(kind: ResourceKind, item: unknown): NormalizedRow | null {
  switch (kind) {
    case 'ec2': {
      const i = item as Ec2Instance
      return {
        key: `ec2:${i.instance_id}`,
        kind,
        kindLabel: 'EC2',
        name: tagName(i.tags) ?? i.instance_id,
        resourceId: i.instance_id,
        region: i.region ?? '—',
        state: i.state ?? '—',
        extra: i.instance_type ? `type ${i.instance_type}` : '',
      }
    }
    case 'ebs': {
      const i = item as EbsVolume
      return {
        key: `ebs:${i.volume_id}`,
        kind,
        kindLabel: 'EBS',
        name: tagName(i.tags) ?? i.volume_id,
        resourceId: i.volume_id,
        region: i.region ?? '—',
        state: i.state ?? '—',
        extra:
          i.size_gb != null
            ? `${i.size_gb} GB · ${i.attachments ?? 0} attachment${i.attachments === 1 ? '' : 's'}`
            : '',
      }
    }
    case 'eip': {
      const i = item as ElasticIp
      return {
        key: `eip:${i.public_ip}`,
        kind,
        kindLabel: 'EIP',
        name: tagName(i.tags) ?? i.public_ip,
        resourceId: i.public_ip,
        region: i.region ?? '—',
        state: i.association_id ? 'associated' : 'unassociated',
        extra: i.allocation_id ?? '',
      }
    }
    case 'nat': {
      const i = item as NatGateway
      return {
        key: `nat:${i.nat_gateway_id}`,
        kind,
        kindLabel: 'NAT',
        name: tagName(i.tags) ?? i.nat_gateway_id,
        resourceId: i.nat_gateway_id,
        region: i.region ?? '—',
        state: i.state ?? '—',
        extra: '',
      }
    }
    case 'elbv2': {
      const i = item as LoadBalancerV2
      return {
        key: `elbv2:${i.arn}`,
        kind,
        kindLabel: 'ELB',
        name: i.name ?? i.arn,
        resourceId: i.arn,
        region: i.region ?? '—',
        state: '—',
        extra: i.type,
      }
    }
    case 'rds': {
      const i = item as RdsInstance
      return {
        key: `rds:${i.db_instance_identifier}`,
        kind,
        kindLabel: 'RDS',
        name: tagName(i.tags) ?? i.db_instance_identifier,
        resourceId: i.db_instance_identifier,
        region: i.region ?? '—',
        state: i.status ?? '—',
        extra: [i.engine, i.db_instance_class].filter(Boolean).join(' · '),
      }
    }
    case 'lambda': {
      const i = item as LambdaFunction
      return {
        key: `lambda:${i.function_name}`,
        kind,
        kindLabel: 'Lambda',
        name: i.function_name,
        resourceId: i.function_name,
        region: i.region ?? '—',
        state: '—',
        extra: i.runtime ?? '',
      }
    }
    case 's3': {
      const i = item as S3Bucket
      return {
        key: `s3:${i.name}`,
        kind,
        kindLabel: 'S3',
        name: i.name,
        resourceId: i.name,
        region: i.region ?? '—',
        state: '—',
        extra: i.creation_date ? `created ${i.creation_date.slice(0, 10)}` : '',
      }
    }
  }
}

function tagName(tags: Record<string, string> | undefined): string | undefined {
  if (!tags) return undefined
  return tags['Name'] ?? tags['name']
}

function utilizationKindsFor(kind: ResourceKind): string[] {
  switch (kind) {
    case 'ec2':
      return ['ec2']
    case 'rds':
      return ['rds']
    case 'lambda':
      return ['lambda']
    case 'elbv2':
      return ['alb', 'nlb']
    default:
      return []
  }
}
