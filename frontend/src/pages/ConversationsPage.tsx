// Phase 6A — Conversations page (shell only).
//
// Conversation list + detail belong to Phase 6C.
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { DataTable } from '../components/DataTable'
import { EmptyState } from '../components/EmptyState'
import { StatusBadge } from '../components/StatusBadge'

export function ConversationsPage() {
  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="Conversations"
        subtitle="Saved AI Cost Analyst conversations"
        actions={<StatusBadge tone="ai">Phase 6C</StatusBadge>}
      />
      <SectionCard title="Saved conversations">
        <DataTable
          columns={[
            { key: 'title', header: 'Title', cell: () => '—' },
            { key: 'updated', header: 'Last updated', cell: () => '—' },
            { key: 'messages', header: 'Messages', cell: () => '—', numeric: true },
          ]}
          rows={[]}
          rowKey={() => 'empty'}
          emptyState={<EmptyState title="No data loaded" description="Conversation history wires in Phase 6C." />}
        />
      </SectionCard>
    </div>
  )
}
