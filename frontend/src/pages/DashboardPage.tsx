// Phase 6A — Dashboard page (structural shell only).
//
// The HipLink reference dashboard has:
//   - PageHeader (product title, page title, subtitle, context row)
//   - 4 KPI tiles
//   - 2 large cards: Cost Trend, Cost by Service
//   - 2 summary cards: Optimization Summary, AWS Environment
//
// Phase 6A wires the layout and the chart-ready containers.  Real
// AWS data, the cost-trend chart, and the service distribution
// chart are owned by Phase 6B.  Phase 6A MUST NOT fabricate any
// values.

import { MetricCard } from '../components/MetricCard'
import { PageHeader, ContextItem } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { EmptyState } from '../components/EmptyState'
import { StatusBadge } from '../components/StatusBadge'

export function DashboardPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Dashboard"
        subtitle="AWS cost visibility, optimization and AI-powered FinOps analysis"
        context={
          <>
            <ContextItem label="Account" value="—" />
            <ContextItem label="Region" value="—" />
            <ContextItem label="Status" value={<StatusBadge tone="neutral">No data loaded</StatusBadge>} />
            <ContextItem label="Last updated" value="—" />
          </>
        }
      />

      {/* KPI tiles */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricCard
          label="AWS Spend"
          icon={<SpendIcon />}
          description="Month-to-date"
        />
        <MetricCard
          label="Cost Δ"
          icon={<DeltaIcon />}
          description="vs. previous period"
        />
        <MetricCard
          label="Resources"
          icon={<ResourcesIcon />}
          description="Tracked across accounts"
        />
        <MetricCard
          label="Optimize"
          icon={<OptimizeIcon />}
          description="Estimated savings"
        />
      </div>

      {/* Two-column chart row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard title="Cost Trend" description="Daily spend over selected period">
          <EmptyState
            title="No data loaded"
            description="Real cost-trend visualisation will be wired in Phase 6B."
          />
        </SectionCard>
        <SectionCard title="Cost by Service" description="Distribution across AWS services">
          <EmptyState
            title="No data loaded"
            description="Real service-distribution visualisation will be wired in Phase 6B."
          />
        </SectionCard>
      </div>

      {/* Two-column summary row */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SectionCard title="Optimization Summary" description="Open recommendations grouped by impact">
          <EmptyState
            title="No data loaded"
            description="Optimization rollups will be wired in Phase 6B."
          />
        </SectionCard>
        <SectionCard title="AWS Environment" description="Accounts, regions and active services">
          <EmptyState
            title="No data loaded"
            description="Environment summary will be wired in Phase 6B."
          />
        </SectionCard>
      </div>
    </div>
  )
}

function SpendIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2v20" />
      <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
    </svg>
  )
}
function DeltaIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m22 7-8.5 8.5-5-5L2 17" />
      <path d="M16 7h6v6" />
    </svg>
  )
}
function ResourcesIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8" />
      <path d="M12 17v4" />
    </svg>
  )
}
function OptimizeIcon() {
  return (
    <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}
