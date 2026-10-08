// Phase 6B — Format helper tests.

import { describe, expect, it } from 'vitest'
import {
  maskAccountId,
  parseDecimal,
  formatCurrencyDecimal,
  formatCurrencyCompact,
  formatSignedChange,
  formatChangePercent,
  formatSharePercent,
  periodLabel,
  periodShortLabel,
  formatLocalTime,
  formatShortDate,
  capabilityLabel,
  capabilityTone,
  savingsSourceLabel,
  savingsSourceTone,
  confidenceLabel,
  confidenceTone,
  changeTone,
  serviceStatusLabel,
} from '../lib/format'

describe('maskAccountId', () => {
  it('masks long ids', () => {
    expect(maskAccountId('974053642038')).toBe('9740…2038')
  })
  it('returns short ids unchanged', () => {
    expect(maskAccountId('abc')).toBe('abc')
  })
  it('handles null and undefined', () => {
    expect(maskAccountId(null)).toBe('—')
    expect(maskAccountId(undefined)).toBe('—')
  })
})

describe('parseDecimal', () => {
  it('parses strings', () => {
    expect(parseDecimal('12.34')).toBe(12.34)
  })
  it('parses numbers', () => {
    expect(parseDecimal(42)).toBe(42)
  })
  it('returns null on invalid input', () => {
    expect(parseDecimal('abc')).toBe(null)
    expect(parseDecimal(null)).toBe(null)
    expect(parseDecimal(undefined)).toBe(null)
    expect(parseDecimal({})).toBe(null)
  })
  it('returns null on non-finite', () => {
    expect(parseDecimal('NaN')).toBe(null)
    expect(parseDecimal(Number.POSITIVE_INFINITY)).toBe(null)
  })
})

describe('formatCurrencyDecimal', () => {
  it('formats with thousands separator', () => {
    expect(formatCurrencyDecimal('629.88')).toBe('USD 629.88')
  })
  it('handles null', () => {
    expect(formatCurrencyDecimal(null)).toBe('—')
  })
  it('respects custom currency', () => {
    expect(formatCurrencyDecimal('100', 'EUR')).toBe('EUR 100.00')
  })
})

describe('formatCurrencyCompact', () => {
  it('formats thousands as K', () => {
    expect(formatCurrencyCompact('2438.08')).toBe('$2.44K')
  })
  it('formats millions as M', () => {
    expect(formatCurrencyCompact('1234567')).toBe('$1.23M')
  })
  it('handles small values', () => {
    expect(formatCurrencyCompact('12.5')).toBe('$12.50')
  })
  it('handles null', () => {
    expect(formatCurrencyCompact(null)).toBe('—')
  })
})

describe('formatSignedChange', () => {
  it('renders positive amounts with plus sign', () => {
    expect(formatSignedChange('12.34')).toBe('+$12.34')
  })
  it('renders negative amounts with minus sign', () => {
    expect(formatSignedChange('-12.34')).toBe('−$12.34')
  })
  it('renders zero as $0.00', () => {
    expect(formatSignedChange('0')).toBe('$0.00')
  })
  it('handles null', () => {
    expect(formatSignedChange(null)).toBe('—')
  })
})

describe('formatChangePercent', () => {
  it('renders positive', () => {
    expect(formatChangePercent('135', '100')).toBe('+35.0%')
  })
  it('renders negative', () => {
    expect(formatChangePercent('92.8', '100')).toBe('−7.2%')
  })
  it('returns placeholder on zero previous', () => {
    expect(formatChangePercent('100', '0')).toBe('—')
    expect(formatChangePercent('100', '0', { emptyFallback: 'No prior' })).toBe('No prior')
  })
  it('returns placeholder on missing data', () => {
    expect(formatChangePercent(null, '100')).toBe('—')
    expect(formatChangePercent('100', null)).toBe('—')
  })
})

describe('formatSharePercent', () => {
  it('renders share', () => {
    expect(formatSharePercent('50', '200')).toEqual({ text: '25.0%', safe: true })
  })
  it('returns placeholder on zero denominator', () => {
    expect(formatSharePercent('50', '0')).toEqual({ text: '—', safe: false })
  })
})

describe('periodLabel / periodShortLabel', () => {
  it('handles known values', () => {
    expect(periodLabel(7)).toBe('Last 7 days')
    expect(periodLabel(30)).toBe('Last 30 days')
    expect(periodLabel(60)).toBe('Last 60 days')
    expect(periodLabel(90)).toBe('Last 90 days')
  })
  it('handles unknown values', () => {
    expect(periodLabel(15)).toBe('Last 15 days')
  })
  it('formats short', () => {
    expect(periodShortLabel(30)).toBe('30d')
  })
})

describe('formatLocalTime', () => {
  it('formats ISO string', () => {
    expect(formatLocalTime('2026-10-08T14:42:00Z')).toMatch(/\d{1,2}:\d{2}\s?(AM|PM)/)
  })
  it('handles null', () => {
    expect(formatLocalTime(null)).toBe('—')
  })
})

describe('formatShortDate', () => {
  it('formats ISO date', () => {
    expect(formatShortDate('2026-10-08')).toBe('Oct 8')
  })
})

describe('capabilityLabel / capabilityTone', () => {
  it('maps statuses to labels', () => {
    expect(capabilityLabel('ACTIVE')).toBe('Active')
    expect(capabilityLabel('NOT_ENROLLED')).toBe('Not enrolled')
    expect(capabilityLabel('UNAVAILABLE')).toBe('Unavailable')
    expect(capabilityLabel(null)).toBe('Unknown')
  })
  it('maps tones', () => {
    expect(capabilityTone('ACTIVE')).toBe('success')
    expect(capabilityTone('NOT_ENROLLED')).toBe('warning')
    expect(capabilityTone('FAILED')).toBe('danger')
    expect(capabilityTone(null)).toBe('neutral')
  })
})

describe('savingsSourceLabel / savingsSourceTone', () => {
  it('maps labels', () => {
    expect(savingsSourceLabel('AWS_COST_OPTIMIZATION_HUB')).toBe('AWS Cost Optimization Hub')
    expect(savingsSourceLabel('AWS_COMPUTE_OPTIMIZER')).toBe('AWS Compute Optimizer')
    expect(savingsSourceLabel('UNKNOWN')).toBe('Deterministic')
  })
  it('maps tones', () => {
    expect(savingsSourceTone('AWS_COMPUTE_OPTIMIZER')).toBe('info')
    expect(savingsSourceTone('UNKNOWN')).toBe('ai')
    expect(savingsSourceTone(null)).toBe('neutral')
  })
})

describe('confidenceLabel / confidenceTone', () => {
  it('maps labels', () => {
    expect(confidenceLabel('HIGH')).toBe('High')
    expect(confidenceLabel('LOW')).toBe('Low')
  })
  it('maps tones', () => {
    expect(confidenceTone('HIGH')).toBe('success')
    expect(confidenceTone('MEDIUM')).toBe('warning')
    expect(confidenceTone('LOW')).toBe('danger')
  })
})

describe('changeTone', () => {
  it('returns warning for increase', () => {
    expect(changeTone('150', '100')).toBe('warning')
  })
  it('returns success for decrease', () => {
    expect(changeTone('80', '100')).toBe('success')
  })
  it('returns neutral for equal', () => {
    expect(changeTone('100', '100')).toBe('neutral')
  })
  it('returns neutral for missing data', () => {
    expect(changeTone(null, '100')).toBe('neutral')
  })
})

describe('serviceStatusLabel', () => {
  it('maps statuses', () => {
    expect(serviceStatusLabel('ok')).toBe('Healthy')
    expect(serviceStatusLabel('denied')).toBe('Access denied')
    expect(serviceStatusLabel('error')).toBe('Error')
    expect(serviceStatusLabel(null)).toBe('Unknown')
  })
})
