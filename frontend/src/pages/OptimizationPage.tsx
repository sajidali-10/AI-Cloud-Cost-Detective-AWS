// Phase 6A — Optimization page (structural shell only).
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { MetricCard } from '../components/MetricCard'
import { DataTable } from '../components/DataTable'
import { EmptyState } from '../components/EmptyState'
import { StatusBadge } from '../components/StatusBadge'

export function OptimizationPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Optimization"
        subtitle="Recommendations grouped by impact and confidence"
        actions={<StatusBadge tone="ai">Phase 6B</StatusBadge>}
      />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <MetricCard label="Open recommendations" />
        <MetricCard label="High impact" />
        <MetricCard label="Estimated monthly savings" />
      </div>
      <SectionCard title="Recommendations" description="Grouped by service and impact">
        <DataTable
          columns={[
            { key: 'service', header: 'Service', cell: () => '—' },
            { key: 'type', header: 'Type', cell: () => '—' },
            { key: 'impact', header: 'Impact', cell: () => '—' },
            { key: 'savings', header: 'Est. savings', cell: () => '—', numeric: true },
            { key: 'confidence', header: 'Confidence', cell: () => '—' },
          ]}
          rows={[]}
          rowKey={() => 'empty'}
          emptyState={<EmptyState title="No data loaded" description="Optimization rollups wire in Phase 6B." />}
        />
      </SectionCard>
    </div>
  )
}
