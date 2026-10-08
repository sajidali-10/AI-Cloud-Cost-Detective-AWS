// Phase 6B — Recommendation source / confidence badges.

import type { SavingsSource, OptimizationConfidence } from '../types/finops'
import {
  confidenceLabel,
  confidenceTone,
  savingsSourceLabel,
  savingsSourceTone,
} from '../lib/format'
import { StatusBadge } from './StatusBadge'

export function RecommendationSourceBadge({ source }: { source: SavingsSource | string | null }) {
  const label = savingsSourceLabel(source)
  const tone = savingsSourceTone(source)
  return (
    <span className="inline-flex items-center rounded-md border border-border bg-surface text-fg-secondary">
      <StatusBadge tone={tone} title={`Savings source: ${source ?? 'Unknown'}`}>
        {label}
      </StatusBadge>
    </span>
  )
}

export function ConfidenceBadge({ confidence }: { confidence: OptimizationConfidence | string | null }) {
  const label = confidenceLabel(confidence)
  const tone = confidenceTone(confidence)
  return (
    <span className="inline-flex items-center rounded-md border border-border bg-surface text-fg-secondary">
      <StatusBadge tone={tone} title={`Confidence: ${confidence ?? 'Unknown'}`}>
        {label}
      </StatusBadge>
    </span>
  )
}
