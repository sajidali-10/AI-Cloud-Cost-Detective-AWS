// Phase 6A — Resources page (structural shell only).
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { DataTable } from '../components/DataTable'
import { EmptyState } from '../components/EmptyState'

export function ResourcesPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Resources"
        subtitle="Inventory of discovered AWS resources"
      />
      <SectionCard title="Discovered resources" description="EC2, RDS, EBS, Lambda and more">
        <DataTable
          columns={[
            { key: 'name', header: 'Name', cell: () => '—' },
            { key: 'type', header: 'Type', cell: () => '—' },
            { key: 'region', header: 'Region', cell: () => '—' },
            { key: 'state', header: 'State', cell: () => '—' },
            { key: 'lastSeen', header: 'Last seen', cell: () => '—' },
          ]}
          rows={[]}
          rowKey={() => 'empty'}
          emptyState={<EmptyState title="No data loaded" description="Resource inventory is wired in Phase 6B." />}
        />
      </SectionCard>
    </div>
  )
}
