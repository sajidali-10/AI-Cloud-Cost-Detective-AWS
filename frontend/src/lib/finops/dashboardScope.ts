// Phase 7D.3 — shared Dashboard scope helpers.
//
// One source of truth for every identity / attribution decision
// the unified executive dashboard needs to render for either
// provider.  Both the Dashboard and any future dashboard sub-card
// (KPI tile, header, summary section) MUST go through these
// helpers — there must NOT be a parallel implementation per
// provider.
//
// Provider neutrality rules (Phase 7D.3):
//
//   * Friendly attribution labels only.  ``acct:<n>`` and
//     ``sub:<guid>`` are never surfaced in the executive UI.
//     When the active selection is unknown the helper renders the
//     aggregate label (``All AWS Accounts`` / ``All Azure
//     Subscriptions``) so the user never sees a raw identifier.
//   * Cache-key segments always include the lower-case provider
//     prefix + the canonical selection segment.  This is what
//     guarantees an AWS response can never satisfy an Azure
//     request (or vice versa) — the keys live in different
//     namespaces.
//   * A monotonically increasing ``generation`` token is part of
//     every dashboard query so a late AWS response cannot
//     overwrite Azure state (or vice versa) when the user
//     switches provider mid-flight.  Each provider/scope change
//     bumps the generation; any in-flight fetches the caller
//     started before the change discard their result via a
//     `useDashboardGeneration` hook in the page component.
//
// This module is provider-aware only — it does not know the
// period; the period is owned by `useFinopsPeriod` and folded
// into cost cache keys at the call site.

import type { AccountSelection, SelectableAccount } from '../accountContext'
import type { ProviderKind } from '../providerContext'

export type DashboardProvider = Extract<ProviderKind, 'AWS' | 'AZURE' | 'RACKSPACE'>
export type DashboardSelection = AccountSelection

export interface DashboardAccountMap {
  /** Lower-case provider slug — used for cache-key prefixes. */
  slug: 'aws' | 'azure' | 'rackspace'
  /** Upper-case wire form — matches the canonical API contract. */
  wire: DashboardProvider
  /** SelectableAccount rows already filtered to the active provider. */
  rows: ReadonlyArray<SelectableAccount>
}

/**
 * Normalise the provider from `useProviderContext` into the
 * dashboard wire form.  Phase 9D.1 — RACKSPACE is now a fully
 * supported dashboard provider (canonical costs, resources and
 * recommendations all report SUPPORTED through
 * ``/api/cloud/providers``); it is no longer treated as
 * out-of-range.
 */
export function asDashboardProvider(provider: ProviderKind): DashboardProvider | null {
  if (provider === 'AWS' || provider === 'AZURE' || provider === 'RACKSPACE') {
    return provider
  }
  return null
}

/**
 * Lower-case provider slug used for cache-key namespaces.
 * Returning a literal (not a `string`) keeps the type checker
 * honest at the call site.
 */
export function providerSlug(provider: ProviderKind): 'aws' | 'azure' | 'rackspace' | null {
  const wire = asDashboardProvider(provider)
  if (wire === null) return null
  if (wire === 'AZURE') return 'azure'
  if (wire === 'RACKSPACE') return 'rackspace'
  return 'aws'
}

/**
 * Friendly scope label for the dashboard subtitle, KPI tiles,
 * page-header context and any "View Resources / View Optimization"
 * link badge.  Aggregate scopes collapse to the canonical phrasing.
 *
 *   * 'all' under AWS  → "All AWS Accounts"
 *   * 'all' under Azure → "All Azure Subscriptions"
 *   * 'acct:<n>' under AWS → display_name from the matching row
 *   * 'sub:<guid>' under Azure → display_name from the matching row
 *   * unknown → fallback aggregate label, never a raw id
 */
export function friendlyScopeLabel(
  selection: DashboardSelection,
  accounts: DashboardAccountMap,
): string {
  if (selection === 'all' || selection === null) {
    if (accounts.wire === 'AZURE') return 'All Azure Subscriptions'
    if (accounts.wire === 'RACKSPACE') return 'All Rackspace Cloud Accounts'
    return 'All AWS Accounts'
  }
  const row = findAccountRow(selection, accounts)
  if (row) return row.display_name
  // Self-corrected stale or unknown — never leak the raw id.
  if (selection.startsWith('sub:')) {
    const guid = selection.slice('sub:'.length)
    const masked = `${guid.slice(0, 4)}…${guid.slice(-4)}`
    return `Subscription ${masked}`
  }
  if (selection.startsWith('acct:')) {
    return `Account ${selection.slice('acct:'.length)}`
  }
  if (accounts.wire === 'AZURE') return 'Azure Subscription'
  if (accounts.wire === 'RACKSPACE') return 'Rackspace Cloud Account'
  return 'AWS Account'
}

/**
 * Masked identifier suitable for the page-header context row.
 * Mirrors `friendlyScopeLabel` for non-aggregate scopes; for
 * aggregate scopes we return the same friendly phrasing so the
 * page header never shows an empty context row.
 */
export function friendlyScopeMaskedId(
  selection: DashboardSelection,
  accounts: DashboardAccountMap,
): string {
  return friendlyScopeLabel(selection, accounts)
}

/**
 * Provider-neutral subtitle string used by the Dashboard page
 * header.  Replaces the AWS-specific copy the old dashboard
 * carried and the Azure-specific copy the new dashboard carried.
 */
export function providerSubtitle(provider: DashboardProvider): string {
  if (provider === 'AZURE') {
    return 'Azure cost visibility, resource inventory, optimization and AI-powered FinOps analysis.'
  }
  if (provider === 'RACKSPACE') {
    return 'Rackspace cost visibility, resource inventory, optimization and AI-powered FinOps analysis.'
  }
  return 'AWS cost visibility, optimization and AI-powered FinOps analysis.'
}

/**
 * Canonical cache-key segment for a selection.  Always includes
 * the provider slug, so a stale Azure response can never satisfy
 * an AWS request (and vice versa).  Unknown selectors self-correct
 * to the aggregate segment so a pre-fix session cannot produce a
 * stale authoritative-zero cache entry.
 */
export function dashboardScopeCacheSegment(
  selection: DashboardSelection,
  provider: DashboardProvider,
): string {
  if (selection === 'all' || selection === null) {
    return `${provider.toLowerCase()}:all`
  }
  // Coerce stale Azure ``acct:<n>`` back to the aggregate segment.
  if (provider === 'AZURE' && selection.startsWith('acct:')) {
    return 'azure:all'
  }
  return `${provider.toLowerCase()}:${selection}`
}

/**
 * Return the row from the selectable-account map that matches the
 * given selection, or null when there is no match.
 */
export function findAccountRow(
  selection: DashboardSelection,
  accounts: DashboardAccountMap,
): SelectableAccount | null {
  if (selection === 'all' || selection === null) return null
  const rows = accounts.rows
  // For Azure selections (sub:<guid>) match by raw account_id
  // (which is the GUID, NOT the local row id).  For AWS
  // selections (acct:<n>) match by local row id.
  if (selection.startsWith('sub:')) {
    const guid = selection.slice('sub:'.length)
    return rows.find((r) => r.account_id === guid) ?? null
  }
  if (selection.startsWith('acct:')) {
    const id = Number(selection.slice('acct:'.length))
    return rows.find((r) => r.id === id) ?? null
  }
  return null
}

/**
 * Generate a new dashboard generation token.  Called from the
 * DashboardPage component whenever the provider or scope changes
 * so the dashboard-level hooks can:
 *
 *   * rebuild their cache keys (so any stale entry cannot satisfy
 *     a new request),
 *   * track the generation at fetch-start so a late response from
 *     a previous generation is discarded before it can overwrite
 *     fresh state.
 */
let __dashboardGeneration = 0
export function nextDashboardGeneration(): number {
  __dashboardGeneration += 1
  return __dashboardGeneration
}

export function currentDashboardGeneration(): number {
  return __dashboardGeneration
}

/** Test-only reset hook.  Tests that exercise provider switching
 *  call this in ``beforeEach`` so each test starts at generation 0. */
export function __resetDashboardGenerationForTests(): void {
  __dashboardGeneration = 0
}
