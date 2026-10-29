// Phase 7C post-release hotfix #2 — provider-aware selection coercion.
//
// The canonical cross-cloud API routes resolve accounts differently
// by provider:
//
//   * AWS  — ``acct:<cloud account_id>`` (the 12-digit AWS account
//     number carried by the registry row's ``account_id``).
//   * Azure — ``sub:<subscription-guid>`` (Cost Management,
//     Resource Graph and Advisor all resolve the subscription GUID).
//   * RACKSPACE — ``acct:<cloud tenant/account id>`` (Phase 9B.1
//     canonical routes resolve the Rackspace account id, never the
//     local database row id).
//
// The AccountSelector now emits the correct form per provider, so
// new selections are already canonical.  This helper is the
// *defence-in-depth* boundary at the API client layer: a stale
// pre-hotfix session may still hold an ``acct:<n>`` value for an
// Azure selection, and passing it through would produce
// ``no AZURE account matches selection='acct:5'``.  We coerce a
// stale ``acct:<n>`` under the Azure provider back to ``all``
// (self-correction) instead of sending a meaningless selector to
// an Azure route.
//
// Phase 9D.1 closure — the LOCAL DATABASE PRIMARY KEY is a
// backend-only key and must NEVER reach a canonical API as a
// cloud selector.  ``buildAccountSelector`` is the single mapping
// every selectable-account row goes through (selector UI, label
// lookup, dashboard attribution, selection revalidation).

export type CanonicalProvider = 'AWS' | 'AZURE' | 'RACKSPACE'

/** Lowercase cache-provider spelling (``aws``/``azure``/``rackspace``). */
export type CacheProviderSlug = 'aws' | 'azure' | 'rackspace'

import type { AccountSelection } from '../accountContext'

type ParticipantProvider = CanonicalProvider | CacheProviderSlug

function isAzureProvider(provider: ParticipantProvider): boolean {
  return provider === 'AZURE' || provider === 'azure'
}

// ---------------------------------------------------------------------------
// Phase 9D.1 — canonical selectable-account mapping (single source of truth)
// ---------------------------------------------------------------------------

/** Minimal shape of a selectable-account row the mapping needs.
 *  ``account_id`` is the CLOUD-side identifier (AWS account number,
 *  Azure subscription GUID, Rackspace account id).  The local
 *  database row primary key (``id``) is deliberately NOT part of
 *  this contract — it must never become a cloud selector. */
export interface AccountSelectionRow {
  account_id: string
}

/**
 * Build the canonical selector value for a selectable-account row.
 *
 *   * AWS       → ``acct:<cloud account_id>``   (never ``acct:<row id>``)
 *   * RACKSPACE → ``acct:<cloud account_id>``   (never ``acct:<row id>``)
 *   * AZURE     → ``sub:<subscription-guid>``   (unchanged Phase 7C contract)
 */
export function buildAccountSelector(
  row: AccountSelectionRow,
  provider: CanonicalProvider,
): AccountSelection {
  if (isAzureProvider(provider)) {
    return `sub:${row.account_id}`
  }
  return `acct:${row.account_id}`
}

/**
 * True when ``selection`` refers to ``row`` under ``provider``.
 * Matching is always on the CLOUD-side ``account_id`` — never on
 * the local database row id — so a database re-sequence can never
 * silently repoint a user's selection at a different account.
 */
export function accountMatchesSelector(
  row: AccountSelectionRow,
  selection: string | null | undefined,
  provider: CanonicalProvider,
): boolean {
  if (!selection || selection === 'all') return false
  if (isAzureProvider(provider)) {
    return selection === `sub:${row.account_id}`
  }
  return selection === `acct:${row.account_id}`
}

/** Provider-aware noun for the account selector row label.
 *  "AWS Account" / "Azure Subscription" / "Rackspace Account". */
export function providerAccountNoun(provider: CanonicalProvider): string {
  if (provider === 'AZURE') return 'Azure Subscription'
  if (provider === 'RACKSPACE') return 'Rackspace Account'
  return 'AWS Account'
}

/** Provider-aware aggregate label for the "all accounts" option. */
export function providerAggregateLabel(provider: CanonicalProvider): string {
  if (provider === 'AZURE') return 'All Azure Subscriptions'
  if (provider === 'RACKSPACE') return 'All Rackspace Cloud Accounts'
  return 'All AWS Accounts'
}

/**
 * Return the canonical ``account_id`` query value for a selection
 * under the given provider, or ``undefined`` when the selection
 * should be omitted (i.e. "all" or a self-corrected stale value).
 */
export function canonicalAccountParam(
  accountId: string | null | undefined,
  provider: CanonicalProvider,
): string | undefined {
  if (!accountId || accountId === 'all') return undefined
  // Azure canonical selectors must be ``sub:<guid>``.  A stale
  // ``acct:<n>`` is meaningless to the Azure routes — coerce to
  // "all" (omit the param) rather than emit ``acct:5`` / ``acct:6``.
  if (isAzureProvider(provider)) {
    if (accountId.startsWith('sub:')) return accountId
    return undefined
  }
  // Phase 9D.1 — AWS / RACKSPACE speak ``acct:<id>``.  A stale
  // Azure ``sub:<guid>`` must never be submitted under a
  // non-Azure provider (it can only fail with AccountNotFound).
  if (accountId.startsWith('sub:')) return undefined
  return accountId
}

/**
 * Canonical cache-key segment for a selection under a provider.
 * Used by the client cache-key builders so a stale Azure
 * ``acct:<n>`` self-corrects to ``all`` in the key shape too,
 * avoiding a stale authoritative-zero cache entry.
 */
export function canonicalCacheSegment(
  accountId: string | null | undefined,
  provider: ParticipantProvider,
): string {
  if (!accountId || accountId === 'all') return 'all'
  if (isAzureProvider(provider) && !accountId.startsWith('sub:')) {
    // Stale pre-hotfix Azure selection — treat as "all".
    return 'all'
  }
  if (!isAzureProvider(provider) && accountId.startsWith('sub:')) {
    // Phase 9D.1 — stale Azure selector under AWS / RACKSPACE.
    return 'all'
  }
  return accountId
}

// ---------------------------------------------------------------------------
// Phase 9D.1 — account-scope failure UX (professional text)
// ---------------------------------------------------------------------------

/** Shape shared by ``Error`` / ``ApiError`` instances. */
interface ErrorLike {
  message?: string | null
  errorCode?: string | null
}

const ACCOUNT_NOT_FOUND_MESSAGE = /no\s+(AWS|AZURE|RACKSPACE)\s+account\s+matches\s+selection/i

/**
 * Map an account-scope failure (``AccountNotFound`` — the backend's
 * ``no <PROVIDER> account matches selection='<selector>'`` — or a
 * disabled account) to provider-aware professional text.  Returns
 * ``null`` when the error is NOT an account-scope failure so the
 * caller keeps its existing message untouched.
 *
 * The raw backend text (which carries the selector form and
 * internal identifiers) must never reach the browser UI.
 */
export function friendlyAccountScopeErrorMessage(
  error: ErrorLike | null | undefined,
  provider: CanonicalProvider,
): string | null {
  if (!error) return null
  const isNotFound =
    error.errorCode === 'AccountNotFound' ||
    ACCOUNT_NOT_FOUND_MESSAGE.test(error.message ?? '')
  if (!isNotFound && error.errorCode !== 'AccountDisabled') return null
  if (error.errorCode === 'AccountDisabled') {
    return `The selected ${providerAccountNoun(provider).toLowerCase()} is currently disabled. Please choose another one or contact an administrator.`
  }
  const noun = providerAccountNoun(provider)
  const pick = provider === 'AZURE' ? 'subscription' : 'account'
  return `The selected ${noun} is no longer available. Please choose another ${pick}.`
}