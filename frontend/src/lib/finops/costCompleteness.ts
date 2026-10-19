// Phase 7D.3.2 / 7D.3.3 — Shared cost-completeness and readiness helpers.
//
// This module is the SINGLE source of truth for three questions
// the Dashboard and Costs pages used to answer differently:
//
//   1. Given a successful cost response, is the numeric total
//      authoritative?  (True zero vs partial subtotal vs all-
//      failed unavailable.)  See ``deriveCostCompleteness``.
//
//   2. Given a set of in-flight / completed / errored domains,
//      what single readiness label should the page surface?
//      See ``deriveDashboardReadiness`` and
//      ``deriveDataReadinessLabel``.
//
//   3. Given a ``data_freshness`` hint from the server (Phase
//      7D.3.3), what single freshness label should the KPI tiles
//      and hero surface?  See ``deriveDataFreshness``.
//
// Phase 7D.3.2 also locked down the asymmetry where the
// Dashboard had its own ``successful_accounts`` /
// ``failed_accounts`` branches and the Costs page rendered raw
// ``report.total_cost`` even when ``status='PARTIAL_SUCCESS'``
// and ``successful_accounts === 0``.  The asymmetry surfaced
// "$0.00" as authoritative spend whenever Azure throttled every
// subscription.
//
// Phase 7D.3.3 extends the model with the FRESH / STALE-LKG
// signal so the Dashboard / Costs pages can render "Stale ·
// last successful <time>" without breaking Phase 7D.3.2
// invariants.

import type { CostReport, CostReportResponse } from '../../types/finops'
import type { LoadStatus } from './cache'

// ---------------------------------------------------------------------------
//  Cost completeness (one report → view-model)
// ---------------------------------------------------------------------------

export type CostCompletenessKind =
  | 'success'
  | 'partial'
  | 'unavailable'

export interface CostCompleteness {
  kind: CostCompletenessKind
  /** Number of subscriptions / accounts that returned real cost data. */
  successfulAccounts: number
  /** Number of subscriptions / accounts that returned an error (THROTTLED / AUTH / ...). */
  failedAccounts: number
  /** Friendly description for KPI subtitles / chart captions. */
  description: string
}

/**
 * Classify a canonical cost report.  The contract is intentionally
 * strict: a numeric 0.00 alone NEVER proves an authoritative zero;
 * the ``status`` envelope, ``successful_accounts`` count, and
 * ``account_errors`` length all have to agree.
 *
 * Rules:
 *   * ``status='SUCCESS'`` AND ``successful > 0``        → 'success'
 *   * ``status='SUCCESS'`` AND ``successful === 0``      → 'unavailable'
 *   * ``status='PARTIAL_SUCCESS'`` AND ``successful > 0`` → 'partial'
 *   * ``status='PARTIAL_SUCCESS'`` AND ``successful === 0`` → 'unavailable'
 *   * ``status='NO_DATA'`` or ``status='ERROR'``         → 'unavailable'
 *   * Unknown / missing status                          → 'unavailable'
 *
 * When AWS does not surface per-account error information (the
 * legacy AWS adapter only emits ``status='SUCCESS'``) the
 * defaults are ``failedAccounts=0, successfulAccounts=1`` and
 * the report renders as ``'success'`` — preserving every Phase
 * 7A / 7B / 7C.1 regression contract.
 */
export function deriveCostCompleteness(
  report: CostReport | null | undefined,
): CostCompleteness {
  if (!report) {
    return {
      kind: 'unavailable',
      successfulAccounts: 0,
      failedAccounts: 0,
      description: 'no cost data',
    }
  }
  const status = String(report.status ?? 'SUCCESS').toUpperCase()
  const successful = report.successful_accounts ?? 0
  const failed = report.failed_accounts ?? 0

  if (status === 'SUCCESS') {
    if (successful === 0 && failed > 0) {
      return {
        kind: 'unavailable',
        successfulAccounts: 0,
        failedAccounts: failed,
        description: 'all accounts failed',
      }
    }
    return {
      kind: 'success',
      successfulAccounts: successful,
      failedAccounts: failed,
      description: 'authoritative',
    }
  }
  if (status === 'PARTIAL_SUCCESS') {
    if (successful === 0) {
      return {
        kind: 'unavailable',
        successfulAccounts: 0,
        failedAccounts: failed,
        description: 'all accounts failed',
      }
    }
    return {
      kind: 'partial',
      successfulAccounts: successful,
      failedAccounts: failed,
      description: `partial · ${failed} unavailable`,
    }
  }
  // NO_DATA / ERROR / unknown → unavailable.
  return {
    kind: 'unavailable',
    successfulAccounts: successful,
    failedAccounts: failed,
    description: status === 'ERROR' ? 'error' : 'no data',
  }
}

// ---------------------------------------------------------------------------
//  Data readiness (multiple domain snapshots → one label)
// ---------------------------------------------------------------------------

export type DataReadinessKind =
  | 'idle'
  | 'loading'
  | 'refreshing'
  | 'live'
  | 'partial'
  | 'unavailable'

export interface DomainSnapshot {
  status: LoadStatus
  error: Error | null
  /** Whether the snapshot holds authoritative data (any domain). */
  hasData: boolean
}

/**
 * Compute the single readiness label for the Dashboard hero /
 * header status badge and the Costs page header status.
 *
 * Decision order:
 *   1. ANY domain in 'loading' with no data yet → 'loading'.
 *   2. ANY domain in 'refreshing' with no data yet → 'refreshing'.
 *   3. At least one 'success' domain with data AND every 'success'
 *      domain passes completeness (see ``deriveCostCompleteness``
 *      when applied externally) → 'live'.
 *   4. At least one 'success' domain with data but some other
 *      domain failed → 'partial'.
 *   5. All domains failed → 'unavailable'.
 *   6. No fetches fired yet → 'idle'.
 *
 * The function does NOT inspect cost-completeness directly — the
 * caller folds the cost-completeness view-model into ``hasData``
 * so the same routine works for resources / recommendations too.
 */
export function deriveDashboardReadiness(
  domains: ReadonlyArray<DomainSnapshot>,
): DataReadinessKind {
  if (domains.length === 0) return 'idle'
  const anyLoading = domains.some(
    (d) => d.status === 'loading' && !d.hasData,
  )
  if (anyLoading) return 'loading'
  const anyRefreshing = domains.some(
    (d) => d.status === 'refreshing' && !d.hasData,
  )
  if (anyRefreshing) return 'refreshing'
  const anySuccess = domains.some((d) => d.status === 'success' && d.hasData)
  const anyError = domains.some(
    (d) => (d.status === 'error' || d.error !== null) && !d.hasData,
  )
  if (anySuccess && anyError) return 'partial'
  if (anySuccess) return 'live'
  if (anyError) return 'unavailable'
  // No data yet (initial idle state with no inflight) → 'idle'.
  const allIdle = domains.every((d) => d.status === 'idle' || (d.status === 'loading' && !d.hasData))
  if (allIdle) return 'loading'
  return 'idle'
}

const READINESS_LABEL: Record<DataReadinessKind, string> = {
  idle: 'Idle',
  loading: 'Loading',
  refreshing: 'Refreshing',
  live: 'Live',
  partial: 'Partial',
  unavailable: 'Unavailable',
}

export function dataReadinessLabel(kind: DataReadinessKind): string {
  return READINESS_LABEL[kind]
}

/**
 * Convenience helper for the cost domain that already has the
 * canonical ``CostReportResponse`` snapshot.
 */
export function costDomainSnapshot(
  response: CostReportResponse | null,
  status: LoadStatus,
  error: Error | null,
): DomainSnapshot {
  if (!response) {
    return { status, error, hasData: false }
  }
  const completeness = deriveCostCompleteness(response.report)
  return {
    status,
    error,
    hasData: completeness.kind !== 'unavailable',
  }
}

// ---------------------------------------------------------------------------
//  Phase 7D.3.3 — Data freshness (server hint → view-model)
// ---------------------------------------------------------------------------

export type DataFreshnessKind = 'live' | 'cached' | 'stale' | 'unavailable'

export interface DataFreshness {
  kind: DataFreshnessKind
  /** Friendly description for KPI subtitle: "Cached" / "Stale · last successful <time>" / etc. */
  description: string
  /** Last successful upstream timestamp; ``null`` for ``unavailable``. */
  lastSuccessfulAt: string | null
}

/**
 * Map the server-emitted ``data_freshness`` hint onto a view-model
 * the KPI tiles and hero can render directly.
 *
 * Defaults to ``'live'`` for AWS responses and for legacy fixtures
 * that do not yet populate the new field — preserves every Phase
 * 7A / 7B / 7C / 7D.1 / 7D.2 regression contract.
 */
export function deriveDataFreshness(
  response: CostReportResponse | null | undefined,
): DataFreshness {
  if (!response) {
    return { kind: 'unavailable', description: 'no data', lastSuccessfulAt: null }
  }
  const hint = response.data_freshness ?? null
  const lastSuccessful = response.last_successful_at ?? null
  switch (hint) {
    case 'STALE':
      return {
        kind: 'stale',
        description: lastSuccessful
          ? `Stale · last successful ${formatShortTimestamp(lastSuccessful)}`
          : 'Stale',
        lastSuccessfulAt: lastSuccessful,
      }
    case 'CACHED':
      return {
        kind: 'cached',
        description: lastSuccessful
          ? `Cached · ${formatShortTimestamp(lastSuccessful)}`
          : 'Cached',
        lastSuccessfulAt: lastSuccessful,
      }
    case 'UNAVAILABLE':
      return {
        kind: 'unavailable',
        description: 'Azure refresh temporarily unavailable',
        lastSuccessfulAt: lastSuccessful,
      }
    case 'LIVE':
    default:
      return {
        kind: 'live',
        description: 'Live',
        lastSuccessfulAt: lastSuccessful,
      }
  }
}

/**
 * Best-effort short timestamp formatter.  We do NOT pull in a
 * date library — ``Intl.DateTimeFormat`` is universally available
 * in modern browsers + Node 20+.
 */
function formatShortTimestamp(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}
