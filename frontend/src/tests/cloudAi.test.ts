// Phase 7D.4 — multi-cloud AI FinOps Analyst client tests.

import { describe, expect, it } from 'vitest'

import {
  buildCloudAIAnalyzeKey,
  buildCloudAIStatusKey,
  hashQuestion,
} from '../lib/cloudAi'

describe('buildCloudAIAnalyzeKey — provider / scope / period isolation', () => {
  it('AWS all + Oct differs from AWS all + Sep', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'How much?', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'How much?', period: { month: '2026-09' },
    })
    expect(a).not.toBe(b)
  })

  it('AWS MCPP and AWS All-Azure differ', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'sub:mcpp', question: 'q', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    expect(a).not.toBe(b)
  })

  it('AWS vs Azure differ', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AZURE', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    expect(a).not.toBe(b)
  })

  it('same inputs produce same key (idempotent)', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    expect(a).toBe(b)
  })

  it('different questions produce different keys', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'how much?', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'top services?', period: { month: '2026-10' },
    })
    expect(a).not.toBe(b)
  })

  it('empty scope defaults to all', () => {
    const a = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: '', question: 'q', period: { month: '2026-10' },
    })
    const b = buildCloudAIAnalyzeKey({
      provider: 'AWS', scope: 'all', question: 'q', period: { month: '2026-10' },
    })
    expect(a).toBe(b)
  })
})

describe('buildCloudAIStatusKey — stable', () => {
  it('returns the same key on every call', () => {
    expect(buildCloudAIStatusKey()).toBe(buildCloudAIStatusKey())
  })
})

describe('hashQuestion — deterministic + short', () => {
  it('same input produces same hash', () => {
    expect(hashQuestion('hello world')).toBe(hashQuestion('hello world'))
  })

  it('different inputs produce different hashes', () => {
    expect(hashQuestion('hello')).not.toBe(hashQuestion('world'))
  })

  it('hash is short (not the full question text)', () => {
    const long = 'x'.repeat(500)
    const hash = hashQuestion(long)
    expect(hash.length).toBeLessThan(20)
  })
})
