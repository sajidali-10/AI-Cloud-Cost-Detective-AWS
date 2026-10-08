// Phase 6B — Presentation-only formatters.
//
// Every helper here is pure and tested.  These helpers accept the
// raw string-from-the-wire (Decimal) and return display strings.
// They NEVER fabricate a value: a null input renders as "—", a 0
// renders as "$0.00", and a previous-period of 0 makes
// `formatChangePercent` return "—".

import type { LookbackDays } from '../types/finops'

/** Mask an AWS account id like "974053642038" → "9740…2038". */
export function maskAccountId(account: string | null | undefined): string {
  if (!account || typeof account !== 'string') return '—'
  if (account.length <= 6) return account
  return `${account.slice(0, 4)}…${account.slice(-4)}`
}

/** Parse a Decimal-serialized number, or null when invalid. */
export function parseDecimal(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/**
 * Format a Decimal-serialized amount as a currency string.
 * Always returns the currency code when amount is non-null so
 * readers never have to guess whether the number is in USD or EUR.
 */
export function formatCurrencyDecimal(
  value: unknown,
  currency: string = 'USD',
  options: { maximumFractionDigits?: number; emptyFallback?: string } = {},
): string {
  const num = parseDecimal(value)
  if (num === null) return options.emptyFallback ?? '—'
  const max = options.maximumFractionDigits ?? 2
  const formatted = num.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: max,
  })
  return `${currency} ${formatted}`
}

/**
 * Compact currency formatter for KPI values: $1.2K, $3.45M, $629.
 * Returns "—" for null.  Never invents precision — uses the integer
 * portion unless the value is below 1000.
 */
export function formatCurrencyCompact(
  value: unknown,
  currency: string = 'USD',
  options: { emptyFallback?: string } = {},
): string {
  const num = parseDecimal(value)
  if (num === null) return options.emptyFallback ?? '—'
  const abs = Math.abs(num)
  let body: string
  if (abs >= 1_000_000) {
    body = `$${(num / 1_000_000).toFixed(2)}M`
  } else if (abs >= 1_000) {
    body = `$${(num / 1_000).toFixed(2)}K`
  } else {
    body = `$${num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  }
  return `${currency === 'USD' ? body : `${currency} ${body}`}`
}

/**
 * Render a signed USD change amount: "+$12.34" or "−$12.34".
 * Returns "—" for null.
 */
export function formatSignedChange(
  value: unknown,
  currency: string = 'USD',
  options: { emptyFallback?: string } = {},
): string {
  const num = parseDecimal(value)
  if (num === null) return options.emptyFallback ?? '—'
  const abs = Math.abs(num)
  const formatted = abs.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  const prefix = currency === 'USD' ? '$' : `${currency} `
  if (num === 0) return `${prefix}${formatted}`
  const sign = num > 0 ? '+' : '−'
  return `${sign}${prefix}${formatted}`
}

/**
 * Render a signed percentage change: "+13.5%" or "−7.2%".
 * Returns "—" when previous == 0 (no false "Critical" cost increase).
 */
export function formatChangePercent(
  current: unknown,
  previous: unknown,
  options: { emptyFallback?: string } = {},
): string {
  const cur = parseDecimal(current)
  const prev = parseDecimal(previous)
  if (cur === null || prev === null) return options.emptyFallback ?? '—'
  if (prev === 0) return options.emptyFallback ?? '—'
  const diff = cur - prev
  const pct = (diff / prev) * 100
  const abs = Math.abs(pct)
  const sign = pct > 0 ? '+' : '−'
  return `${sign}${abs.toFixed(1)}%`
}

/**
 * Format a percentage derived from two values (share of total).
 * `safe = false` when the denominator is zero — caller can decide to
 * hide the column entirely.
 */
export function formatSharePercent(
  numerator: unknown,
  denominator: unknown,
  options: { emptyFallback?: string; fractionDigits?: number } = {},
): { text: string; safe: boolean } {
  const num = parseDecimal(numerator)
  const den = parseDecimal(denominator)
  if (num === null || den === null || den === 0) {
    return { text: options.emptyFallback ?? '—', safe: false }
  }
  const pct = (num / den) * 100
  const digits = options.fractionDigits ?? 1
  return { text: `${pct.toFixed(digits)}%`, safe: true }
}

/** "USD" or the actual currency code — useful when cost is non-USD. */
export function currencySymbol(currency: string): string {
  return currency === 'USD' ? '$' : `${currency} `
}

/** Human label for a lookback window: 30 → "30 days". */
export function periodLabel(days: LookbackDays | number): string {
  if (days === 7) return 'Last 7 days'
  if (days === 30) return 'Last 30 days'
  if (days === 60) return 'Last 60 days'
  if (days === 90) return 'Last 90 days'
  return `Last ${days} days`
}

/** Short suffix: "7d", "30d", "60d", "90d". */
export function periodShortLabel(days: number): string {
  return `${days}d`
}

/** "10:42 AM" — used in PageHeader context row. */
export function formatLocalTime(date: Date | string | null | undefined): string {
  if (!date) return '—'
  const d = typeof date === 'string' ? new Date(date) : date
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  })
}

/** "Oct 8" — short date label. */
export function formatShortDate(value: string | Date): string {
  const d = typeof value === 'string' ? new Date(value + (value.length === 10 ? 'T00:00:00Z' : '')) : value
  if (!(d instanceof Date) || Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

/** Friendly label for a service status value. */
export function serviceStatusLabel(s: string | null | undefined): string {
  switch (s) {
    case 'ok':
      return 'Healthy'
    case 'denied':
      return 'Access denied'
    case 'error':
      return 'Error'
    case null:
    case undefined:
    case '':
      return 'Unknown'
    default:
      return s
  }
}

/** Friendly label for a capability status. */
export function capabilityLabel(s: string | null | undefined): string {
  switch (s) {
    case 'ACTIVE':
      return 'Active'
    case 'AVAILABLE':
      return 'Available'
    case 'INACTIVE':
      return 'Inactive'
    case 'NOT_ENROLLED':
      return 'Not enrolled'
    case 'PENDING':
      return 'Pending'
    case 'FAILED':
      return 'Failed'
    case 'ACCESS_DENIED':
      return 'Access denied'
    case 'UNAVAILABLE':
      return 'Unavailable'
    case null:
    case undefined:
    case '':
      return 'Unknown'
    default:
      return s
  }
}

/** Tone suggestion for a capability status (semantic palette). */
export function capabilityTone(s: string | null | undefined): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (s) {
    case 'ACTIVE':
    case 'AVAILABLE':
      return 'success'
    case 'INACTIVE':
    case 'NOT_ENROLLED':
    case 'PENDING':
      return 'warning'
    case 'FAILED':
    case 'ACCESS_DENIED':
    case 'UNAVAILABLE':
      return 'danger'
    default:
      return 'neutral'
  }
}

/** Friendly label for a savings source. */
export function savingsSourceLabel(s: string | null | undefined): string {
  switch (s) {
    case 'AWS_COST_OPTIMIZATION_HUB':
      return 'AWS Cost Optimization Hub'
    case 'AWS_COMPUTE_OPTIMIZER':
      return 'AWS Compute Optimizer'
    case 'CALCULATED':
      return 'Calculated'
    case 'UNKNOWN':
      return 'Deterministic'
    case null:
    case undefined:
    case '':
      return 'Unknown'
    default:
      return s
  }
}

/** Tone for a savings source badge. */
export function savingsSourceTone(
  s: string | null | undefined,
): 'info' | 'ai' | 'success' | 'neutral' {
  switch (s) {
    case 'AWS_COST_OPTIMIZATION_HUB':
    case 'AWS_COMPUTE_OPTIMIZER':
      return 'info'
    case 'CALCULATED':
      return 'success'
    case 'UNKNOWN':
      return 'ai'
    default:
      return 'neutral'
  }
}

/** Friendly label for a confidence value. */
export function confidenceLabel(c: string | null | undefined): string {
  switch (c) {
    case 'HIGH':
      return 'High'
    case 'MEDIUM':
      return 'Medium'
    case 'LOW':
      return 'Low'
    default:
      return c ?? '—'
  }
}

export function confidenceTone(c: string | null | undefined): 'success' | 'warning' | 'danger' | 'neutral' {
  switch (c) {
    case 'HIGH':
      return 'success'
    case 'MEDIUM':
      return 'warning'
    case 'LOW':
      return 'danger'
    default:
      return 'neutral'
  }
}

/**
 * Tone for a signed change vs. previous period.  A cost decrease is
 * success; an increase is warning.  Spec: NEVER "Critical" without
 * supporting evidence.
 */
export function changeTone(
  current: unknown,
  previous: unknown,
): 'success' | 'warning' | 'neutral' {
  const cur = parseDecimal(current)
  const prev = parseDecimal(previous)
  if (cur === null || prev === null) return 'neutral'
  if (cur === prev) return 'neutral'
  return cur > prev ? 'warning' : 'success'
}

/** Convenience: collapse a small list into "Other" deterministically. */
export function collapseTail<T>(
  items: T[],
  size: number,
  combine: (rest: T[]) => T,
): T[] {
  if (items.length <= size) return items
  return [...items.slice(0, size - 1), combine(items.slice(size - 1))]
}
