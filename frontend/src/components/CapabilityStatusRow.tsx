// Phase 7A — Capability status row.
//
// Compact representation of a Phase 3 `ServiceCapability`:
// name + status pill, plus operator-facing guidance when a native
// AWS optimization source is INACTIVE or NOT_ENROLLED.  The app
// NEVER calls `UpdateEnrollmentStatus` or any equivalent mutation
// API — AWS enrollment is performed manually outside the
// application, then detected automatically the next time the
// dashboard refreshes.

import type { ServiceCapability } from '../types/finops'
import { capabilityLabel, capabilityTone } from '../lib/format'
import { StatusBadge } from './StatusBadge'

const GUIDANCE: Record<string, string> = {
  compute_optimizer: (
    'Compute Optimizer is not currently opted in. ' +
    'Run `aws compute-optimizer update-enrollment-status --status Active` from an admin principal, ' +
    'or open the Compute Optimizer console, to enable. After enrollment, refresh to detect native recommendations.'
  ),
  cost_optimization_hub: (
    'Cost Optimization Hub is not enrolled. Open the Cost Optimization Hub console and enroll the management account. ' +
    'After enrollment and AWS processing (~24h), refresh to detect hub recommendations.'
  ),
  deterministic_engine: (
    'Deterministic recommendations work without any AWS enrollment.'
  ),
}

export function CapabilityStatusRow({
  label,
  capability,
  description,
  kind,
}: {
  label: string
  capability?: ServiceCapability | null
  description?: string
  /** Source identifier; selects the operator-facing guidance copy. */
  kind?: 'compute_optimizer' | 'cost_optimization_hub' | 'deterministic_engine'
}) {
  const status = capability?.status ?? null
  const tone = capabilityTone(status)
  const text = capabilityLabel(status)
  const showGuidance =
    kind === 'compute_optimizer' && status === 'INACTIVE'
    || kind === 'cost_optimization_hub' && (status === 'INACTIVE' || status === 'NOT_ENROLLED')
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
        {showGuidance && kind && (
          <div
            className="mt-1 rounded bg-warning-soft px-2 py-1 text-fg-secondary"
            data-testid="capability-guidance"
          >
            {GUIDANCE[kind]}
          </div>
        )}
      </div>
      <StatusBadge tone={tone}>{text}</StatusBadge>
    </div>
  )
}
