// Phase 7A — AI provider readiness badge.
//
// Driven by `AIStatusResponse.status`.  Never embeds credentials,
// raw provider URLs, or API keys.  When the provider is not yet
// configured the badge surfaces the required environment variable
// so operators can wire credentials without reading the backend code.

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
  let tone: StatusTone = 'neutral'
  let label = 'AI status unknown'
  if (status.status === 'OK') {
    tone = 'success'
    label = `AI ready · ${status.model_alias}`
  } else if (status.status === 'DEGRADED') {
    tone = 'warning'
    label = 'AI degraded'
  } else if (!status.ai_enabled) {
    tone = 'neutral'
    label = 'AI disabled'
  } else if (!status.provider_configured) {
    tone = 'warning'
    label = status.required_env_var
      ? `AI waiting on ${status.required_env_var}`
      : 'AI provider not configured'
  } else {
    tone = 'danger'
    label = 'AI unreachable'
  }
  const title = status.message ?? status.status
  return (
    <span className="inline-flex items-center text-fg-secondary">
      <StatusBadge tone={tone} title={title}>{label}</StatusBadge>
    </span>
  )
}
