// Phase 6A — AI Cost Analyst shell.
//
// Full chat experience (conversation list, message stream, WebSocket
// transport) is owned by Phase 6C.  Phase 6A renders the route
// shell so the role-aware navigation is testable end-to-end.
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { EmptyState } from '../components/EmptyState'
import { StatusBadge } from '../components/StatusBadge'

export function AIAnalystPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="AI Cost Analyst"
        subtitle="Ask grounded questions about your AWS cost posture"
        actions={<StatusBadge tone="ai">Phase 6C</StatusBadge>}
      />
      <SectionCard title="Conversation">
        <EmptyState
          title="AI conversation shell"
          description="The full AI Cost Analyst experience (conversation list, message stream, realtime transport) is delivered in Phase 6C."
        />
      </SectionCard>
    </div>
  )
}
