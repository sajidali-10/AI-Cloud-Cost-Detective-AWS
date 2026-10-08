// Phase 6B — Capability status row.
//
// Compact representation of a Phase 3 `ServiceCapability`:
// name + status pill.  Used in the Optimization page and the
// Dashboard's environment panel.

import type { ServiceCapability } from '../types/finops'
import { capabilityLabel, capabilityTone } from '../lib/format'
import { StatusBadge } from './StatusBadge'

export function CapabilityStatusRow({
  label,
  capability,
  description,
}: {
  label: string
  capability?: ServiceCapability | null
  description?: string
}) {
  const status = capability?.status ?? null
  const tone = capabilityTone(status)
  const text = capabilityLabel(status)
  return (
    <div className="flex items-center justify-between gap-3 py-1.5 text-xs">
      <div className="min-w-0">
        <div className="text-fg-primary">{label}</div>
        {description && <div className="text-fg-muted">{description}</div>}
        {capability?.detail && (
          <div className="text-fg-muted" data-testid="capability-detail">
            {capability.detail}
          </div>
        )}
        {capability?.error_code && (
          <div className="text-fg-muted">code: {capability.error_code}</div>
        )}
      </div>
      <StatusBadge tone={tone}>{text}</StatusBadge>
    </div>
  )
}
