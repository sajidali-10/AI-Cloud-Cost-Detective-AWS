// Phase 6A — Costs page (structural shell only).
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { EmptyState } from '../components/EmptyState'
import { FilterBar, FilterSelect } from '../components/FilterBar'

export function CostsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Costs"
        subtitle="Browse AWS cost data by account, service and region"
        actions={<FilterBar><FilterSelect label="Account" value="all" onChange={() => {}} options={[{value:'all',label:'All accounts'}]} /></FilterBar>}
      />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard title="Cost by Service" description="Top services by spend">
          <EmptyState title="No data loaded" description="Available in Phase 6B." />
        </SectionCard>
        <SectionCard title="Cost by Region" description="Regional distribution">
          <EmptyState title="No data loaded" description="Available in Phase 6B." />
        </SectionCard>
      </div>
      <SectionCard title="Cost Detail" description="Line-item cost explorer">
        <EmptyState title="No data loaded" description="Available in Phase 6B." />
      </SectionCard>
    </div>
  )
}
