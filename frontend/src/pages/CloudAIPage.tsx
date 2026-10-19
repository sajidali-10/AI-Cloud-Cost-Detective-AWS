// Phase 7D.4 — multi-cloud AI FinOps Analyst workspace.
//
// Reuses the existing visual system (SectionCard, MetricCard,
// ProviderSelector, AccountSelector, PeriodSelector, AiProviderBadge,
// StatusBadge) and surfaces:
//
//   - provider / scope / period selectors
//   - suggested questions
//   - answer prose
//   - deterministic evidence cards
//   - data-freshness indicator
//   - AI-unavailable mode (still renders deterministic evidence)
//
// All question submission goes through the FinOps cache store
// (``useFinopsQuery``) keyed by provider / scope / period /
// question-hash.  Repeated identical questions hit the in-memory
// cache and don't spam the AI endpoint.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { PageHeader } from '../components/PageHeader'
import { SectionCard } from '../components/SectionCard'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeletonRows } from '../components/LoadingSkeleton'
import { StatusBadge } from '../components/StatusBadge'
import { ProviderSelector } from '../components/ProviderSelector'
import { AccountSelector } from '../components/AccountSelector'
import { PeriodSelector } from '../components/PeriodSelector'
import { AiProviderBadge } from '../components/AiProviderBadge'
import { useFinopsQuery } from '../lib/finops/store'
import {
  fetchCloudAIStatus,
  fetchCloudAIAnalyze,
  buildCloudAIStatusKey,
  buildCloudAIAnalyzeKey,
  SUGGESTED_QUESTIONS,
  type CloudAIProvider,
  type CloudAIAnswer,
  type CloudAIStatus,
  type CloudAIEvidence,
  type CloudAIAnalyzeRequest,
} from '../lib/cloudAi'
import { useProviderContext } from '../lib/providerContext'
import { useAccountContext } from '../lib/accountContext'
import { formatLocalTime } from '../lib/format'

function freshnessTone(
  freshness: CloudAIAnswer['data_freshness'] | 'UNKNOWN',
): 'success' | 'warning' | 'danger' | 'neutral' | 'info' {
  switch (freshness) {
    case 'LIVE':
      return 'success'
    case 'CACHED':
      return 'info'
    case 'STALE':
      return 'warning'
    case 'PARTIAL':
      return 'warning'
    case 'UNAVAILABLE':
      return 'danger'
    default:
      return 'neutral'
  }
}

function freshnessLabel(
  freshness: CloudAIAnswer['data_freshness'] | 'UNKNOWN',
): string {
  switch (freshness) {
    case 'LIVE':
      return 'Live'
    case 'CACHED':
      return 'Cached'
    case 'STALE':
      return 'Stale'
    case 'PARTIAL':
      return 'Partial'
    case 'UNAVAILABLE':
      return 'Unavailable'
    default:
      return 'Unknown'
  }
}

function statusLabel(status: CloudAIAnswer['status']): string {
  switch (status) {
    case 'SUCCESS':
      return 'Success'
    case 'PARTIAL_SUCCESS':
      return 'Partial'
    case 'UNAVAILABLE':
      return 'AI unavailable'
    case 'DISABLED':
      return 'AI disabled'
    case 'FAILED':
      return 'AI failed'
    default:
      return status
  }
}

export function CloudAIPage() {
  const { provider, providerVersion } = useProviderContext()
  const { selection } = useAccountContext()
  const [question, setQuestion] = useState<string>('')
  const [submittedQuestion, setSubmittedQuestion] = useState<string>('')
  const [submittedScope, setSubmittedScope] = useState<string>('')
  const [submittedProvider, setSubmittedProvider] = useState<CloudAIProvider>('AWS')

  // ---- AI capability status (cached for 60 s) ----
  const statusQ = useFinopsQuery<CloudAIStatus>(
    buildCloudAIStatusKey(),
    () => fetchCloudAIStatus(),
    { ttlMs: 60_000 },
  )
  const aiStatus: CloudAIStatus | null = statusQ.entry.data

  // ---- Build the analyze request key ----
  const providerForRequest: CloudAIProvider = (provider === 'AZURE' ? 'AZURE' : 'AWS')
  const request: CloudAIAnalyzeRequest | null = useMemo(() => {
    if (!submittedQuestion || submittedQuestion.length < 3) return null
    return {
      provider: submittedProvider,
      scope: submittedScope || 'all',
      question: submittedQuestion,
    }
  }, [submittedProvider, submittedScope, submittedQuestion])

  const analyzeQ = useFinopsQuery<CloudAIAnswer>(
    request ? buildCloudAIAnalyzeKey(request) : 'cloud_ai:analyze:pending',
    async () => {
      if (!request) throw new Error('No question submitted yet.')
      return fetchCloudAIAnalyze(request)
    },
    { ttlMs: 30_000 },
  )

  const answer = analyzeQ.entry.data
  const submitting = analyzeQ.entry.status === 'loading' || analyzeQ.entry.status === 'refreshing'
  const errorMsg = analyzeQ.entry.error?.message

  const submit = useCallback(
    (q: string) => {
      const trimmed = q.trim()
      if (trimmed.length < 3) return
      setSubmittedQuestion(trimmed)
      setSubmittedScope(selection || 'all')
      setSubmittedProvider(providerForRequest)
    },
    [selection, providerForRequest],
  )

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    submit(question)
  }

  // Reset answer when scope / provider / period changes so the UI
  // does not display stale evidence under a new selection.
  useEffect(() => {
    setSubmittedQuestion('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection, providerForRequest, providerVersion])

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="AI Cloud Cost Detective"
        title="AI FinOps Analyst"
        subtitle="Multi-cloud grounded answers from canonical cost, resource, and optimization data."
        context={
          <>
            <span className="text-fg-muted">Provider</span>
            <AiProviderBadge status={{ provider: providerForRequest, enabled: aiStatus?.enabled ?? false, status: aiStatus?.status ?? 'UNAVAILABLE', message: aiStatus?.message ?? null } as any} />
            <span className="text-fg-muted">Scope</span>
            <span className="text-fg-secondary">
              {submittedScope || selection || 'all'}
            </span>
            <span className="text-fg-muted">Status</span>
            <span data-testid="cloud-ai-status">
              <StatusBadge tone={aiStatus?.enabled ? (aiStatus?.status === 'READY' ? 'success' : 'warning') : 'neutral'}>
                {aiStatus?.enabled ? (aiStatus.status === 'READY' ? 'AI ready' : 'AI unavailable') : 'AI disabled'}
              </StatusBadge>
            </span>
          </>
        }
      />

      <SectionCard title="Selection" padded>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div>
            <div className="text-xs text-fg-muted mb-1">Provider</div>
            <ProviderSelector />
          </div>
          <div>
            <div className="text-xs text-fg-muted mb-1">Scope</div>
            <AccountSelector />
          </div>
          <div>
            <div className="text-xs text-fg-muted mb-1">Period</div>
            <PeriodSelector />
          </div>
        </div>
      </SectionCard>

      <SectionCard title="Ask a question" padded>
        <form onSubmit={onSubmit} className="space-y-3">
          <textarea
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="e.g. How much are we spending this month?"
            className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
            rows={3}
            data-testid="cloud-ai-question"
          />
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={submitting || question.trim().length < 3}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-on-primary disabled:opacity-50"
              data-testid="cloud-ai-submit"
            >
              {submitting ? 'Asking…' : 'Ask'}
            </button>
            <span className="text-xs text-fg-muted">Suggested:</span>
            {SUGGESTED_QUESTIONS.slice(0, 6).map((s) => (
              <button
                key={s.label}
                type="button"
                onClick={() => {
                  setQuestion(s.question)
                  submit(s.question)
                }}
                className="rounded-full border border-border bg-surface px-2 py-0.5 text-xs text-fg-secondary hover:bg-surface-2"
              >
                {s.label}
              </button>
            ))}
          </div>
        </form>
      </SectionCard>

      {!aiStatus?.enabled && (
        <SectionCard title="AI analyst status" padded>
          <p className="text-sm text-fg-muted">
            AI generation is disabled in configuration. The deterministic evidence below is still
            computed from the canonical envelopes, but the model is not invoked.
          </p>
          <p className="text-xs text-fg-muted mt-2">{aiStatus?.message ?? 'AI_DISABLED'}</p>
        </SectionCard>
      )}

      {errorMsg && !answer && (
        <ErrorState
          title="AI analyst error"
          message={errorMsg}
          onRetry={() => analyzeQ.refresh()}
        />
      )}

      {submitting && !answer && (
        <SectionCard title="AI analyst response" padded>
          <LoadingSkeletonRows rows={4} />
        </SectionCard>
      )}

      {answer && (
        <>
          <SectionCard
            title="AI analyst response"
            actions={
              <span data-testid="cloud-ai-freshness">
                <StatusBadge tone={freshnessTone(answer.data_freshness)}>
                  {freshnessLabel(answer.data_freshness)}
                </StatusBadge>
              </span>
            }
            padded
          >
            <div data-testid="cloud-ai-answer" className="prose prose-invert max-w-none whitespace-pre-line">
              {answer.answer || 'AI analyst returned no answer text.'}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
              <span data-testid="cloud-ai-status-badge">
                <StatusBadge tone={answer.status === 'SUCCESS' ? 'success' : 'warning'}>
                  {statusLabel(answer.status)}
                </StatusBadge>
              </span>
              <span>•</span>
              <span>Provider: {answer.provider}</span>
              <span>•</span>
              <span>Scope: {answer.scope}</span>
              {answer.account_display_name && (
                <>
                  <span>•</span>
                  <span>{answer.account_display_name}</span>
                </>
              )}
              {answer.model && (
                <>
                  <span>•</span>
                  <span>Model: {answer.model}</span>
                </>
              )}
              <span>•</span>
              <span>Duration: {answer.duration_ms} ms</span>
            </div>
            {answer.warnings && answer.warnings.length > 0 && (
              <ul className="mt-3 list-disc pl-5 text-xs text-fg-muted">
                {answer.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}
          </SectionCard>

          <SectionCard title="Evidence" padded>
            {answer.evidence.length === 0 ? (
              <p className="text-sm text-fg-muted">No deterministic evidence available.</p>
            ) : (
              <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3" data-testid="cloud-ai-evidence">
                {answer.evidence.map((card, i) => (
                  <li
                    key={`${card.type}-${i}`}
                    className="rounded-md border border-border bg-surface-2 p-3"
                  >
                    <div className="text-xs uppercase text-fg-muted">{card.type}</div>
                    <div className="text-sm font-medium text-fg-primary">{card.label}</div>
                    <div className="mt-1 text-lg font-mono">
                      {card.value}
                      {card.currency ? ` ${card.currency}` : ''}
                    </div>
                    {card.secondary && (
                      <div className="mt-1 text-xs text-fg-muted">{card.secondary}</div>
                    )}
                    {card.scope && (
                      <div className="mt-1 text-[10px] uppercase text-fg-muted">scope: {card.scope}</div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>
        </>
      )}

      {!submitting && !answer && !errorMsg && (
        <SectionCard title="AI analyst" padded>
          <p className="text-sm text-fg-muted">
            Ask a question above to get a grounded multi-cloud answer. Suggested questions cover
            spend totals, top services, cost changes, optimization opportunities, and
            region/account comparisons.
          </p>
        </SectionCard>
      )}
    </div>
  )
}
