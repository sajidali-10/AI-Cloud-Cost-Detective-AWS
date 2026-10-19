// Phase 7D.4 — multi-cloud AI FinOps Analyst client.
//
// Wraps the canonical ``/api/cloud/ai/analyze`` endpoint with
// provider-aware caching via the same useFinopsQuery store the
// rest of the app uses.

import { apiFetch } from './api'
import { buildCloudCostsCacheKey as _buildCloudCostsCacheKey } from './finops/cloudCosts'
import type { BillingPeriod } from '../types/finops'

export type CloudAIProvider = 'AWS' | 'AZURE'

export type CloudAIDataFreshness = 'LIVE' | 'CACHED' | 'STALE' | 'PARTIAL' | 'UNAVAILABLE'

export interface CloudAIEvidence {
  type: string
  label: string
  value: string
  currency?: string | null
  secondary?: string | null
  scope?: string | null
  reference_id?: string | null
}

export interface CloudAIAnswer {
  answer: string
  status: 'SUCCESS' | 'PARTIAL_SUCCESS' | 'FAILED' | 'UNAVAILABLE' | 'DISABLED'
  provider: CloudAIProvider
  scope: string
  account_display_name?: string | null
  period: { kind: string; start: string; end: string; days: number }
  data_freshness: CloudAIDataFreshness
  evidence: CloudAIEvidence[]
  warnings: string[]
  model?: string | null
  duration_ms: number
}

export interface CloudAIStatus {
  enabled: boolean
  model?: string | null
  status: 'READY' | 'DISABLED' | 'UNAVAILABLE'
  message?: string | null
}

export interface CloudAIAnalyzeRequest {
  provider: CloudAIProvider
  scope: string
  question: string
  period?: { month?: string; days?: number; start_date?: string; end_date?: string } | null
  region?: string | null
}

export function buildCloudAIStatusKey(): string {
  return 'cloud_ai:status'
}

export function buildCloudAIAnalyzeKey(req: CloudAIAnalyzeRequest): string {
  // Reuse the canonical cost cache key for the period component
  // so the same provider/scope/period maps to one cache slot.
  const kind = req.period?.month ? 'month' : 'days'
  const month = req.period?.month ?? ''
  const days = 30
  return `cloud_ai:analyze:${req.provider}:${req.scope || 'all'}:${kind}:${kind === 'month' ? month : days}:${hashQuestion(req.question)}`
}

function hashQuestion(q: string): string {
  // Deterministic short hash so we don't cache by the literal
  // question text (which could be enormous).
  let h = 0
  for (let i = 0; i < q.length; i++) {
    h = (h * 31 + q.charCodeAt(i)) | 0
  }
  return Math.abs(h).toString(36)
}

export { hashQuestion }

export async function fetchCloudAIStatus(): Promise<CloudAIStatus> {
  return apiFetch<CloudAIStatus>('/api/cloud/ai/status', { timeoutMs: 15_000 })
}

export async function fetchCloudAIAnalyze(req: CloudAIAnalyzeRequest): Promise<CloudAIAnswer> {
  return apiFetch<CloudAIAnswer>('/api/cloud/ai/analyze', {
    method: 'POST',
    body: JSON.stringify(req),
    timeoutMs: 60_000,
  })
}

// Suggested questions shown in the UI.  Each maps to a category
// from the Phase 7D.4 spec.
export const SUGGESTED_QUESTIONS: { label: string; question: string; provider?: CloudAIProvider }[] = [
  { label: 'How much are we spending this month?', question: 'How much are we spending this month?', provider: 'AWS' },
  { label: 'Which cloud account is costing the most?', question: 'Which cloud account is costing the most?', provider: 'AWS' },
  { label: 'Top services by spend', question: 'What are the top services by spend?' },
  { label: 'Why did costs increase?', question: 'Why did costs increase compared to the previous period?' },
  { label: 'Where can we save money?', question: 'Where can we save money? List the biggest optimization opportunities.' },
  { label: 'Show biggest optimization opportunities', question: 'Show the biggest optimization opportunities with estimated savings.' },
  { label: 'Compare AWS QA and Production', question: 'Compare AWS QA and Production accounts side-by-side.' },
  { label: 'What is happening in Azure?', question: 'What is happening in Azure? Summarize this month.', provider: 'AZURE' },
  { label: 'Which regions are costing the most?', question: 'Which regions are costing the most?' },
  { label: 'Are these numbers live or cached?', question: 'Are these numbers live or cached?' },
]
