// Phase 6C — AI provider readiness badge.
//
// Driven by `AIStatusResponse.status`.  Never embeds the model URL
// verbatim (the litellm base URL is operator-internal and could
// expose internal hosts).

import type { AIStatusResponse } from '../types/ai'
import { StatusBadge, type StatusTone } from './StatusBadge'

export function AiProviderBadge({ status }: { status: AIStatusResponse | null }) {
  if (!status) {
    return (
      <span className="inline-flex items-center text-fg-secondary">
        <StatusBadge tone="neutral">AI status unknown</StatusBadge>
      </span>
    )
  }
  const tone: StatusTone =
    status.status === 'OK'
      ? 'success'
      : status.status === 'DEGRADED'
        ? 'warning'
        : 'danger'
  const label =
    status.status === 'OK'
      ? `AI ready · ${status.model_alias}`
      : status.status === 'DEGRADED'
        ? 'AI degraded'
        : 'AI disabled'
  return (
    <span className="inline-flex items-center text-fg-secondary">
      <StatusBadge tone={tone} title={status.message ?? status.status}>{label}</StatusBadge>
    </span>
  )
}
